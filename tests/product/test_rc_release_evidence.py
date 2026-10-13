"""Release aggregation is fail-closed: only fresh evidence of the exact commit counts.

Regression (ENT-12-UI closure): final-release-gate.json reported PASS 21/21 for a new commit
while 20 of the 21 evidence files belonged to the previous commit, because the runner carried
the statuses of the previous summary forward.
"""

from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from scripts import rc_live_gate as rc
from scripts import security_gate

HEAD = "a" * 40
OLD = "b" * 40
COMMIT_TIME = datetime(2026, 10, 14, 8, 0, tzinfo=UTC)
NOW = COMMIT_TIME + timedelta(hours=2)


def evidence(name: str, **changes: object) -> dict[str, object]:
    data: dict[str, object] = {
        "gate": name,
        "status": "PASS",
        "reason": "ok",
        "tested_commit": HEAD,
        "timestamp": (NOW - timedelta(minutes=30)).isoformat(),
    }
    data.update(changes)
    return data


def verdict(data: object, gate: str = "ocr_ingestion", **kwargs: object) -> rc.EvidenceVerdict:
    options: dict[str, object] = {"commit": HEAD, "commit_time": COMMIT_TIME, "now": NOW}
    options.update(kwargs)
    return rc.validate_gate_evidence(gate, data, **options)  # type: ignore[arg-type]


def test_fresh_evidence_of_the_exact_commit_passes_and_failures_stay_failures() -> None:
    assert verdict(evidence("ocr_ingestion")).status == "PASS"
    assert verdict(evidence("ocr_ingestion", status="FAIL")).status == "FAIL"
    assert verdict(evidence("ocr_ingestion", status="BLOCKED")).status == "BLOCKED"


@pytest.mark.parametrize(
    ("data", "expected", "fragment"),
    [
        (rc.MISSING, "BLOCKED", "missing"),
        (None, "FAIL", "malformed"),
        (["PASS"], "FAIL", "malformed"),
        (evidence("ocr_ingestion", tested_commit=OLD), "BLOCKED", "another commit"),
        (evidence("ocr_ingestion", tested_commit="031befe"), "FAIL", "malformed"),
        (evidence("ocr_ingestion", gate="web_e2e"), "FAIL", "another gate"),
        ({k: v for k, v in evidence("ocr_ingestion").items() if k != "gate"}, "FAIL", "no gate"),
        (
            {k: v for k, v in evidence("ocr_ingestion", tested_commit=OLD).items() if k != "gate"},
            "BLOCKED",
            "another commit",
        ),
        (evidence("ocr_ingestion", status="pass"), "FAIL", "malformed"),
        (evidence("ocr_ingestion", status=True), "FAIL", "malformed"),
        (evidence("ocr_ingestion", timestamp="yesterday"), "FAIL", "timestamp"),
        (evidence("ocr_ingestion", timestamp="2026-10-14T09:00:00"), "FAIL", "timestamp"),
        (
            evidence("ocr_ingestion", timestamp=(NOW + timedelta(hours=1)).isoformat()),
            "FAIL",
            "future",
        ),
        (
            evidence("ocr_ingestion", timestamp=(COMMIT_TIME - timedelta(hours=1)).isoformat()),
            "BLOCKED",
            "predates the commit",
        ),
    ],
)
def test_missing_malformed_stale_and_mismatched_evidence_never_passes(
    data, expected, fragment
) -> None:
    result = verdict(data)
    assert result.status == expected and fragment in result.reason


def test_evidence_older_than_the_freshness_window_is_stale() -> None:
    old_commit_time = NOW - timedelta(days=3)
    stale = evidence("ocr_ingestion", timestamp=(NOW - timedelta(hours=25)).isoformat())
    result = verdict(stale, commit_time=old_commit_time)
    assert result.status == "BLOCKED" and "24 hours" in result.reason
    assert verdict(stale, commit_time=None).status == "BLOCKED"


