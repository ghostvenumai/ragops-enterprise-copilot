"""Rate-limiting gate classification, blocking, sanitization and RC aggregation."""

from __future__ import annotations

import json

from scripts import rate_limiting_gate as gate
from scripts import rc_live_gate, tenant_isolation_gate


def passing() -> gate.RateLimitResult:
    result = gate.RateLimitResult(checks=dict.fromkeys(gate.REQUIRED, True))
    result.metrics.update({"max_observed_allowed": gate.CONCURRENCY_LIMIT})
    return result


def test_all_required_checks_pass() -> None:
    assert gate.classify(passing(), 0)[0] == "PASS"


def test_missing_or_failed_check_fails() -> None:
    result = passing()
    del result.checks["query_path_enforced"]
    assert gate.classify(result, 0)[0] == "FAIL"
    result = passing()
    result.checks["redis_outage_verified"] = False
    assert "redis_outage_verified" in gate.classify(result, 0)[1]


def test_leakage_overshoot_and_provider_calls_fail() -> None:
    leaking = passing()
    leaking.rate_limit_tenant_leakage = 1
    assert gate.classify(leaking, 0)[1] == "one tenant, user or route consumed another bucket"
    overshoot = passing()
    overshoot.metrics["max_observed_allowed"] = gate.CONCURRENCY_LIMIT + 1
    assert gate.classify(overshoot, 0)[0] == "FAIL"
    assert gate.classify(passing(), 1)[1] == "outbound provider traffic was attempted"


def test_unavailable_redis_is_blocked(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(gate, "EVIDENCE", tmp_path / "rate-limiting.json")
    monkeypatch.setattr(tenant_isolation_gate, "_redis_endpoint", lambda url, evidence: None)
    assert gate.main() == 2
    evidence = json.loads(gate.EVIDENCE.read_text())
    assert (evidence["status"], evidence["exit_code"], evidence["redis_reachable"]) == (
        "BLOCKED",
        2,
        False,
    )


def test_evidence_is_sanitized_and_states_architecture() -> None:
    evidence = gate.build_evidence(passing(), 0, {"enforced": ["/v1/query"], "exempt": ["/health"]})
    serialized = json.dumps(evidence)
    for forbidden in ("redis://", "Bearer", "eyJ", "password", "tenant-alpha"):
        assert forbidden not in serialized
    assert evidence["query_path_enforced"] is True
    assert evidence["sensitive_key_material_detected"] is False
    assert evidence["window_edge_behavior"].startswith("not sliding")


def test_rc_runner_merges_rate_limit_evidence_and_keeps_prior_pass() -> None:
    assert "rate_limiting" in rc_live_gate.DETAILED_EVIDENCE_GATES
    prior = {name: "PASS" for name in rc_live_gate.GATES[:10]}
    merged = rc_live_gate.merge_gate_statuses(
        prior, {"rate_limiting"}, {"rate_limiting": {"status": "PASS"}}
    )
    assert all(merged[name] == "PASS" for name in prior)
    assert (merged["rate_limiting"], merged["backup_restore"]) == ("PASS", "BLOCKED")
