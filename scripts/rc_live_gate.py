"""Phase 10B live-gate runner; records BLOCKED when prerequisites are absent."""
# ruff: noqa: E501, S603, S607

from __future__ import annotations

import json
import os
import platform
import re
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "evidence" / "product-v1" / "rc-live"
GATES = (
    "docker_compose",
    "postgresql",
    "postgresql_concurrency",
    "redis_worker",
    "qdrant",
    "oidc",
    "external_llm_provider",
    "tenant_isolation",
    "model_router",
    "finops",
    "rate_limiting",
    "backup_restore",
    "readiness_failure_recovery",
    "browser_e2e",
    "rag_query",
    "query_idempotency",
    "reservation_reconciliation",
    "ocr_ingestion",
    "document_intelligence",
    "web_e2e",
    "privacy_egress",
    "security",
)
# Gates that persist their own detailed evidence, which the runner merges instead of replacing.
DETAILED_EVIDENCE_GATES = frozenset(
    {
        "redis_worker",
        "qdrant",
        "external_llm_provider",
        "tenant_isolation",
        "model_router",
        "finops",
        "rate_limiting",
        "backup_restore",
        "readiness_failure_recovery",
        "browser_e2e",
        "rag_query",
        "query_idempotency",
        "reservation_reconciliation",
        "ocr_ingestion",
        "document_intelligence",
        "web_e2e",
        "privacy_egress",
        "security",
    }
)
# The security gate reads the evidence of the other gates, so it always runs last.
ORCHESTRATOR_GATES = frozenset({"security"})
OIDC_BOOTSTRAP = [".venv/bin/python", "scripts/configure_local_oidc_integration.py"]
# Release evidence counts only for the tested commit and only while fresh (as in security_gate).
MAX_EVIDENCE_AGE = timedelta(hours=24)
CLOCK_SKEW = timedelta(minutes=5)
STATUSES = ("PASS", "FAIL", "BLOCKED")
COMMIT_RE = re.compile(r"[0-9a-f]{40}")
# The single documented exception (docs/RELEASE_CANDIDATE.md, paid-provider evidence policy): the
# paid provider gate is never re-run automatically, so its PASS on an ancestor commit still counts,
# but only while nothing the gate exercises has changed since that commit.
HISTORICAL_EVIDENCE_PATHS: dict[str, tuple[str, ...]] = {
    "external_llm_provider": (
        "src/ragops/llm",
        "src/ragops/modeling",
        "src/ragops/privacy",
        "scripts/external_llm_provider_gate.py",
        "pyproject.toml",
        "constraints.txt",
    ),
}
HISTORICAL_REQUIRED: dict[str, tuple[tuple[str, object], ...]] = {
    "external_llm_provider": (
        ("live_request_status", "EXECUTED"),
        ("provider_response_valid", True),
        ("request_count", 1),
    ),
}


def aggregate_statuses(statuses: dict[str, str]) -> tuple[str, str]:
    failed = [gate for gate, status in statuses.items() if status == "FAIL"]
    blocked = [gate for gate, status in statuses.items() if status == "BLOCKED"]
    if failed:
        return "FAIL", f"FAIL: mandatory gate {failed[0]} failed"
    if blocked:
        return (
            "BLOCKED",
            f"BLOCKED: {len(blocked)} mandatory gates still require live prerequisites",
        )
    return "PASS", "PASS: all mandatory gates passed"


def merge_gate_statuses(
    previous: dict[str, str], selected: set[str], gate_results: dict[str, dict[str, str]]
) -> dict[str, str]:
    """Update only selected gates in a status map (gate registry helper).

    The release result never trusts a previous summary: main() derives every status from
    validate_gate_evidence() instead.
    """
    statuses = dict(previous)
    for gate in GATES:
        statuses.setdefault(gate, "BLOCKED")
    for gate in selected.intersection(GATES):
        statuses[gate] = gate_results.get(gate, {"status": "BLOCKED"}).get("status", "BLOCKED")
    return statuses


