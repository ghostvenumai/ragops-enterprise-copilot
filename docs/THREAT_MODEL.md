# Threat Model

## Method

STRIDE-oriented assessment for local portfolio runtime.

| STRIDE | Attack Surface | Control | Residual Risk | Test Evidence |
| --- | --- | --- | --- | --- |
| Spoofing | Tenant or role fields in requests | RBAC and tenant filters | Demo auth is simplified | `tests/security/test_mandantentrennung.py` |
| Tampering | Uploaded file paths | Path traversal and extension checks | Binary parser adapters are local fallback | `tests/unit/test_security_controls.py` |
| Repudiation | Query handling | Correlation ID and audit events | Local logs are file-backed | Integration workflow tests |
| Information disclosure | Cross-tenant retrieval | Tenant filter before scoring | Admin role needs production IAM | Security tests |
| Denial of service | Large uploads and long inputs | Size and input limits, Docker limits | Full rate limiter is adapter interface | Docker config check |
| Elevation of privilege | Prompt injection in documents | Detection, penalty, evidence filtering | Pattern-based detection is incomplete | Prompt-injection tests |

## Required Defensive Test Cases

- Ignore previous instructions.
- Cross-tenant data request.
- System prompt disclosure.
- Fake administrator override.
- Obfuscated exfiltration language.
- Source-priority manipulation.
- Indirect injection in support ticket.

Synthetic fixtures under `data/synthetic/documents` cover these cases.
