# Model providers

`LLMProvider` implementations are isolated behind the provider adapter. The repository includes deterministic test and demo behavior plus OpenAI and Azure OpenAI configuration adapters; credentials are supplied by environment or secret references and are never persisted as values or returned by APIs.

The OpenAI adapter uses the SDK Responses API (`responses.create`) so current
GPT-5.6-class model deployments are handled through the supported interface.
Azure OpenAI remains on its existing Chat Completions path until its deployed
API version is separately verified for Responses API compatibility.

Provider prices and model capabilities are catalog metadata, not hard-coded commercial claims. Live provider checks require explicit operator configuration and are not run automatically.

## Live external-provider gate

Production and release-candidate verification require a real provider. The gate
does not fall back to the deterministic provider and reports `BLOCKED` when
credentials or the explicit paid-test opt-in are absent. OpenAI uses
`OPENAI_API_KEY` and `OPENAI_MODEL`; Azure
OpenAI uses `AZURE_OPENAI_API_KEY`, `AZURE_OPENAI_ENDPOINT`,
`AZURE_OPENAI_DEPLOYMENT` and `AZURE_OPENAI_API_VERSION`. Values are supplied
through the host secret environment and are never written to evidence.

On the authorized host, after loading the secret environment, run:

```bash
cd /home/serverserver/tools/ragops-enterprise-copilot && \
set -a && source "$HOME/ragops-integration.env" && set +a && \
RAGOPS_RC_GATES=external_llm_provider make rc-live-gate && \
cat evidence/product-v1/rc-live/external-llm-provider.json
```

`PASS` is valid only when the command has completed exactly one actual external
provider request (OpenAI: Responses API) with `RAGOPS_ALLOW_PAID_INTEGRATION_TESTS=1`. The recommended
low-cost smoke-test model is `gpt-5.6-luna`; it remains operator-configurable
and is not hard-coded into routing policy. Network, credential, provider, or
configuration failures remain `BLOCKED`/`FAIL` and must not be reclassified.

## Sanitized failure diagnostics

The external gate preserves optional SDK diagnostics on a failed opted-in request:
`provider_http_status`, `provider_exception_class`, `provider_error_type`,
`provider_error_code`, `provider_error_param`, and `sanitized_provider_message`.
Existing callers can continue to use `ProviderError.category` and its message.

Previously, the adapter discarded these fields when wrapping exceptions in
`ProviderError`, and the gate recorded only the category. An HTTP 200 Responses
result without text remains `PROVIDER_ERROR` with the message
`provider returned no text`, and now also records the Responses
`provider_response_status` (for example `incomplete` or `failed`), the
`provider_incomplete_reason` (for example `max_output_tokens`) and, for a failed
response, the sanitized `provider_error_code` and message. The actual cause of a
failed live request still requires a new authorized host run; offline diagnostics
tests do not establish it.

If the provider client cannot be constructed from the configuration, the gate
reports `FAIL` with `live_request_status: NOT_EXECUTED` and `request_count: 0`,
because no provider request was sent.

Diagnostics select individual scalar fields from SDK error bodies. They never
dump the request, response, headers, environment, or exception. API key patterns,
Bearer credentials, labelled key/token/secret/authorization values, and configured
provider keys are redacted before each diagnostic is bounded to 400 characters.
Control characters and line breaks become spaces. Fields are sanitized again at
the evidence boundary. The generic public exception message stays unchanged;
raw SDK exception chaining is suppressed to avoid accidental traceback disclosure.

HTTP 401/403 map to the existing `AUTHENTICATION` category, 429 to `RATE_LIMIT`,
timeouts to `TIMEOUT`, and connection/server failures to `PROVIDER_ERROR`.
HTTP 404 or known `model_not_found`/`model_unavailable` codes map to
`MODEL_UNAVAILABLE`; generic HTTP 400 remains `INVALID_REQUEST`. Merely mentioning
the word "model" in an error message does not change its category.

Local regression tests use the installed SDK with `httpx.MockTransport`; all gate
evidence written by tests is temporary. Existing `rc-live` failure and PASS evidence
is kept intact until the operator deliberately reruns the gate once.