def merge_detailed_gate_evidence(
    persisted: dict[str, object],
    fallback: dict[str, str],
    commit: str,
    timestamp: str,
    gate: str = "redis_worker",
) -> dict[str, object]:
    """Retain gate-specific diagnostics emitted by the gate implementation."""
    merged = dict(persisted)
    merged.setdefault("gate", gate)
    merged.setdefault("status", fallback.get("status", "BLOCKED"))
    merged.setdefault("reason", fallback.get("detail", "Gate did not produce evidence"))
    merged.setdefault("tested_commit", commit)
    merged.setdefault("timestamp", timestamp)
    return merged


def evidence_path(gate: str, directory: Path | None = None) -> Path:
    return (directory or OUT) / f"{gate.replace('_', '-')}.json"


class _Missing:
    pass


MISSING = _Missing()


def read_evidence(path: Path) -> object:
    """The parsed evidence, MISSING when absent, or None when it cannot be parsed."""
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return MISSING
    except OSError:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


@dataclass(frozen=True)
class EvidenceVerdict:
    status: str
    reason: str
    tested_commit: str | None = None
    basis: str = "tested_commit"

    def as_dict(self) -> dict[str, str | None]:
        return {
            "status": self.status,
            "reason": self.reason,
            "tested_commit": self.tested_commit,
            "basis": self.basis,
        }


def _git(*arguments: str, root: Path = ROOT) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(  # noqa: S603
            ["git", *arguments], cwd=root, capture_output=True, text=True, check=False, timeout=30
        )
    except (OSError, subprocess.TimeoutExpired):
        return None


def historical_evidence_check(
    gate: str, tested: str, commit: str, root: Path = ROOT
) -> tuple[bool, str]:
    """Whether an older PASS of the excepted gate still describes this commit."""
    paths = HISTORICAL_EVIDENCE_PATHS.get(gate)
    if not paths:
        return False, "no historical exception for this gate"
    ancestor = _git("merge-base", "--is-ancestor", tested, commit, root=root)
    if ancestor is None or ancestor.returncode != 0:
        return False, f"{tested[:7]} is not an ancestor of {commit[:7]}"
    changed = _git("diff", "--name-only", tested, commit, "--", *paths, root=root)
    if changed is None or changed.returncode != 0:
        return False, "the change set since the tested commit could not be determined"
    if changed.stdout.strip():
        return False, f"paths the gate exercises changed since {tested[:7]}"
    dirty = _git("status", "--porcelain", "--", *paths, root=root)
    if dirty is None or dirty.returncode != 0 or dirty.stdout.strip():
        return False, "paths the gate exercises have uncommitted changes"
    return True, f"historical PASS of ancestor {tested[:7]}; exercised paths unchanged"