@pytest.mark.parametrize(
    ("exit_status", "status"),
    [("PASS", "FAIL"), ("PASS", "BLOCKED"), ("FAIL", "PASS"), ("BLOCKED", "PASS")],
)
def test_contradicting_exit_status_and_evidence_fail(exit_status, status) -> None:
    result = verdict(evidence("ocr_ingestion", status=status), exit_status=exit_status)
    assert result.status == "FAIL" and "contradiction" in result.reason


def test_a_failed_run_keeps_the_distinction_between_fail_and_blocked() -> None:
    assert verdict(evidence("ocr_ingestion", status="FAIL"), exit_status="FAIL").status == "FAIL"
    blocked = evidence("ocr_ingestion", status="BLOCKED")
    assert verdict(blocked, exit_status="FAIL").status == "BLOCKED"


def test_the_historical_exception_is_limited_to_the_paid_provider_gate() -> None:
    def always(gate: str, tested: str, head: str) -> tuple[bool, str]:
        return True, "unchanged"

    provider = evidence(
        "external_llm_provider",
        tested_commit=OLD,
        timestamp=(NOW - timedelta(days=1)).isoformat(),
        live_request_status="EXECUTED",
        provider_response_valid=True,
        request_count=1,
    )
    accepted = verdict(provider, gate="external_llm_provider", historical=always)
    assert accepted.status == "PASS" and accepted.basis == "historical_exception"
    assert accepted.tested_commit == OLD
    for gate in sorted(set(rc.GATES) - {"external_llm_provider"}):
        stale = evidence(gate, tested_commit=OLD)
        assert verdict(stale, gate=gate, historical=always).status == "BLOCKED", gate
    assert verdict(provider, gate="external_llm_provider").status == "BLOCKED"
    for change in ({"status": "FAIL"}, {"request_count": 0}, {"provider_response_valid": "true"}):
        result = verdict({**provider, **change}, gate="external_llm_provider", historical=always)
        assert result.status in {"BLOCKED", "FAIL"} and result.basis != "historical_exception"
    refused = verdict(
        provider, gate="external_llm_provider", historical=lambda *_: (False, "changed")
    )
    assert refused.status == "BLOCKED" and "changed" in refused.reason


