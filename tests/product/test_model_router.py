from __future__ import annotations

import pytest

from ragops.modeling.router import (
    ComplexitySignals,
    FallbackPolicyError,
    LLMModelRouter,
    ModelSpec,
    RoutingClass,
    TenantModelPolicy,
)


def catalog() -> list[ModelSpec]:
    return [
        ModelSpec(
            "deterministic",
            "economy",
            "Demo Economy",
            routing_tier=RoutingClass.SIMPLE,
            input_cost_per_token=0.000001,
            output_cost_per_token=0.000002,
        ),
        ModelSpec(
            "openai",
            "balanced",
            "Configured Balanced",
            routing_tier=RoutingClass.STANDARD,
            supports_structured_output=True,
            input_cost_per_token=0.00001,
            output_cost_per_token=0.00002,
        ),
        ModelSpec(
            "openai",
            "strong",
            "Configured Strong",
            routing_tier=RoutingClass.COMPLEX,
            supports_structured_output=True,
            high_risk_allowed=True,
            input_cost_per_token=0.00003,
            output_cost_per_token=0.00004,
        ),
    ]


def test_deterministic_cost_aware_routing_and_reasons() -> None:
    router = LLMModelRouter(catalog())
    simple = router.route(
        "tenant-a", ComplexitySignals(estimated_input_tokens=100, expected_output_tokens=100)
    )
    assert simple.routing_class == RoutingClass.SIMPLE and simple.model_id == "economy"
    complex_ = router.route(
        "tenant-a", ComplexitySignals(source_count=6, estimated_input_tokens=100)
    )
    assert complex_.routing_class == RoutingClass.COMPLEX and complex_.model_id == "strong"
    assert "MULTI_DOCUMENT_SYNTHESIS" in complex_.reason_codes


def test_capabilities_and_high_risk_policy_override_price() -> None:
    router = LLMModelRouter(
        catalog(),
        {"tenant-a": TenantModelPolicy("tenant-a", high_risk_models=frozenset({"strong"}))},
    )
    decision = router.route(
        "tenant-a", ComplexitySignals(high_risk_flag=True, structured_output_required=True)
    )
    assert decision.routing_class == RoutingClass.HIGH_RISK and decision.model_id == "strong"
    with pytest.raises(FallbackPolicyError):
        LLMModelRouter(
            [catalog()[0]],
            {"tenant-a": TenantModelPolicy("tenant-a", max_routing_tier=RoutingClass.COMPLEX)},
        ).route("tenant-a", ComplexitySignals(tool_required=True))


def test_tenant_policy_blocks_client_model_and_fallback_is_bounded() -> None:
    policy = TenantModelPolicy(
        "tenant-a", allowed_providers=frozenset({"openai"}), allowed_models=frozenset({"balanced"})
    )
    decision = LLMModelRouter(catalog(), {"tenant-a": policy}).route(
        "tenant-a",
        ComplexitySignals(),
        requested_provider="deterministic",
        requested_model="economy",
    )
    assert decision.provider_id == "openai" and "CLIENT_SELECTION_IGNORED" in decision.reason_codes
    assert decision.fallback_chain == ()


def test_invalid_catalog_is_rejected() -> None:
    with pytest.raises(ValueError):
        LLMModelRouter([ModelSpec("p", "m", "m", input_cost_per_token=-1)])
