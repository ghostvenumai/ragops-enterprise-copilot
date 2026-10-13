"""query_idempotency gate: classification, evidence contract, redaction, cleanup and RC state."""

from __future__ import annotations

import json
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from scripts import query_idempotency_gate as gate
from scripts import rc_live_gate, security_gate

RAW_KEY = "gate-key-" + "7" * 16
HMAC_KEY = "gate-test-hmac-" + "secret-0123456789-abcdefgh"
QUESTION = "Welche Freigaberegel gilt im Projekt ORBIT?"


def passing() -> gate.IdempotencyResult:
    result = gate.IdempotencyResult(checks=dict.fromkeys(gate.REQUIRED, True))
    result.replay_provider_calls = 1
    result.replay_usage_records = 1
    result.replay_reservations = 1
    result.concurrent_request_count = gate.CONCURRENT_REQUESTS
    result.concurrent_provider_calls = 1
    result.concurrent_usage_records = 1
    result.concurrent_reservations = 1
    result.accounting_failure_provider_calls = 1
    result.ambiguous_provider_calls = 1
    result.retained_ambiguous_records = 2
    return result


def test_complete_evidence_passes_and_states_the_contract() -> None:
    assert gate.classify(passing())[0] == "PASS"
    evidence = gate.build_evidence(passing())
    expected = {
        "gate": "query_idempotency",
        "durable_store_used": True,
        "idempotency_scope": "tenant+principal+endpoint+key_hash",
        "raw_keys_stored": False,
        "keyed_digest_used": True,
        "key_digest_algorithm": "HMAC-SHA256",
        "request_fingerprint_algorithm": "sha256/canonical-json/v1",
        "successful_replay_verified": True,
        "replay_provider_calls": 1,
        "replay_usage_records": 1,
        "replay_reservations": 1,
        "original_logical_correlation_id_preserved": True,
        "concurrent_request_count": 10,
        "concurrent_provider_calls": 1,
        "concurrent_usage_records": 1,
        "concurrent_reservations": 1,
        "duplicate_idempotency_records": 0,
        "conflict_verified": True,
        "conflict_provider_calls": 0,
        "cross_tenant_same_key_verified": True,
        "idempotency_tenant_leakage": 0,
        "idempotency_user_leakage": 0,
        "response_tenant_leakage": 0,
        "usage_tenant_leakage": 0,
        "budget_tenant_leakage": 0,
        "zero_result_replay_verified": True,
        "zero_result_provider_calls": 0,
        "accounting_failure_retry_blocked": True,
        "accounting_failure_provider_calls": 1,
        "ambiguous_provider_retry_blocked": True,
        "ambiguous_provider_calls": 1,
        "idempotency_db_failure_fail_closed": True,
        "retained_ambiguous_records": 2,
        "paid_provider_calls": 0,
        "cleanup_status": "PASS",
        "unrelated_data_unchanged": True,
    }
    assert {key: evidence[key] for key in expected} == expected


@pytest.mark.parametrize("check", sorted(gate.REQUIRED))
def test_a_failed_or_missing_check_fails(check) -> None:
    failed = passing()
    failed.checks[check] = False
    assert gate.classify(failed) == ("FAIL", f"query_idempotency invariant failed: {check}")
    missing = passing()
    del missing.checks[check]
    assert gate.classify(missing)[0] == "FAIL" and check in gate.classify(missing)[1]


@pytest.mark.parametrize(
    "counter",
    [
        "idempotency_tenant_leakage",
        "idempotency_user_leakage",
        "response_tenant_leakage",
        "usage_tenant_leakage",
        "budget_tenant_leakage",
        "duplicate_idempotency_records",
        "conflict_provider_calls",
        "zero_result_provider_calls",
        "zero_result_usage_records",
        "zero_result_reservations",
        "db_failure_provider_calls",
        "paid_provider_calls",
    ],
)
def test_any_leakage_or_forbidden_side_effect_fails(counter) -> None:
    result = passing()
    setattr(result, counter, 1)
    status, reason = gate.classify(result)
    assert status == "FAIL" and counter in reason


