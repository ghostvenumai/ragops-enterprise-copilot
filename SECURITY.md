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
