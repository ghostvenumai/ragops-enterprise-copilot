"""privacy_egress gate: verdict, evidence contract for LIVE-16 and release registration."""

from __future__ import annotations

import json

import pytest
from scripts import privacy_egress_gate as gate
from scripts import rc_live_gate, security_gate


def passing() -> gate.PrivacyResult:
    result = gate.PrivacyResult()
    for name in sorted(gate.REQUIRED):
        result.check(name, True)
    result.denied_cases, result.allowed_external_calls = 11, 2
    return result


def test_a_complete_run_passes() -> None:
    assert gate.classify(passing())[0] == "PASS"


@pytest.mark.parametrize("check", sorted(gate.REQUIRED))
def test_every_required_check_is_mandatory(check) -> None:
    result = passing()
    result.checks[check] = False
    assert gate.classify(result)[0] == "FAIL"
    del result.checks[check]
    assert gate.classify(result)[0] == "FAIL"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("denied_external_calls", 1),
        ("cross_tenant_leakage", 1),
        ("paid_provider_calls", 1),
        ("denied_cases", 9),
        ("allowed_external_calls", 0),
    ],
)
def test_leaks_paid_calls_and_too_little_coverage_fail(field, value) -> None:
    result = passing()
    setattr(result, field, value)
    assert gate.classify(result)[0] == "FAIL"


def test_evidence_carries_every_field_live_16_requires() -> None:
    evidence = gate.build_evidence(passing())
    (control,) = [c for c in security_gate.CONTROLS if c.id == "LIVE-16"]
    for key, _ in (*control.required, *control.minimums):
        assert key in evidence or key == "secret_scan_passed", key
    assert evidence["cleanup_status"] == "PASS" and evidence["paid_provider_calls"] == 0
    required = dict(control.required)
    assert required["denied_external_calls"] == 0 and required["store_false_enforced"] is True
    assert dict(control.minimums) == {"denied_cases": 10, "allowed_external_calls": 1}


def test_secret_leaks_turn_the_evidence_into_a_failure(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(gate, "EVIDENCE", tmp_path / "privacy-egress.json")
    evidence = {"gate": "privacy_egress", "tested_commit": "abc", "detail": gate.QUESTION}
    assert gate._finish(evidence, "PASS", "ok", 0, (gate.QUESTION,)) == 1
    raw = (tmp_path / "privacy-egress.json").read_text()
    assert gate.QUESTION not in raw and json.loads(raw)["secret_scan_passed"] is False


def test_the_gate_is_mandatory_and_runs_before_security() -> None:
    assert len(rc_live_gate.GATES) == 22
    assert rc_live_gate.GATES[-2:] == ("privacy_egress", "security")
    assert "privacy_egress" in rc_live_gate.DETAILED_EVIDENCE_GATES
    assert "src/ragops/privacy" in rc_live_gate.HISTORICAL_EVIDENCE_PATHS["external_llm_provider"]


def test_the_gate_uses_fake_sdk_clients_and_a_gate_owned_database_only() -> None:
    source = (gate.ROOT / "scripts" / "privacy_egress_gate.py").read_text(encoding="utf-8")
    assert "ragops_test_privacy_" in source and "DROP DATABASE IF EXISTS" in source
    assert "RAGOPS_ALLOW_PAID_INTEGRATION_TESTS" not in source
    assert "provider_from_env" not in source and "_no_real_sdk" in source