def validate_gate_evidence(
    gate: str,
    data: object,
    *,
    commit: str,
    commit_time: datetime | None,
    now: datetime,
    exit_status: str | None = None,
    historical: Callable[[str, str, str], tuple[bool, str]] | None = None,
) -> EvidenceVerdict:
    """Fail-closed status of one mandatory gate from its evidence file.

    Missing or stale evidence is BLOCKED (the gate must run on this commit); malformed or
    contradictory evidence is FAIL. ``exit_status`` is the runner's view of a gate executed
    in this invocation (exit code 0 = PASS); ``historical`` decides the documented exception.
    """
    if data is MISSING:
        return EvidenceVerdict("BLOCKED", "evidence missing; the gate has not run")
    if not isinstance(data, dict):
        return EvidenceVerdict("FAIL", "evidence malformed")
    status, tested = data.get("status"), data.get("tested_commit")
    named = data.get("gate")
    if named is not None and named != gate:
        return EvidenceVerdict("FAIL", "evidence names another gate")
    if status not in STATUSES or not isinstance(tested, str) or not COMMIT_RE.fullmatch(tested):
        return EvidenceVerdict("FAIL", "evidence malformed (status or tested_commit)")
    try:
        timestamp = datetime.fromisoformat(str(data.get("timestamp")))
    except ValueError:
        timestamp = None
    if timestamp is None or timestamp.tzinfo is None:
        return EvidenceVerdict("FAIL", "evidence malformed (timestamp)", tested)
    if timestamp > now + CLOCK_SKEW:
        return EvidenceVerdict("FAIL", "evidence timestamp lies in the future", tested)
    if exit_status is not None and (exit_status == "PASS") != (status == "PASS"):
        return EvidenceVerdict(
            "FAIL", f"contradiction: gate exit {exit_status}, evidence {status}", tested
        )
    if not commit or tested != commit:
        if historical is not None and status == "PASS" and gate in HISTORICAL_EVIDENCE_PATHS:
            missing = [
                key
                for key, expected in HISTORICAL_REQUIRED.get(gate, ())
                if type(data.get(key)) is not type(expected) or data.get(key) != expected
            ]
            if missing:
                return EvidenceVerdict(
                    "BLOCKED", f"historical evidence incomplete: {', '.join(missing)}", tested
                )
            accepted, why = historical(gate, tested, commit)
            if accepted:
                return EvidenceVerdict("PASS", why, tested, "historical_exception")
            return EvidenceVerdict("BLOCKED", f"stale: {why}", tested)
        return EvidenceVerdict("BLOCKED", "stale: evidence is for another commit", tested)
    if named is None:
        return EvidenceVerdict("FAIL", "evidence names no gate", tested)
    if commit_time is None:
        return EvidenceVerdict("BLOCKED", "commit time unknown; freshness unverifiable", tested)
    if timestamp < commit_time - CLOCK_SKEW:
        return EvidenceVerdict("BLOCKED", "stale: evidence predates the commit", tested)
    if now - timestamp > MAX_EVIDENCE_AGE:
        return EvidenceVerdict("BLOCKED", "stale: evidence older than 24 hours", tested)
    reason = data.get("reason")
    return EvidenceVerdict(status, str(reason) if reason else f"{gate} {status}", tested)


def release_verdicts(
    commit: str,
    commit_time: datetime | None,
    now: datetime,
    exit_statuses: dict[str, str],
    root: Path = ROOT,
    directory: Path | None = None,
) -> dict[str, EvidenceVerdict]:
    """Validate the evidence of every mandatory gate; nothing is carried over from summaries."""

    def historical(gate: str, tested: str, head: str) -> tuple[bool, str]:
        return historical_evidence_check(gate, tested, head, root)

    return {
        gate: validate_gate_evidence(
            gate,
            read_evidence(evidence_path(gate, directory)),
            commit=commit,
            commit_time=commit_time,
            now=now,
            exit_status=exit_statuses.get(gate),
            # The exception only covers evidence that was not (re)produced in this run.
            historical=historical if gate not in exit_statuses else None,
        )
        for gate in GATES
    }


def version(command: str) -> str:
    path = shutil.which(command)
    if not path:
        return "UNAVAILABLE"
    result = subprocess.run([path, "--version"], capture_output=True, text=True, check=False)  # noqa: S603
    return (result.stdout or result.stderr).strip()


def run_gate(command: list[str]) -> tuple[str, str]:
    result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, check=False)  # noqa: S603
    status = "PASS" if result.returncode == 0 else "BLOCKED" if result.returncode == 2 else "FAIL"
    return (status, (result.stderr or result.stdout)[-500:])


def run_oidc_gate(
    run: Callable[[list[str]], tuple[str, str]], manage_gate_client: bool
) -> tuple[str, str]:
    """Run the OIDC check; with admin access, create and always remove its gate-only client."""
    if not manage_gate_client:
        return run([".venv/bin/python", "scripts/oidc_integration_check.py"])
    status, detail = run(OIDC_BOOTSTRAP)
    try:
        if status == "PASS":
            status, detail = run([".venv/bin/python", "scripts/oidc_integration_check.py"])
    finally:
        cleanup_status, cleanup_detail = run([*OIDC_BOOTSTRAP, "--cleanup"])
    if cleanup_status != "PASS":
        return "FAIL", f"OIDC gate cleanup failed: {cleanup_detail}"
    return status, detail


