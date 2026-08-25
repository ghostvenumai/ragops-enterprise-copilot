# Security Policy

## Supported Version

This portfolio repository supports the current `main` branch.

## Reporting

Do not include secrets or real personal data in reports. Security issues should
include reproduction steps using synthetic data only.

## Local Security Model

- Deterministic provider by default.
- No external LLM call unless explicitly configured through environment
  variables.
- Tenant-aware retrieval filters.
- Prompt-injection detection treats document text as untrusted.
- Audit logs redact PII and avoid full confidential document bodies.

## TTS Credentials and Cache

- OpenAI TTS reads its credential only from the process environment variable
  `OPENAI_API_KEY` and only after a validated cache miss.
- Credentials are excluded from cache keys, manifests, logs, reports,
  screenshots, videos, tests, and repository files.
- Provider errors pass through credential and Authorization-header redaction
  before they can reach terminal output or persistent state.
- Cached audio is local, Git-ignored, validated before reuse, and committed by
  atomic rename only after successful decoding and format checks.
- A complete cache is usable without API access. A missing credential plus a
  cache miss fails closed and creates no replacement audio.
- `--force-tts` and `--clear-tts-cache` require explicit user commands; the
  master loop never selects either mode automatically.

Operational details and test evidence are documented in
[docs/TTS_CACHE.md](docs/TTS_CACHE.md).
