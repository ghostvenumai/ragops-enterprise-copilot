"""Model-router gate contracts: real router decisions, injected defects must fail the gate."""

from __future__ import annotations

import json
import random

import httpx
import pytest
from scripts import model_router_gate as gate
from scripts import rc_live_gate

from ragops.modeling.router import LLMModelRouter, RoutingDecision, TenantModelPolicy

REQUIRED_SCENARIOS = {
    "default_normal_task",
    "low_cost_simple_task",
    "high_capability_task",
    "tenant_a_pinned_model",
    "tenant_b_unaffected_by_a",
    "preferred_provider_unavailable",
    "all_eligible_unavailable",
    "unknown_model_in_policy",
    "disabled_model_not_selected",
    "cost_ceiling_routes_cheaper",
    "policy_denies_fallback",
    "repeated_request_same_decision",
}


def test_real_router_passes_all_invariants_without_provider_calls() -> None:
    result, paid_calls = gate.run()
    evidence = gate.build_evidence(result, paid_calls)
    assert evidence["status"] == "PASS", evidence["checks"]
    assert REQUIRED_SCENARIOS <= {item["scenario"] for item in evidence["scenarios"]}
    assert all(item["passed"] for item in evidence["scenarios"])
    assert (evidence["paid_provider_calls"], evidence["router_tenant_leakage"]) == (0, 0)
    assert evidence["max_fallback_depth_observed"] == 2
    assert evidence["query_path_uses_router"] is False
    assert evidence["persistence_used"] is False
    assert evidence["cross_tenant_catalog_influence"] is False
    assert evidence["catalog_mutation_scope"] == "platform_admin_tenant_only"


def test_cross_tenant_policy_lookup_is_reported_as_leakage(monkeypatch) -> None:
    original = LLMModelRouter.route

    def shared_policy(self, tenant_id, signals, **kwargs):
        policy = next(iter(self.policies.values()), TenantModelPolicy(tenant_id))
        self.policies = {**self.policies, tenant_id: policy}
        return original(self, tenant_id, signals, **kwargs)

    monkeypatch.setattr(LLMModelRouter, "route", shared_policy)
    result, paid_calls = gate.run()
    assert result.router_tenant_leakage > 0
    assert gate.classify(result, paid_calls)[0] == "FAIL"


def test_ignored_enabled_flag_is_reported(monkeypatch) -> None:
    original = LLMModelRouter._eligible

    def ignores_enabled(self, model, policy, routing_class, signals):
        return original(self, gate_enabled(model), policy, routing_class, signals)

    def gate_enabled(model):
        from dataclasses import replace

        return replace(model, enabled=True)

    monkeypatch.setattr(LLMModelRouter, "_eligible", ignores_enabled)
    result, paid_calls = gate.run()
    assert result.checks["disabled_model_excluded"] is False
    assert gate.classify(result, paid_calls)[0] == "FAIL"


def test_fallback_despite_policy_is_reported(monkeypatch) -> None:
    original = LLMModelRouter.route

    def always_fallback(self, tenant_id, signals, **kwargs):
        decision = original(self, tenant_id, signals, **kwargs)
        if decision.fallback_chain:
            return decision
        spare = tuple(
            (m.provider_id, m.model_id)
            for m in self.models
            if m.enabled
            and (m.provider_id, m.model_id) != (decision.provider_id, decision.model_id)
        )
        return RoutingDecision(
            decision.routing_class,
            decision.provider_id,
            decision.model_id,
            decision.reason_codes,
            decision.estimated_input_tokens,
            decision.estimated_output_tokens,
            decision.estimated_cost,
            spare,
        )

    monkeypatch.setattr(LLMModelRouter, "route", always_fallback)
    result, _ = gate.run()
    assert result.checks["policy_denies_fallback"] is False


def test_nondeterministic_selection_is_reported(monkeypatch) -> None:
    original = LLMModelRouter.route
    rng = random.Random(7)  # noqa: S311 - simulates nondeterminism, not cryptography

    def jitter(self, tenant_id, signals, **kwargs):
        decision = original(self, tenant_id, signals, **kwargs)
        if decision.fallback_chain and rng.random() < 0.5:
            provider, model = decision.fallback_chain[0]
            return RoutingDecision(
                decision.routing_class,
                provider,
                model,
                decision.reason_codes,
                decision.estimated_input_tokens,
                decision.estimated_output_tokens,
                decision.estimated_cost,
                decision.fallback_chain[1:],
            )
        return decision

    monkeypatch.setattr(LLMModelRouter, "route", jitter)
    result, paid_calls = gate.run()
    assert gate.classify(result, paid_calls)[0] == "FAIL"


def test_outbound_http_is_counted_and_blocked() -> None:
    with gate.outbound_guard() as attempts:
        with pytest.raises(httpx.ConnectError):
            httpx.Client().get("https://provider.invalid/v1/responses")
    assert attempts == ["provider.invalid"]
    result = gate.GateResult(checks={"all": True})
    assert gate.classify(result, len(attempts)) == (
        "FAIL",
        "outbound provider traffic was attempted",
    )


def test_evidence_contains_only_routing_metadata() -> None:
    result, paid_calls = gate.run()
    serialized = json.dumps(gate.build_evidence(result, paid_calls))
    for forbidden in ("Bearer", "eyJ", "BEGIN", "sk-", "Evidence:", "Question:"):
        assert forbidden not in serialized


def test_main_writes_pass_evidence(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(gate, "EVIDENCE", tmp_path / "model-router.json")
    assert gate.main() == 0
    evidence = json.loads(gate.EVIDENCE.read_text())
    assert (evidence["gate"], evidence["status"], evidence["integration_test_exit_code"]) == (
        "model_router",
        "PASS",
        0,
    )


def test_missing_dependency_is_blocked_and_unexpected_error_fails(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(gate, "EVIDENCE", tmp_path / "model-router.json")

    def missing():
        raise ImportError("synthetic", name="fastapi")

    monkeypatch.setattr(gate, "run", missing)
    assert gate.main() == 2
    assert json.loads(gate.EVIDENCE.read_text())["status"] == "BLOCKED"

    def broken():
        raise RuntimeError("internal detail must not be persisted")

    monkeypatch.setattr(gate, "run", broken)
    assert gate.main() == 1
    raw = gate.EVIDENCE.read_text()
    assert json.loads(raw)["status"] == "FAIL" and "internal detail" not in raw


def test_rc_runner_merges_router_evidence_and_keeps_prior_pass() -> None:
    assert "model_router" in rc_live_gate.DETAILED_EVIDENCE_GATES
    prior = {name: "PASS" for name in rc_live_gate.GATES[:8]}
    merged = rc_live_gate.merge_gate_statuses(
        prior, {"model_router"}, {"model_router": {"status": "PASS"}}
    )
    assert all(merged[name] == "PASS" for name in prior)
    assert (merged["model_router"], merged["finops"]) == ("PASS", "BLOCKED")
