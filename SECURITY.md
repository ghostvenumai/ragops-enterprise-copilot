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
- Prompt-injection detection treats document text as untrusted; the layered
  defense and its limits are documented in `docs/SECURITY_NOTES.md`.
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

## Productization boundary (0.2.0.dev0)

Production startup is disabled pending verified identity, persistent authorized
retrieval and workers. Demo identity is client-controlled; do not expose demo
endpoints to untrusted users or use real data. Tenant administrators no longer
bypass the selected tenant in RBAC/retrieval. This fixes the backend bypass but
does not authenticate demo clients. See docs/PRODUCTIZATION_AUDIT.md for unresolved
release-blocking findings. Static analysis and synthetic evaluation are not a
security certification. The schema/migrations are not locally execution-verified.

## Secret scan suppressions

The project secret scan (`scripts/verify.py`, `make security`) has no directory,
file or pattern exclusions. `SECRET_SCAN_ALLOWLIST` explains individual synthetic
test literals by exact file, exact pattern and the SHA-256 of the exact matched
text, each with a written rationale; the scan report lists them as
`explained_synthetic_literals`. Any other match in the same file, any change to a
listed literal and any entry that no longer matches fail the scan.
`.env.integration.example` is the only additional environment template that may be
tracked, and only while every credential variable in it is empty and every URL uses
the `USER:PASSWORD` placeholder. `tests/unit/test_secret_scan_allowlist.py` proves
that nearby unapproved values are still reported.