@pytest.mark.parametrize(
    "counter",
    [
        "replay_provider_calls",
        "replay_usage_records",
        "replay_reservations",
        "accounting_failure_provider_calls",
        "ambiguous_provider_calls",
    ],
)
@pytest.mark.parametrize("value", [0, 2])
def test_exactly_one_side_effect_is_required(counter, value) -> None:
    result = passing()
    setattr(result, counter, value)
    status, reason = gate.classify(result)
    assert status == "FAIL" and counter in reason


@pytest.mark.parametrize(
    "counter", ["concurrent_provider_calls", "concurrent_usage_records", "concurrent_reservations"]
)
def test_a_duplicate_concurrent_side_effect_fails(counter) -> None:
    result = passing()
    setattr(result, counter, 2)
    status, reason = gate.classify(result)
    assert status == "FAIL" and counter in reason


def test_too_few_concurrent_requests_are_no_proof() -> None:
    result = passing()
    result.concurrent_request_count = 1
    assert gate.classify(result)[0] == "FAIL"


def test_a_stored_raw_key_fails() -> None:
    result = passing()
    result.raw_keys_stored = True
    assert gate.classify(result) == ("FAIL", "a raw idempotency key was stored")
    assert gate.build_evidence(result)["raw_keys_stored"] is True


def test_a_cleanup_failure_is_a_gate_failure() -> None:
    for check in gate.CLEANUP_CHECKS:
        result = passing()
        result.checks[check] = False
        assert gate.classify(result)[0] == "FAIL"
        assert gate.build_evidence(result)["cleanup_status"] == "FAIL"
    result = passing()
    result.checks["unrelated_databases_unchanged"] = False
    assert gate.build_evidence(result)["unrelated_data_unchanged"] is False


