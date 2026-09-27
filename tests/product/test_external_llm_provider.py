from __future__ import annotations

from types import SimpleNamespace

import pytest
from scripts import external_llm_provider_gate as live_gate

from ragops.llm.providers import (
    AzureOpenAIProvider,
    OpenAIProvider,
    ProviderError,
    ProviderErrorCategory,
)
from ragops.storage.models import Citation


def response(text: str = "Antwort [DOC-1]") -> SimpleNamespace:
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=text))],
        usage=SimpleNamespace(prompt_tokens=11, completion_tokens=4, total_tokens=15),
    )


class FakeCompletions:
    def __init__(self, result: object) -> None:
        self.result = result
        self.calls: list[dict[str, object]] = []

    def create(self, **kwargs: object) -> object:
        self.calls.append(kwargs)
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result


def fake_client(result: object) -> tuple[SimpleNamespace, FakeCompletions]:
    completions = FakeCompletions(result)
    return SimpleNamespace(chat=SimpleNamespace(completions=completions)), completions


def fake_responses_client(result: object) -> tuple[SimpleNamespace, FakeCompletions]:
    responses = FakeCompletions(result)
    return SimpleNamespace(responses=SimpleNamespace(create=responses.create)), responses


def citation() -> Citation:
    return Citation("DOC-1", "Policy", "tenant-a", "evidence", 1.0)


class ProviderFailure(Exception):
    def __init__(self, status_code: int) -> None:
        super().__init__("provider failure")
        self.status_code = status_code


