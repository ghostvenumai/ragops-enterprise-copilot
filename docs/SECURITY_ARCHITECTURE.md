# Security Architecture

## Boundaries

- Tenant boundary: user tenant is enforced before retrieval and CRM filtering.
- Role boundary: access levels map to role maximums.
- Data boundary: document text is untrusted and can be penalized or removed.
- Provider boundary: external provider adapters require environment variables.
- Telemetry boundary: PII is masked and full confidential text is not logged.

## Controls

- RBAC and tenant guard.
- File extension allowlist.
- File size limit.
- Safe filenames and path traversal checks.
- Prompt-injection pattern detection.
- PII masking.
- Correlation IDs.
- Audit events.
- Docker hardening.
