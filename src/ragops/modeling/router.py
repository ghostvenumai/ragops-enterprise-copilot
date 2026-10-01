"""Deterministic, policy-constrained model routing and fallback decisions."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol, TypeVar

from ragops.modeling.diagnostics import sanitize_provider_diagnostic

T = TypeVar("T")


class RoutingClass(StrEnum):
    SIMPLE = "SIMPLE"
    STANDARD = "STANDARD"
    COMPLEX = "COMPLEX"
    HIGH_RISK = "HIGH_RISK"


class ProviderErrorCategory(StrEnum):
    TIMEOUT = "TIMEOUT"
    RATE_LIMIT = "RATE_LIMIT"
    AUTHENTICATION = "AUTHENTICATION"
    MODEL_UNAVAILABLE = "MODEL_UNAVAILABLE"
    CONTEXT_LIMIT = "CONTEXT_LIMIT"
    INVALID_REQUEST = "INVALID_REQUEST"
    PROVIDER_ERROR = "PROVIDER_ERROR"
    POLICY_DENIED = "POLICY_DENIED"


class ProviderError(RuntimeError):
    def __init__(
        self, category: ProviderErrorCategory, message: str = "provider request failed",
        *,
        provider_http_status: int | None = None,
        provider_exception_class: str | None = None,
        provider_error_type: str | None = None,
        provider_error_code: str | None = None,
        provider_error_param: str | None = None,
        sanitized_provider_message: str | None = None,
        provider_response_status: str | None = None,
        provider_incomplete_reason: str | None = None,
    ) -> None:
        super().__init__(message)
        self.category = category
        self.provider_http_status = (
            provider_http_status
            if type(provider_http_status) is int and 100 <= provider_http_status <= 599
            else None
        )
        self.provider_exception_class = sanitize_provider_diagnostic(provider_exception_class)
        self.provider_error_type = sanitize_provider_diagnostic(provider_error_type)
        self.provider_error_code = sanitize_provider_diagnostic(provider_error_code)
        self.provider_error_param = sanitize_provider_diagnostic(provider_error_param)
        self.sanitized_provider_message = sanitize_provider_diagnostic(
            sanitized_provider_message if sanitized_provider_message is not None else message
        )
        # Responses API status/reason for a request that returned HTTP 200 without text.
        self.provider_response_status = sanitize_provider_diagnostic(provider_response_status)
        self.provider_incomplete_reason = sanitize_provider_diagnostic(provider_incomplete_reason)

    def diagnostic_fields(self, *, sensitive_values: tuple[str, ...] = ()) -> dict[str, object]:
        """Allowlisted, re-sanitized fields for evidence; no exception/request dump."""
        fields: dict[str, object] = {}
        if self.provider_http_status is not None:
            fields["provider_http_status"] = self.provider_http_status
        for name in (
            "provider_exception_class", "provider_error_type", "provider_error_code",
            "provider_error_param", "sanitized_provider_message",
            "provider_response_status", "provider_incomplete_reason",
        ):
            value = sanitize_provider_diagnostic(
                getattr(self, name), sensitive_values=sensitive_values
            )
            if value is not None:
                fields[name] = value
        return fields


@dataclass(frozen=True)
class ComplexitySignals:
    retrieved_chunks: int = 0
    source_count: int = 0
    contradictions: int = 0
    intent: str = "factual"
    structured_output_required: bool = False
    high_risk_flag: bool = False
    tool_required: bool = False
    estimated_input_tokens: int = 0
    expected_output_tokens: int = 500


@dataclass(frozen=True)
class ModelSpec:
    provider_id: str
    model_id: str
    display_name: str
    enabled: bool = True
    routing_tier: RoutingClass = RoutingClass.STANDARD
    context_window: int = 8192
    supports_tools: bool = False
    supports_structured_output: bool = False
    input_cost_per_token: float = 0.0
    output_cost_per_token: float = 0.0
    latency_class: str = "balanced"
    approved_use_cases: frozenset[str] = frozenset()
    high_risk_allowed: bool = False
    fallback_priority: int = 100

    def estimated_cost(self, input_tokens: int, output_tokens: int) -> float:
        return input_tokens * self.input_cost_per_token + output_tokens * self.output_cost_per_token


@dataclass(frozen=True)
class TenantModelPolicy:
    tenant_id: str
    allowed_providers: frozenset[str] = frozenset()
    allowed_models: frozenset[str] = frozenset()
    default_tier: RoutingClass = RoutingClass.STANDARD
    max_routing_tier: RoutingClass = RoutingClass.COMPLEX
    high_risk_models: frozenset[str] = frozenset()
    cost_ceiling_eur: float | None = None
    fallback_enabled: bool = True


@dataclass(frozen=True)
class RoutingDecision:
    routing_class: RoutingClass
    provider_id: str
    model_id: str
    reason_codes: tuple[str, ...]
    estimated_input_tokens: int
    estimated_output_tokens: int
    estimated_cost: float
    fallback_chain: tuple[tuple[str, str], ...] = ()
    policy_version: str = "v1"


def _incomplete(model: ModelSpec) -> bool:
    """A catalog entry without provider or model identity can never be routed to."""
    return not model.provider_id.strip() or not model.model_id.strip()


class ProviderAdapter(Protocol):
    provider_id: str

    def health(self) -> str: ...


class ProviderRegistry:
    def __init__(self) -> None:
        self._providers: dict[str, ProviderAdapter] = {}

    def register(self, provider: ProviderAdapter) -> None:
        if not provider.provider_id:
            raise ValueError("provider id is required")
        self._providers[provider.provider_id] = provider

    def get(self, provider_id: str) -> ProviderAdapter:
        try:
            return self._providers[provider_id]
        except KeyError as exc:
            raise ProviderError(ProviderErrorCategory.MODEL_UNAVAILABLE) from exc

    def health(self, provider_id: str) -> str:
        return self.get(provider_id).health()


class ModelCatalog:
    """Small mutable catalog used by admin wiring and deterministic tests."""

    def __init__(self, models: list[ModelSpec] | None = None) -> None:
        self._models: dict[tuple[str, str], ModelSpec] = {}
        for model in models or []:
            self.upsert(model)

    def upsert(self, model: ModelSpec) -> None:
        if (
            _incomplete(model)
            or model.input_cost_per_token < 0
            or model.output_cost_per_token < 0
            or model.context_window < 1
        ):
            raise ValueError("invalid model configuration")
        self._models[(model.provider_id, model.model_id)] = model

    def list(self) -> tuple[ModelSpec, ...]:
        return tuple(self._models.values())

    def get(self, provider_id: str, model_id: str) -> ModelSpec:
        return self._models[(provider_id, model_id)]


class ModelPolicyService:
    def __init__(self, policies: dict[str, TenantModelPolicy] | None = None) -> None:
        self._policies = policies or {}

    def get(self, tenant_id: str) -> TenantModelPolicy:
        return self._policies.get(tenant_id, TenantModelPolicy(tenant_id))

    def set(self, policy: TenantModelPolicy) -> None:
        self._policies[policy.tenant_id] = policy


class FallbackPolicyError(ValueError):
    pass


# Failures after which the next model in the chain may be tried.
FALLBACK_CATEGORIES = frozenset(
    {
        ProviderErrorCategory.TIMEOUT,
        ProviderErrorCategory.RATE_LIMIT,
        ProviderErrorCategory.MODEL_UNAVAILABLE,
        ProviderErrorCategory.PROVIDER_ERROR,
    }
)


class LLMModelRouter:
    def __init__(
        self,
        models: list[ModelSpec],
        policies: dict[str, TenantModelPolicy] | None = None,
        providers: ProviderRegistry | None = None,
    ) -> None:
        self.models = tuple(models)
        self.policies = policies or {}
        self.providers = providers or ProviderRegistry()
        self._validate_catalog()

    def classify(self, signals: ComplexitySignals) -> tuple[RoutingClass, tuple[str, ...]]:
        if signals.high_risk_flag or signals.intent.lower() in {
            "compliance",
            "security",
            "legal",
            "critical_operations",
        }:
            return RoutingClass.HIGH_RISK, ("HIGH_RISK_POLICY",)
        if (
            signals.contradictions > 0
            or signals.source_count >= 5
            or signals.retrieved_chunks >= 12
        ):
            return RoutingClass.COMPLEX, (
                "CONTRADICTORY_EVIDENCE" if signals.contradictions else "MULTI_DOCUMENT_SYNTHESIS",
            )
        if (
            signals.source_count >= 2
            or signals.retrieved_chunks >= 4
            or signals.structured_output_required
        ):
            return RoutingClass.STANDARD, ("MULTI_DOCUMENT_SYNTHESIS",)
        return RoutingClass.SIMPLE, ("SIMPLE_FACTUAL_QUERY",)

    def route(
        self,
        tenant_id: str,
        signals: ComplexitySignals,
        *,
        requested_provider: str | None = None,
        requested_model: str | None = None,
    ) -> RoutingDecision:
        policy = self.policies.get(tenant_id, TenantModelPolicy(tenant_id))
        routing_class, reasons = self.classify(signals)
        if requested_provider or requested_model:
            reasons = reasons + ("CLIENT_SELECTION_IGNORED",)
        if routing_class == RoutingClass.HIGH_RISK:
            allowed = [
                model
                for model in self.models
                if model.model_id in policy.high_risk_models or model.high_risk_allowed
            ]
        else:
            allowed = list(self.models)
        candidates = [
            model for model in allowed if self._eligible(model, policy, routing_class, signals)
        ]
        if not candidates:
            raise FallbackPolicyError("no approved model satisfies tenant policy and capabilities")
        candidates.sort(
            key=lambda model: (
                model.estimated_cost(
                    signals.estimated_input_tokens, signals.expected_output_tokens
                ),
                model.fallback_priority,
                model.provider_id,
                model.model_id,
            )
        )
        selected = candidates[0]
        fallback = tuple(
            (model.provider_id, model.model_id)
            for model in candidates[1:]
            if policy.fallback_enabled
        )
        return RoutingDecision(
            routing_class,
            selected.provider_id,
            selected.model_id,
            reasons,
            signals.estimated_input_tokens,
            signals.expected_output_tokens,
            selected.estimated_cost(signals.estimated_input_tokens, signals.expected_output_tokens),
            fallback,
        )

    def _eligible(
        self,
        model: ModelSpec,
        policy: TenantModelPolicy,
        routing_class: RoutingClass,
        signals: ComplexitySignals,
    ) -> bool:
        tier_order = {
            RoutingClass.SIMPLE: 0,
            RoutingClass.STANDARD: 1,
            RoutingClass.COMPLEX: 2,
            RoutingClass.HIGH_RISK: 3,
        }
        meets_class = (
            routing_class == RoutingClass.HIGH_RISK
            or tier_order[model.routing_tier] >= tier_order[routing_class]
        )
        return (
            model.enabled
            and (not policy.allowed_providers or model.provider_id in policy.allowed_providers)
            and (not policy.allowed_models or model.model_id in policy.allowed_models)
            and tier_order[model.routing_tier] <= tier_order[policy.max_routing_tier]
            and meets_class
            and model.context_window >= signals.estimated_input_tokens
            and (not signals.tool_required or model.supports_tools)
            and (not signals.structured_output_required or model.supports_structured_output)
            and (
                policy.cost_ceiling_eur is None
                or model.estimated_cost(
                    signals.estimated_input_tokens, signals.expected_output_tokens
                )
                <= policy.cost_ceiling_eur
            )
        )

    def _validate_catalog(self) -> None:
        seen: set[tuple[str, str]] = set()
        for model in self.models:
            key = (model.provider_id, model.model_id)
            if (
                key in seen
                or _incomplete(model)
                or model.input_cost_per_token < 0
                or model.output_cost_per_token < 0
                or model.context_window < 1
            ):
                raise ValueError("invalid or duplicate model catalog entry")
            seen.add(key)

    def execute_with_fallback(
        self,
        decision: RoutingDecision,
        invoke: Callable[[str, str], T],
        *,
        fallback_categories: frozenset[ProviderErrorCategory] = FALLBACK_CATEGORIES,
    ) -> tuple[T, int]:
        """Execute a bounded, policy-filtered fallback chain.

        A caller may narrow ``fallback_categories`` (never widen them beyond the policy
        chain): the chain itself always comes from the routing decision.
        """
        chain = ((decision.provider_id, decision.model_id),) + decision.fallback_chain
        visited: set[tuple[str, str]] = set()
        last_error: ProviderError | None = None
        for provider_id, model_id in chain:
            if (provider_id, model_id) in visited:
                continue
            visited.add((provider_id, model_id))
            try:
                return invoke(provider_id, model_id), len(visited) - 1
            except ProviderError as exc:
                last_error = exc
                if exc.category not in fallback_categories & FALLBACK_CATEGORIES:
                    raise
        if last_error is not None:
            raise last_error
        raise FallbackPolicyError("fallback chain is empty")
