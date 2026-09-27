"""Phase 10B live-gate runner; records BLOCKED when prerequisites are absent."""
# ruff: noqa: E501, S603, S607

from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
from datetime import UTC, datetime
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
    }
)


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
    """Update only selected gates while preserving the prior release-gate state."""
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
        status, detail = run_gate([".venv/bin/python", "scripts/oidc_integration_check.py"])
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
        redis_evidence_path = OUT / "redis-worker.json"
        redis_evidence_path.write_text(
            json.dumps(
                {
                    "gate": "redis_worker",
                    "status": "BLOCKED",
                    "reason": "Redis/worker gate did not produce a result",
                    "tested_commit": commit,
                    "timestamp": datetime.now(UTC).isoformat(),
                },
                indent=2,
            ),
            encoding="utf-8",
        )
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
        status, detail = run_gate(
            [".venv/bin/python", "scripts/external_llm_provider_gate.py"]
        )
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
    previous: dict[str, object] = {}
    try:
        previous = json.loads((OUT / "final-release-gate.json").read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        previous = {}
    prior_statuses = previous.get("mandatory_gates", {})
    prior: dict[str, str] = (
        {str(key): str(value) for key, value in prior_statuses.items()}
        if isinstance(prior_statuses, dict)
        else {}
    )
    gate_status_results: dict[str, dict[str, str]] = {}
    for gate in selected.intersection(GATES):
        gate_result = gate_results.get(gate, {"status": "BLOCKED", "reason": reason})
        evidence_path = OUT / f"{gate.replace('_', '-')}.json"
        if gate in DETAILED_EVIDENCE_GATES:
            try:
                persisted = json.loads(evidence_path.read_text(encoding="utf-8"))
            except (FileNotFoundError, json.JSONDecodeError):
                persisted = {}
            if not isinstance(persisted, dict):
                persisted = {}
            preserved = merge_detailed_gate_evidence(
                persisted, gate_result, commit, datetime.now(UTC).isoformat(), gate
            )
            evidence_path.write_text(json.dumps(preserved, indent=2), encoding="utf-8")
            gate_status_results[gate] = {"status": str(preserved["status"])}
        else:
            gate_result = {
                **gate_result,
                "tested_commit": commit,
                "timestamp": datetime.now(UTC).isoformat(),
            }
            evidence_path.write_text(json.dumps(gate_result, indent=2), encoding="utf-8")
            gate_status_results[gate] = gate_result
    statuses = merge_gate_statuses(prior, selected, gate_status_results)
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
        "reason": current_reason,
    }
    (OUT / "final-release-gate.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    (OUT / "rc-live-report.md").write_text(
        "# Phase 10B\n\nOverall: **BLOCKED**. Execute this runner on the authorized integration host described in the briefing.\n",
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