def _git(root: Path, *arguments: str) -> str:
    return subprocess.run(  # noqa: S603
        ["git", *arguments],  # noqa: S607
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _commit(root: Path, path: str, content: str) -> str:
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    _git(root, "add", path)
    _git(root, "-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-qm", path)
    return _git(root, "rev-parse", "HEAD")


def test_historical_check_needs_an_ancestor_with_unchanged_provider_paths(tmp_path) -> None:
    _git(tmp_path, "init", "-q")
    tested = _commit(tmp_path, "src/ragops/llm/providers.py", "v1\n")
    head = _commit(tmp_path, "apps/web/page.tsx", "ui\n")
    ok, _ = rc.historical_evidence_check("external_llm_provider", tested, head, tmp_path)
    assert ok
    assert not rc.historical_evidence_check("ocr_ingestion", tested, head, tmp_path)[0]
    assert not rc.historical_evidence_check("external_llm_provider", head, tested, tmp_path)[0]
    (tmp_path / "src/ragops/llm/providers.py").write_text("dirty\n", encoding="utf-8")
    dirty = rc.historical_evidence_check("external_llm_provider", tested, head, tmp_path)
    assert not dirty[0] and "uncommitted" in dirty[1]
    _git(tmp_path, "checkout", "-q", "--", ".")
    for path in ("src/ragops/llm/providers.py", "constraints.txt", "src/ragops/modeling/r.py"):
        changed = _commit(tmp_path, path, f"changed {path}\n")
        result = rc.historical_evidence_check("external_llm_provider", tested, changed, tmp_path)
        assert not result[0] and "changed" in result[1], path


def test_freshness_rules_match_the_security_gate() -> None:
    assert rc.MAX_EVIDENCE_AGE == security_gate.MAX_EVIDENCE_AGE
    assert rc.CLOCK_SKEW == security_gate.CLOCK_SKEW


# ------------------------------------------------------------------ runner end to end


def _head() -> str:
    return _git(rc.ROOT, "rev-parse", "HEAD")


@pytest.fixture
def runner(tmp_path, monkeypatch):
    monkeypatch.setattr(rc, "OUT", tmp_path)
    monkeypatch.setattr(rc, "version", lambda command: "test")
    for name in ("RAGOPS_IDENTITY_PROVIDER", "RAGOPS_TEST_DATABASE_URL", "KEYCLOAK_ADMIN_PASSWORD"):
        monkeypatch.delenv(name, raising=False)

    def run(gates: str) -> dict[str, object]:
        monkeypatch.setenv("RAGOPS_RC_GATES", gates)
        assert rc.main() == 0
        return json.loads((tmp_path / "final-release-gate.json").read_text(encoding="utf-8"))

    return run


def _write_all(directory: Path, commit: str, timestamp: datetime) -> None:
    for gate in rc.GATES:
        record = {
            "gate": gate,
            "status": "PASS",
            "reason": "ok",
            "tested_commit": commit,
            "timestamp": timestamp.isoformat(),
        }
        if gate == "external_llm_provider":
            record.update(
                live_request_status="EXECUTED", provider_response_valid=True, request_count=1
            )
        rc.evidence_path(gate, directory).write_text(json.dumps(record), encoding="utf-8")


def test_a_previous_pass_summary_with_stale_evidence_cannot_produce_pass(tmp_path, runner) -> None:
    (tmp_path / "final-release-gate.json").write_text(
        json.dumps(
            {"commit": OLD, "overall": "PASS", "mandatory_gates": dict.fromkeys(rc.GATES, "PASS")}
        ),
        encoding="utf-8",
    )
    _write_all(tmp_path, OLD, datetime.now(UTC))
    result = runner("no_gate_selected")
    assert result["commit"] == _head()
    assert result["overall"] == "BLOCKED"
    assert all(status == "BLOCKED" for status in result["mandatory_gates"].values())
    assert "another commit" in result["evidence_validation"]["ocr_ingestion"]["reason"]


def test_missing_evidence_blocks_even_with_a_pass_summary(tmp_path, runner) -> None:
    (tmp_path / "final-release-gate.json").write_text(
        json.dumps({"overall": "PASS", "mandatory_gates": dict.fromkeys(rc.GATES, "PASS")}),
        encoding="utf-8",
    )
    result = runner("no_gate_selected")
    assert result["overall"] == "BLOCKED"


def test_incremental_runs_on_the_same_commit_still_aggregate_to_pass(tmp_path, runner) -> None:
    _write_all(tmp_path, _head(), datetime.now(UTC))
    result = runner("no_gate_selected")
    assert result["overall"] == "PASS", result["evidence_validation"]
    assert len(result["mandatory_gates"]) == len(rc.GATES) == 21


def test_a_selected_gate_never_keeps_its_older_pass(tmp_path, runner, monkeypatch) -> None:
    _write_all(tmp_path, _head(), datetime.now(UTC))
    monkeypatch.setattr(rc, "run_gate", lambda command: ("FAIL", "crashed before evidence"))
    result = runner("ocr_ingestion")
    written = json.loads(rc.evidence_path("ocr_ingestion", tmp_path).read_text(encoding="utf-8"))
    assert (
        written["status"] == "BLOCKED" and result["mandatory_gates"]["ocr_ingestion"] == "BLOCKED"
    )
    assert result["overall"] == "BLOCKED"
    monkeypatch.setattr(rc, "run_gate", lambda command: ("PASS", "claims success, writes nothing"))
    result = runner("ocr_ingestion")
    assert result["mandatory_gates"]["ocr_ingestion"] == "FAIL" and result["overall"] == "FAIL"
