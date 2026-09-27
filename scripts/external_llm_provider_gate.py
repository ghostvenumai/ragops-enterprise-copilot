"""Live gate for a real configured external LLM provider.

This gate never substitutes the deterministic provider.  It returns BLOCKED when
credentials are absent and PASS only after a real provider request succeeds.
"""
# ruff: noqa: S603

from __future__ import annotations

import json
import os
import time
from datetime import UTC, datetime
from pathlib import Path

from ragops.llm.providers import provider_from_env
from ragops.modeling.router import ProviderError

ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = ROOT / "evidence" / "product-v1" / "rc-live" / "external-llm-provider.json"


def _write(payload: dict[str, object]) -> None:
    EVIDENCE.parent.mkdir(parents=True, exist_ok=True)
    EVIDENCE.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _required_missing(provider: str) -> list[str]:
    names = {
        "openai": ("OPENAI_API_KEY", "OPENAI_MODEL"),
        "azure_openai": (
            "AZURE_OPENAI_API_KEY",
            "AZURE_OPENAI_ENDPOINT",
            "AZURE_OPENAI_DEPLOYMENT",
            "AZURE_OPENAI_API_VERSION",
        ),
    }
    return [name for name in names.get(provider, ()) if not os.getenv(name)]


def _placeholder(value: str | None, markers: tuple[str, ...]) -> bool:
    return not value or value.strip() in markers


def _base_evidence(provider: str, model: str | None, opt_in: bool) -> dict[str, object]:
    key_name = "AZURE_OPENAI_API_KEY" if provider == "azure_openai" else "OPENAI_API_KEY"
    key_value = os.getenv(key_name)
    return {
        "gate": "external_llm_provider",
        "status": "BLOCKED",
        "provider": provider or "UNCONFIGURED",
        "model": model or "UNCONFIGURED",
        "paid_integration_opt_in": opt_in,
        "api_key_configured": bool(
            key_value and not _placeholder(key_value, ("REPLACE_WITH_REAL_OPENAI_API_KEY",))
        ),
        "live_request_status": "NOT_EXECUTED",
        "request_count": 0,
        "provider_response_valid": False,
        "integration_test_exit_code": 2,
        "timestamp": datetime.now(UTC).isoformat(),
    }


def main() -> int:
    provider_name = os.getenv("RAGOPS_LLM_PROVIDER", "").lower()
    model = os.getenv("OPENAI_MODEL") if provider_name == "openai" else os.getenv(
        "AZURE_OPENAI_DEPLOYMENT"
    )
    opt_in = os.getenv("RAGOPS_ALLOW_PAID_INTEGRATION_TESTS") == "1"
    base = _base_evidence(provider_name, model, opt_in)
    if not opt_in:
        _write(
            {
                **base,
                "reason": (
                    "Paid external integration requires "
                    "RAGOPS_ALLOW_PAID_INTEGRATION_TESTS=1"
                ),
            }
        )
        return 2
    if provider_name not in {"openai", "azure_openai"}:
        _write({**base, "reason": "A real external provider is required"})
        return 2
    missing = _required_missing(provider_name)
    if provider_name == "openai":
        if _placeholder(os.getenv("OPENAI_API_KEY"), ("REPLACE_WITH_REAL_OPENAI_API_KEY",)):
            if "OPENAI_API_KEY" not in missing:
                missing.append("OPENAI_API_KEY")
        if _placeholder(os.getenv("OPENAI_MODEL"), ("REPLACE_WITH_OPENAI_MODEL",)):
            if "OPENAI_MODEL" not in missing:
                missing.append("OPENAI_MODEL")
    if missing:
        _write(
            {
                **base,
                "status": "BLOCKED",
                "reason": "Required external provider configuration is missing",
                "missing": missing,
            }
        )
        return 2
    # The connectivity probe is deliberately tiny and the SDK is configured with
    # zero retries, so one gate run produces at most one billable request.
    os.environ["RAGOPS_LLM_MAX_OUTPUT_TOKENS"] = "16"
    try:
        provider = provider_from_env()
    except Exception as exc:  # client construction fails before any provider request
        _write(
            {
                **base,
                "status": "FAIL",
                "reason": "External provider configuration failed before any request",
                "error_type": type(exc).__name__,
                "integration_test_exit_code": 1,
            }
        )
        return 1
    started = time.perf_counter()
    try:
        response = provider.generate(
            "Return a short confirmation that the live external provider is reachable.",
            [],
            "Synthetic connectivity probe; do not invent business facts.",
        )
        if not response.text.strip():
            raise RuntimeError("provider returned an empty response")
    except ProviderError as exc:
        _write(
            {
                **base,
                "status": "FAIL",
                "reason": "External provider request failed",
                "error_category": str(exc.category),
                **exc.diagnostic_fields(sensitive_values=(
                    os.getenv("OPENAI_API_KEY", ""), os.getenv("AZURE_OPENAI_API_KEY", ""),
                )),
                "latency_ms": round((time.perf_counter() - started) * 1000, 3),
                "live_request_status": "EXECUTED",
                "request_count": 1,
                "integration_test_exit_code": 1,
            }
        )
        return 1
    except Exception as exc:  # configuration and SDK errors are safe to summarize
        _write(
            {
                **base,
                "status": "FAIL",
                "reason": "External provider request failed",
                "error_type": type(exc).__name__,
                "live_request_status": "EXECUTED",
                "request_count": 1,
                "integration_test_exit_code": 1,
            }
        )
        return 1
    _write(
        {
            **base,
            "status": "PASS",
            "model": response.usage.model,
            "latency_ms": round((time.perf_counter() - started) * 1000, 3),
            "paid_integration_opt_in": True,
            "live_request_status": "EXECUTED",
            "request_count": 1,
            "input_tokens": response.usage.prompt_tokens,
            "output_tokens": response.usage.completion_tokens,
            "total_tokens": response.usage.total_tokens,
            "provider_response_valid": True,
            "integration_test_exit_code": 0,
        }
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
