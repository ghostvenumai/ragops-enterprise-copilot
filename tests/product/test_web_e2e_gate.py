"""The web_e2e gate (ENT-12-UI): registry, verdict logic and the LIVE-15 evidence contract."""

from __future__ import annotations

import base64
import json

from scripts import browser_e2e_gate as e2e
from scripts import rc_live_gate, security_gate
from scripts import web_e2e_gate as gate


def passing() -> e2e.GateResult:
    result = e2e.GateResult()
    for name in (
        *gate.REQUIRED,
        "processes_stopped",
        "keycloak_gate_objects_removed",
        "redis_gate_keys_removed",
        "gate_database_removed",
        "unrelated_postgres_data_unchanged",
        "unrelated_redis_data_unchanged",
    ):
        result.check(name, True)
    return result


def test_gate_is_registered_before_security() -> None:
    assert rc_live_gate.GATES[-3:] == ("web_e2e", "privacy_egress", "security")
    assert "web_e2e" in rc_live_gate.DETAILED_EVIDENCE_GATES


def test_live_15_binds_the_bff_boundary_checks() -> None:
    (control,) = [c for c in security_gate.CONTROLS if c.id == "LIVE-15"]
    assert control.kind == "live" and control.evidence_file == "web-e2e.json"
    required = dict(control.required)
    for name in gate.LIVE_15_CHECKS:
        assert required[name] is True, name
    assert required["cross_tenant_leakage"] == 0 and required["paid_provider_calls"] == 0
    assert required["cleanup_status"] == "PASS" and required["secret_scan_passed"] is True
    assert dict(control.minimums)["locales_verified"] == 2


def test_evidence_provides_every_field_live_15_reads() -> None:
    (control,) = [c for c in security_gate.CONTROLS if c.id == "LIVE-15"]
    evidence = gate.build_evidence(passing(), {})
    evidence.update({"secret_scan_passed": True})
    fields = {key for key, _ in control.required} | {key for key, _ in control.minimums}
    assert fields - {"status"} <= set(evidence)
    assert evidence["status"] == "PASS" and evidence["cleanup_status"] == "PASS"
    assert evidence["locales_verified"] == 2


def test_verdict_requires_every_check_and_no_leakage() -> None:
    assert gate.classify(passing())[0] == "PASS"
    missing = passing()
    del missing.checks["accessibility_wcag22aa"]
    assert gate.classify(missing) == ("FAIL", "web e2e invariants failed: accessibility_wcag22aa")
    failed = passing()
    failed.check("no_tokens_in_browser", False)
    assert gate.classify(failed)[0] == "FAIL"
    leaked = passing()
    leaked.tenant_leakage = 1
    assert gate.classify(leaked)[0] == "FAIL"
    paid = passing()
    paid.paid_provider_calls = 1
    assert gate.classify(paid)[0] == "FAIL"


def test_a_failed_check_is_reflected_in_the_live_15_fields() -> None:
    result = passing()
    result.check("csrf_cross_origin_rejected", False)
    evidence = gate.build_evidence(result, {})
    assert evidence["csrf_cross_origin_rejected"] is False and evidence["status"] == "FAIL"


def test_cleanup_fails_when_an_environment_check_is_missing() -> None:
    result = passing()
    del result.checks["gate_database_removed"]
    assert gate.build_evidence(result, {})["cleanup_status"] == "FAIL"


def test_token_pattern_and_evidence_never_carry_credentials() -> None:
    # Built at runtime so no token-shaped literal is committed (the secret scan would flag it).
    part = lambda raw: base64.urlsafe_b64encode(raw).decode().rstrip("=")  # noqa: E731
    shaped = ".".join((part(b'{"alg":"RS256"}'), part(b'{"sub":"synthetic"}'), part(b"signature")))
    assert gate.JWT.search(shaped)
    serialized = json.dumps(gate.build_evidence(passing(), {}))
    assert not gate.JWT.search(serialized) and "password" not in serialized


def test_axe_runs_the_wcag_22_aa_rule_set_in_both_languages() -> None:
    assert {"wcag2a", "wcag2aa", "wcag21aa", "wcag22aa"} <= set(gate.AXE_TAGS)
    evidence = gate.build_evidence(passing(), {})
    assert evidence["locales"] == ["de-DE", "en-US"]
