"""reservation_reconciliation gate: classification, diagnostics, evidence contract, RC state."""

from __future__ import annotations

import json

import pytest
from scripts import rc_live_gate, security_gate
from scripts import reservation_reconciliation_gate as gate

ALL_CHECKS = sorted(gate.REQUIRED)


def passing() -> gate.ReconciliationResult:
    result = gate.ReconciliationResult()
    for name in ALL_CHECKS:
        result.check(name, True)
    result.concurrent_reconcilers = gate.RECONCILERS
    result.concurrent_usage_records = 1
    result.concurrent_terminal_transitions = 1
    return result


def test_a_complete_run_passes() -> None:
    status, reason = gate.classify(passing())
    assert status == "PASS" and gate.failure_class(passing(), status, reason) is None


@pytest.mark.parametrize(
    ("change", "expected"),
    [
        ({"provider_calls_during_reconciliation": 1}, "PROVIDER_REPLAY_ATTEMPT"),
        ({"pre_provider_provider_calls": 1}, "PROVIDER_REPLAY_ATTEMPT"),
        ({"duplicate_usage_records": 1}, "DUPLICATE_USAGE"),
        ({"concurrent_usage_records": 2}, "DUPLICATE_USAGE"),
        ({"concurrent_terminal_transitions": 2}, "DUPLICATE_SETTLEMENT"),
        ({"cross_tenant_leakage": 1}, "TENANT_SCOPE_FAILURE"),
        ({"ambiguous_released": True}, "UNKNOWN_BILLING"),
        ({"unknown_usage_booked_as_zero": True}, "UNKNOWN_BILLING"),
        ({"legacy_unknown_auto_released": True}, "UNKNOWN_BILLING"),
    ],
)
def test_each_violation_fails_with_a_diagnosis(change, expected) -> None:
    result = passing()
    for name, value in change.items():
        setattr(result, name, value)
    status, reason = gate.classify(result)
    assert status == "FAIL"
    assert gate.failure_class(result, status, reason) == expected


@pytest.mark.parametrize(
    ("check", "expected"),
    [
        ("reconciliation_db_failure_fail_closed", "DB_FAILURE"),
        ("cross_tenant_not_found", "TENANT_SCOPE_FAILURE"),
        ("accounting_failure_recovered", "EVIDENCE_MISMATCH"),
        ("ambiguous_case_held", "UNKNOWN_BILLING"),
        ("same_key_recovery", "INVALID_STATE"),
        ("gate_database_removed", "CLEANUP_FAILURE"),
    ],
)
def test_a_failed_check_names_its_failure_class(check, expected) -> None:
    result = passing()
    result.checks[check] = False
    status, reason = gate.classify(result)
    assert status == "FAIL" and gate.failure_class(result, status, reason) == expected


def test_missing_stages_and_too_few_reconcilers_fail() -> None:
    assert gate.classify(gate.ReconciliationResult())[0] == "FAIL"
    result = passing()
    result.concurrent_reconcilers = gate.RECONCILERS - 1
    assert gate.classify(result)[0] == "FAIL"
    assert gate.failure_class(result, "BLOCKED", "less memory than required") == (
        "RESOURCE_BLOCKED"
    )
    assert set(gate.FAILURE_CLASSES) >= set(gate._CLASS_OF.values()) | {"UNDETERMINED"}


def test_evidence_carries_every_field_the_security_control_needs() -> None:
    evidence = gate.build_evidence(passing())
    (control,) = [c for c in security_gate.CONTROLS if c.id == "LIVE-12"]
    for key, _ in (*control.required, *control.minimums, *control.maximums):
        assert key in evidence or key in ("status", "secret_scan_passed")
    assert evidence["cleanup_status"] == "PASS" and evidence["paid_provider_calls"] == 0


def test_secret_leaks_turn_the_evidence_into_a_failure(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(gate, "EVIDENCE", tmp_path / "reservation-reconciliation.json")
    leak = "postgresql+psycopg://user:secret@localhost/ragops_test_recon_x"
    evidence = {"gate": "reservation_reconciliation", "tested_commit": "abc", "detail": leak}
    assert gate._finish(evidence, "PASS", "ok", 0, secrets=(leak,)) == 1
    raw = (tmp_path / "reservation-reconciliation.json").read_text()
    written = json.loads(raw)
    assert written["status"] == "FAIL" and written["secret_scan_passed"] is False
    assert leak not in raw and "detail" not in written


def test_rc_runner_has_reconciliation_before_ocr_and_security() -> None:
    assert len(rc_live_gate.GATES) == 22
    assert rc_live_gate.GATES[-6:] == (
        "reservation_reconciliation",
        "ocr_ingestion",
        "document_intelligence",
        "web_e2e",
        "privacy_egress",
        "security",
    )
    assert "reservation_reconciliation" in rc_live_gate.DETAILED_EVIDENCE_GATES
    prior = {name: "PASS" for name in rc_live_gate.GATES if name != "reservation_reconciliation"}
    merged = rc_live_gate.merge_gate_statuses(
        prior, {"reservation_reconciliation"}, {"reservation_reconciliation": {"status": "PASS"}}
    )
    assert all(value == "PASS" for value in merged.values()) and len(merged) == 22
    blocked = rc_live_gate.merge_gate_statuses(prior, set(), {})
    assert blocked["reservation_reconciliation"] == "BLOCKED"


def test_security_has_thirty_two_controls_with_live_fifteen() -> None:
    assert len(security_gate.CONTROLS) == 33
    live = [control for control in security_gate.CONTROLS if control.kind == "live"]
    assert len(live) == 16
    (control,) = [c for c in live if c.evidence_file == "reservation-reconciliation.json"]
    assert control.id == "LIVE-12"
    required = dict(control.required)
    assert required["provider_calls_during_reconciliation"] == 0
    assert required["ambiguous_released"] is False
    assert required["unknown_usage_booked_as_zero"] is False
    assert required["cross_tenant_leakage"] == 0 and required["paid_provider_calls"] == 0
    assert dict(control.maximums) == {
        "concurrent_usage_records": 1,
        "concurrent_terminal_transitions": 1,
    }
    assert dict(control.minimums) == {"concurrent_reconcilers": 10}


def test_the_gate_uses_only_the_local_provider_and_a_gate_owned_database() -> None:
    source = (gate.ROOT / "scripts" / "reservation_reconciliation_gate.py").read_text()
    assert "ragops_test_recon_" in source and "DROP DATABASE IF EXISTS" in source
    for paid in ("OpenAIProvider", "AzureOpenAI", "RAGOPS_ALLOW_PAID_INTEGRATION_TESTS"):
        assert paid not in source


def test_the_gate_provider_is_a_local_adapter() -> None:
    from ragops.privacy.egress import is_external

    assert not is_external(gate.GateProvider())