def test_openai_provider_executes_real_sdk_contract_without_fallback(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("OPENAI_MODEL", "test-model")
    client, calls = fake_responses_client(
        SimpleNamespace(
            output_text="Antwort [DOC-1]",
            usage=SimpleNamespace(input_tokens=11, output_tokens=4, total_tokens=15),
            model="test-model",
        )
    )
    provider = OpenAIProvider(client=client)
    result = provider.generate("question", [citation()], "evidence")
    assert result.text == "Antwort [DOC-1]"
    assert result.usage.total_tokens == 15
    assert result.used_source_ids == ("DOC-1",)
    assert calls.calls[0]["model"] == "test-model"
    assert calls.calls[0]["max_output_tokens"] == 600
    assert calls.calls[0]["input"][0]["role"] == "system"
    assert "Evidence" in calls.calls[0]["input"][1]["content"]


def test_azure_provider_executes_same_contract(monkeypatch) -> None:
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://azure.example")
    monkeypatch.setenv("AZURE_OPENAI_DEPLOYMENT", "deployment")
    monkeypatch.setenv("AZURE_OPENAI_API_VERSION", "2026-01-01")
    client, calls = fake_client(response("Azure response"))
    provider = AzureOpenAIProvider(client=client)
    assert provider.generate("question", [], "evidence").text == "Azure response"
    assert calls.calls[0]["model"] == "deployment"


def test_openai_responses_output_fallback_and_limit(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("OPENAI_MODEL", "test-model")
    monkeypatch.setenv("RAGOPS_LLM_MAX_OUTPUT_TOKENS", "16")
    result = SimpleNamespace(
        output=[SimpleNamespace(content=[SimpleNamespace(text="fallback text")])],
        usage=SimpleNamespace(input_tokens=2, output_tokens=3, total_tokens=5),
        model="test-model",
    )
    client, calls = fake_responses_client(result)
    generated = OpenAIProvider(client=client).generate("question", [], "evidence")
    assert generated.text == "fallback text"
    assert generated.usage.prompt_tokens == 2
    assert calls.calls[0]["max_output_tokens"] == 16


@pytest.mark.parametrize(
    ("exception", "category"),
    [
        (TimeoutError(), ProviderErrorCategory.TIMEOUT),
        (ProviderFailure(429), ProviderErrorCategory.RATE_LIMIT),
        (ProviderFailure(401), ProviderErrorCategory.AUTHENTICATION),
        (ProviderFailure(404), ProviderErrorCategory.MODEL_UNAVAILABLE),
    ],
)
def test_external_errors_are_normalized(monkeypatch, exception: BaseException, category) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("OPENAI_MODEL", "test-model")
    client, _ = fake_responses_client(exception)
    provider = OpenAIProvider(client=client)
    with pytest.raises(ProviderError) as raised:
        provider.generate("question", [], "evidence")
    assert raised.value.category == category


def test_empty_provider_response_fails_closed(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("OPENAI_MODEL", "test-model")
    client, _ = fake_responses_client(
        SimpleNamespace(output_text="", output=[], usage=SimpleNamespace())
    )
    with pytest.raises(ProviderError, match="no text"):
        OpenAIProvider(client=client).generate("question", [], "evidence")


def test_live_gate_blocks_without_external_provider(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("RAGOPS_LLM_PROVIDER", raising=False)
    monkeypatch.delenv("RAGOPS_ALLOW_PAID_INTEGRATION_TESTS", raising=False)
    monkeypatch.setattr(live_gate, "EVIDENCE", tmp_path / "external.json")
    assert live_gate.main() == 2
    evidence = (tmp_path / "external.json").read_text()
    assert '"status": "BLOCKED"' in evidence
    assert '"live_request_status": "NOT_EXECUTED"' in evidence


def test_live_gate_never_accepts_deterministic_provider(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("RAGOPS_LLM_PROVIDER", "deterministic")
    monkeypatch.setenv("RAGOPS_ALLOW_PAID_INTEGRATION_TESTS", "1")
    monkeypatch.setattr(live_gate, "EVIDENCE", tmp_path / "external.json")
    assert live_gate.main() == 2


@pytest.mark.parametrize("opt_in", [None, "0", "yes", "true"])
def test_live_gate_requires_exact_paid_opt_in(monkeypatch, tmp_path, opt_in) -> None:
    monkeypatch.setenv("RAGOPS_LLM_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "never-written")
    monkeypatch.setenv("OPENAI_MODEL", "gpt-test")
    if opt_in is None:
        monkeypatch.delenv("RAGOPS_ALLOW_PAID_INTEGRATION_TESTS", raising=False)
    else:
        monkeypatch.setenv("RAGOPS_ALLOW_PAID_INTEGRATION_TESTS", opt_in)
    calls = 0

    def provider_factory() -> object:
        nonlocal calls
        calls += 1
        raise AssertionError("provider must not be constructed without exact opt-in")

    monkeypatch.setattr(live_gate, "provider_from_env", provider_factory)
    monkeypatch.setattr(live_gate, "EVIDENCE", tmp_path / "external.json")
    assert live_gate.main() == 2
    assert calls == 0


def test_live_gate_blocks_placeholders_without_provider_call(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("RAGOPS_LLM_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "REPLACE_WITH_REAL_OPENAI_API_KEY")
    monkeypatch.setenv("OPENAI_MODEL", "REPLACE_WITH_OPENAI_MODEL")
    monkeypatch.setenv("RAGOPS_ALLOW_PAID_INTEGRATION_TESTS", "1")
    calls = 0

    def provider_factory() -> object:
        nonlocal calls
        calls += 1
        raise AssertionError("provider must not be constructed with placeholders")

    monkeypatch.setattr(live_gate, "provider_from_env", provider_factory)
    monkeypatch.setattr(live_gate, "EVIDENCE", tmp_path / "external.json")
    assert live_gate.main() == 2
    assert calls == 0


def test_live_gate_success_path_is_single_call_and_sanitized(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("RAGOPS_LLM_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "real-secret-never-output")
    monkeypatch.setenv("OPENAI_MODEL", "gpt-5.6-luna")
    monkeypatch.setenv("RAGOPS_ALLOW_PAID_INTEGRATION_TESTS", "1")

    class FakeProvider:
        model = "gpt-5.6-luna"

        def generate(self, question, citations, context):
            assert question.startswith("Return a short confirmation")
            assert citations == []
            assert "Synthetic connectivity probe" in context
            return SimpleNamespace(
                text="reachable",
                usage=SimpleNamespace(
                    model=self.model, prompt_tokens=3, completion_tokens=1, total_tokens=4
                ),
            )

    monkeypatch.setattr(live_gate, "provider_from_env", lambda: FakeProvider())
    monkeypatch.setattr(live_gate, "EVIDENCE", tmp_path / "external.json")
    assert live_gate.main() == 0
    evidence = (tmp_path / "external.json").read_text()
    assert "real-secret-never-output" not in evidence
    assert '"request_count": 1' in evidence
    assert '"provider_response_valid": true' in evidence


def test_live_gate_provider_failure_is_fail_after_execution(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("RAGOPS_LLM_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "real-secret-never-output")
    monkeypatch.setenv("OPENAI_MODEL", "gpt-5.6-luna")
    monkeypatch.setenv("RAGOPS_ALLOW_PAID_INTEGRATION_TESTS", "1")

    class FailingProvider:
        def generate(self, question, citations, context):
            raise ProviderError(ProviderErrorCategory.AUTHENTICATION)

    monkeypatch.setattr(live_gate, "provider_from_env", lambda: FailingProvider())
    monkeypatch.setattr(live_gate, "EVIDENCE", tmp_path / "external.json")
    assert live_gate.main() == 1
    evidence = (tmp_path / "external.json").read_text()
    assert '"status": "FAIL"' in evidence
    assert '"live_request_status": "EXECUTED"' in evidence
    assert '"request_count": 1' in evidence
