from __future__ import annotations

import pytest

from ragops.modeling.router import (
    ComplexitySignals,
    LLMModelRouter,
    ModelSpec,
    ProviderError,
    ProviderErrorCategory,
    RoutingClass,
)


def _router() -> LLMModelRouter:
    return LLMModelRouter(
        [
            ModelSpec("a", "cheap", "Cheap", input_cost_per_token=0.01, output_cost_per_token=0.01),
            ModelSpec(
                "b", "backup", "Backup", input_cost_per_token=0.02, output_cost_per_token=0.02
            ),
        ]
    )


def test_retryable_failure_uses_bounded_fallback() -> None:
    decision = _router().route("tenant", ComplexitySignals())
    calls: list[tuple[str, str]] = []

    def invoke(provider: str, model: str) -> str:
        calls.append((provider, model))
        if len(calls) == 1:
            raise ProviderError(ProviderErrorCategory.RATE_LIMIT)
        return "ok"

    result, fallback_count = _router().execute_with_fallback(decision, invoke)
    assert result == "ok" and fallback_count == 1 and len(calls) == 2


def test_permanent_provider_error_does_not_fallback() -> None:
    decision = _router().route("tenant", ComplexitySignals())

    def invoke(_: str, __: str) -> str:
        raise ProviderError(ProviderErrorCategory.AUTHENTICATION)

    with pytest.raises(ProviderError) as error:
        _router().execute_with_fallback(decision, invoke)
    assert error.value.category == ProviderErrorCategory.AUTHENTICATION


def test_classification_is_deterministic() -> None:
    router = _router()
    signals = ComplexitySignals(source_count=6, contradictions=1)
    assert router.classify(signals)[0] == RoutingClass.COMPLEX
    assert router.classify(signals) == router.classify(signals)