def blocked_placeholder(gate: str, commit: str) -> dict[str, str]:
    """Written before a detailed gate runs, so a crash can never leave older evidence."""
    return {
        "gate": gate,
        "status": "BLOCKED",
        "reason": f"{gate} gate did not produce a result",
        "tested_commit": commit,
        "timestamp": datetime.now(UTC).isoformat(),
    }


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    commit = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True, check=False
    ).stdout.strip()  # noqa: S603
    preflight = {
        "timestamp": datetime.now(UTC).isoformat(),
        "host": platform.platform(),
        "docker": version("docker"),
        "compose": version("docker-compose"),
        "commit": commit,
    }
    (OUT / "host-preflight.json").write_text(json.dumps(preflight, indent=2), encoding="utf-8")
    (OUT / "tested-commit.json").write_text(
        json.dumps({"commit": commit, "version": "0.2.0.dev0"}, indent=2), encoding="utf-8"
    )
    reason = "BLOCKED: authorized host services, network and credentials are unavailable in current sandbox"
    selected = {
        item.strip()
        for item in os.getenv("RAGOPS_RC_GATES", ",".join(GATES)).split(",")
        if item.strip()
    }
    gate_results: dict[str, dict[str, str]] = {}
    # A selected gate first loses its older evidence, so a crash can never leave it in place.
    for gate in GATES:
        if gate in selected and gate in DETAILED_EVIDENCE_GATES:
            evidence_path(gate).write_text(
                json.dumps(blocked_placeholder(gate, commit), indent=2), encoding="utf-8"
            )
    if "docker_compose" in selected and shutil.which("docker-compose"):
        status, detail = run_gate(
            [
                "docker-compose",
                "-f",
                "docker-compose.yml",
                "-f",
                "docker-compose.prod.yml",
                "config",
            ]
        )
        gate_results["docker_compose"] = {
            "status": status,
            "detail": detail or "Compose config validated",
        }
    if "oidc" in selected and os.getenv("RAGOPS_IDENTITY_PROVIDER") == "oidc":
        status, detail = run_oidc_gate(run_gate, bool(os.getenv("KEYCLOAK_ADMIN_PASSWORD")))
        gate_results["oidc"] = {
            "status": status,
            "detail": detail or "OIDC integration check passed",
        }
    if "postgresql" in selected and os.getenv("RAGOPS_TEST_DATABASE_URL", "").startswith(
        "postgresql+psycopg://"
    ):
        status, detail = run_gate([".venv/bin/pytest", "tests/product/test_postgres.py", "-q"])
        gate_results["postgresql"] = {
            "status": status,
            "detail": detail or "PostgreSQL integration test completed",
        }
    concurrency_database_url = (
        os.getenv("RAGOPS_TEST_CONCURRENCY_DATABASE_URL")
        or os.getenv("RAGOPS_TEST_DATABASE_URL")
        or ""
    )
    concurrency_fallback = os.getenv("RAGOPS_ALLOW_CONCURRENCY_DB_FALLBACK") == "1"
    if (
        "postgresql_concurrency" in selected
        and concurrency_database_url.startswith("postgresql+psycopg://")
        and (os.getenv("RAGOPS_TEST_CONCURRENCY_DATABASE_URL") or concurrency_fallback)
    ):
        status, detail = run_gate([".venv/bin/python", "scripts/postgres_concurrency_gate.py"])
        gate_results["postgresql_concurrency"] = {
            "status": status,
            "detail": detail or "Concurrency gate completed",
        }
    if "redis_worker" in selected:
        status, detail = run_gate([".venv/bin/python", "scripts/redis_worker_gate.py"])
        gate_results["redis_worker"] = {
            "status": status,
            "detail": detail or "See sanitized redis-worker.json evidence",
        }
    if "qdrant" in selected:
        status, detail = run_gate([".venv/bin/python", "scripts/qdrant_live_gate.py"])
        gate_results["qdrant"] = {
            "status": status,
            "detail": detail or "See sanitized qdrant.json evidence",
        }
    if "external_llm_provider" in selected:
        status, detail = run_gate([".venv/bin/python", "scripts/external_llm_provider_gate.py"])
        gate_results["external_llm_provider"] = {
            "status": status,
            "detail": detail or "See sanitized external-llm-provider.json evidence",
        }
    if "tenant_isolation" in selected:
        status, detail = run_gate([".venv/bin/python", "scripts/tenant_isolation_gate.py"])
        gate_results["tenant_isolation"] = {
            "status": status,
            "detail": detail or "See sanitized tenant-isolation.json evidence",
        }
    if "model_router" in selected:
        status, detail = run_gate([".venv/bin/python", "scripts/model_router_gate.py"])
        gate_results["model_router"] = {
            "status": status,
            "detail": detail or "See sanitized model-router.json evidence",
        }
    if "finops" in selected:
        status, detail = run_gate([".venv/bin/python", "scripts/finops_gate.py"])
        gate_results["finops"] = {
            "status": status,
            "detail": detail or "See sanitized finops.json evidence",
        }
    if "rate_limiting" in selected:
        status, detail = run_gate([".venv/bin/python", "scripts/rate_limiting_gate.py"])
        gate_results["rate_limiting"] = {
            "status": status,
            "detail": detail or "See sanitized rate-limiting.json evidence",
        }
    if "backup_restore" in selected:
        status, detail = run_gate([".venv/bin/python", "scripts/backup_restore_gate.py"])
        gate_results["backup_restore"] = {
            "status": status,
            "detail": detail or "See sanitized backup-restore.json evidence",
        }
    if "readiness_failure_recovery" in selected:
        status, detail = run_gate(
            [".venv/bin/python", "scripts/readiness_failure_recovery_gate.py"]
        )
        gate_results["readiness_failure_recovery"] = {
            "status": status,
            "detail": detail or "See sanitized readiness-failure-recovery.json evidence",
        }
    if "rag_query" in selected:
        status, detail = run_gate([".venv/bin/python", "scripts/rag_query_gate.py"])
        gate_results["rag_query"] = {
            "status": status,
            "detail": detail or "See sanitized rag-query.json evidence",
        }
    if "query_idempotency" in selected:
        status, detail = run_gate([".venv/bin/python", "scripts/query_idempotency_gate.py"])
        gate_results["query_idempotency"] = {
            "status": status,
            "detail": detail or "See sanitized query-idempotency.json evidence",
        }
    if "reservation_reconciliation" in selected:
        status, detail = run_gate(
            [".venv/bin/python", "scripts/reservation_reconciliation_gate.py"]
        )
        gate_results["reservation_reconciliation"] = {
            "status": status,
            "detail": detail or "See sanitized reservation-reconciliation.json evidence",
        }
    if "ocr_ingestion" in selected:
        status, detail = run_gate([".venv/bin/python", "scripts/ocr_ingestion_gate.py"])
        gate_results["ocr_ingestion"] = {
            "status": status,
            "detail": detail or "See sanitized ocr-ingestion.json evidence",
        }
    if "document_intelligence" in selected:
        status, detail = run_gate([".venv/bin/python", "scripts/document_intelligence_gate.py"])
        gate_results["document_intelligence"] = {
            "status": status,
            "detail": detail or "See sanitized document-intelligence.json evidence",
        }
    if "web_e2e" in selected:
        status, detail = run_gate([".venv/bin/python", "scripts/web_e2e_gate.py"])
        gate_results["web_e2e"] = {
            "status": status,
            "detail": detail or "See sanitized web-e2e.json evidence",
        }
    if "privacy_egress" in selected:
        status, detail = run_gate([".venv/bin/python", "scripts/privacy_egress_gate.py"])
        gate_results["privacy_egress"] = {
            "status": status,
            "detail": detail or "See sanitized privacy-egress.json evidence",
        }
    if "browser_e2e" in selected:
        status, detail = run_gate([".venv/bin/python", "scripts/browser_e2e_gate.py"])
        gate_results["browser_e2e"] = {
            "status": status,
            "detail": detail or "See sanitized browser-e2e.json evidence",
        }
    ordered = [gate for gate in GATES if gate in selected and gate not in ORCHESTRATOR_GATES]
    ordered += [gate for gate in GATES if gate in selected and gate in ORCHESTRATOR_GATES]
    for gate in ordered:
        if gate == "security":
            # All other evidence of this run is on disk by now.
            (OUT / "security.json").write_text(
                json.dumps(blocked_placeholder("security", commit), indent=2), encoding="utf-8"
            )
            status, detail = run_gate([".venv/bin/python", "scripts/security_gate.py"])
            gate_results["security"] = {
                "status": status,
                "detail": detail or "See sanitized security.json evidence",
            }
        gate_result = gate_results.get(gate, {"status": "BLOCKED", "reason": reason})
        path = evidence_path(gate)
        if gate in DETAILED_EVIDENCE_GATES:
            persisted = read_evidence(path)
            if not isinstance(persisted, dict):
                persisted = {}
            preserved = merge_detailed_gate_evidence(
                persisted, gate_result, commit, datetime.now(UTC).isoformat(), gate
            )
            path.write_text(json.dumps(preserved, indent=2), encoding="utf-8")
        else:
            gate_result = {
                "gate": gate,
                **gate_result,
                "tested_commit": commit,
                "timestamp": datetime.now(UTC).isoformat(),
            }
            path.write_text(json.dumps(gate_result, indent=2), encoding="utf-8")
    commit_time = None
    shown = _git("show", "-s", "--format=%cI", "HEAD")
    if shown is not None and shown.returncode == 0:
        try:
            commit_time = datetime.fromisoformat(shown.stdout.strip())
        except ValueError:
            commit_time = None
    verdicts = release_verdicts(
        commit,
        commit_time,
        datetime.now(UTC),
        {gate: result["status"] for gate, result in gate_results.items() if gate in selected},
    )
    statuses = {gate: verdict.status for gate, verdict in verdicts.items()}
    (OUT / "compose-validation.txt").write_text(reason + "\n", encoding="utf-8")
    (OUT / "container-health.json").write_text(
        json.dumps({"status": "BLOCKED", "reason": reason}, indent=2), encoding="utf-8"
    )
    (OUT / "rto-rpo.json").write_text(
        json.dumps(
            {"status": "BLOCKED", "rto": "NOT_MEASURED", "rpo": "design target only"}, indent=2
        ),
        encoding="utf-8",
    )
    (OUT / "performance-sanity.json").write_text(
        json.dumps({"status": "BLOCKED", "reason": reason}, indent=2), encoding="utf-8"
    )
    (OUT / "secret-scan.json").write_text(
        json.dumps({"status": "BLOCKED", "reason": reason}, indent=2), encoding="utf-8"
    )
    overall, current_reason = aggregate_statuses(statuses)
    result: dict[str, object] = {
        "commit": commit,
        "version": "0.2.0.dev0",
        "overall": overall,
        "mandatory_gates": statuses,
        "evidence_validation": {gate: verdict.as_dict() for gate, verdict in verdicts.items()},
        "reason": current_reason,
        "timestamp": datetime.now(UTC).isoformat(),
    }
    (OUT / "final-release-gate.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    (OUT / "rc-live-report.md").write_text(
        "# Phase 10B\n\nOverall: **BLOCKED**. Execute this runner on the authorized integration host described in the briefing.\n",
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