def test_missing_prerequisites_are_blocked(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(gate, "EVIDENCE", tmp_path / "query-idempotency.json")

    def unavailable(*args, **kwargs):
        raise gate.PrerequisiteMissing("RAGOPS_TEST_DATABASE_URL is not configured")

    monkeypatch.setattr(gate, "run", unavailable)
    assert gate.main() == 2
    evidence = json.loads((tmp_path / "query-idempotency.json").read_text())
    assert evidence["status"] == "BLOCKED" and evidence["gate"] == "query_idempotency"


@pytest.mark.parametrize(
    "leak",
    [
        RAW_KEY,
        QUESTION,
        HMAC_KEY,
        "Bearer abcdefghijklmnopqrstuvwxyz0123456789",
        "postgresql+psycopg://rc:SyntheticPw123@127.0.0.1:5432/ragops_test_idem",
        "-----BEGIN RSA " + "PRIVATE KEY-----",
    ],
    ids=["raw-key", "question", "hmac-secret", "bearer", "database-url", "private-key"],
)
def test_evidence_with_a_secret_key_or_text_is_replaced_by_a_failure(
    monkeypatch, tmp_path, leak
) -> None:
    monkeypatch.setattr(gate, "EVIDENCE", tmp_path / "query-idempotency.json")
    evidence = {"gate": "query_idempotency", "tested_commit": "abc", "detail": f"x {leak} y"}
    assert gate._finish(evidence, "PASS", "ok", 0, secrets=(RAW_KEY, QUESTION, HMAC_KEY)) == 1
    raw = (tmp_path / "query-idempotency.json").read_text()
    written = json.loads(raw)
    assert written["status"] == "FAIL" and written["secret_scan_passed"] is False
    assert leak not in raw and "detail" not in written


def test_rc_runner_runs_query_idempotency_before_reconciliation_and_security() -> None:
    assert len(rc_live_gate.GATES) == 21
    assert rc_live_gate.GATES[-6:] == (
        "query_idempotency",
        "reservation_reconciliation",
        "ocr_ingestion",
        "document_intelligence",
        "web_e2e",
        "security",
    )
    assert "query_idempotency" in rc_live_gate.DETAILED_EVIDENCE_GATES
    prior = {name: "PASS" for name in rc_live_gate.GATES if name != "query_idempotency"}
    merged = rc_live_gate.merge_gate_statuses(
        prior, {"query_idempotency"}, {"query_idempotency": {"status": "PASS"}}
    )
    assert all(value == "PASS" for value in merged.values()) and len(merged) == 21
    assert rc_live_gate.merge_gate_statuses(prior, set(), {})["query_idempotency"] == "BLOCKED"


def test_security_validates_idempotency_evidence() -> None:
    assert len(security_gate.CONTROLS) == 32
    live = [control for control in security_gate.CONTROLS if control.kind == "live"]
    assert len(live) == 15
    (control,) = [c for c in security_gate.CONTROLS if c.evidence_file == "query-idempotency.json"]
    assert control.id == "LIVE-11"
    required = dict(control.required)
    for key in (
        "duplicate_idempotency_records",
        "idempotency_tenant_leakage",
        "idempotency_user_leakage",
        "response_tenant_leakage",
        "paid_provider_calls",
    ):
        assert required[key] == 0
    for key in (
        "durable_store_used",
        "successful_replay_verified",
        "conflict_verified",
        "accounting_failure_retry_blocked",
        "ambiguous_provider_retry_blocked",
        "secret_scan_passed",
    ):
        assert required[key] is True
    assert required["raw_keys_stored"] is False and required["keyed_digest_used"] is True
    assert required["replay_provider_calls"] == required["replay_usage_records"] == 1
    assert required["status"] == "PASS" and required["cleanup_status"] == "PASS"
    assert dict(control.maximums) == {
        "concurrent_provider_calls": 1,
        "concurrent_usage_records": 1,
        "concurrent_reservations": 1,
    }
    assert dict(control.minimums)["concurrent_request_count"] == gate.CONCURRENT_REQUESTS


# --------------------------------------------------------------------------- failure cleanup


@pytest.mark.parametrize("stage", gate.STAGES)
def test_a_failing_stage_still_cleans_up_and_fails(monkeypatch, tmp_path, stage) -> None:
    monkeypatch.setattr(gate, "EVIDENCE", tmp_path / "query-idempotency.json")
    exits: list[str] = []

    @contextmanager
    def fake_environment(result, evidence):
        try:
            yield SimpleNamespace(secrets=[RAW_KEY, HMAC_KEY])
        finally:
            exits.append("cleaned")
            for name in gate.CLEANUP_CHECKS + gate.UNRELATED_CHECKS:
                result.check(name, True)

    monkeypatch.setattr(gate, "environment", fake_environment)
    for name in gate.STAGES:
        monkeypatch.setattr(gate, name, lambda env, result: None)

    def broken(env, result):
        raise RuntimeError(f"injected failure in {stage}")

    monkeypatch.setattr(gate, stage, broken)
    assert gate.main() == 1
    assert exits == ["cleaned"]
    evidence = json.loads((tmp_path / "query-idempotency.json").read_text())
    assert evidence["status"] == "FAIL" and evidence["cleanup_status"] == "PASS"
    assert evidence["errors"] and evidence["errors"][0].startswith(f"{stage}:")
    assert RAW_KEY not in json.dumps(evidence) and HMAC_KEY not in json.dumps(evidence)


def test_keyed_digest_is_a_required_invariant() -> None:
    assert "keyed_digest_used" in gate.REQUIRED
    result = passing()
    result.checks["keyed_digest_used"] = False
    assert gate.classify(result) == (
        "FAIL",
        "query_idempotency invariant failed: keyed_digest_used",
    )
    assert gate.build_evidence(result)["keyed_digest_used"] is False
