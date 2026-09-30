# Threat Model Validation

Generated evidence is written to `evidence/` by `make verify`. The validation
links tests to controls and records missing optional tools without inventing
results.

For the release candidate, the `security` gate (`scripts/security_gate.py`) maps each
mandatory control of `docs/THREAT_MODEL.md` to exactly one evidence source and writes
the per-control result to `evidence/product-v1/rc-live/security.json`. A control
without present, well-formed, current and true evidence keeps the gate `BLOCKED`.
