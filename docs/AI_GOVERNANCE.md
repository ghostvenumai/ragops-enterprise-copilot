# AI Governance

## Controls

- Prompt version: `RAGOPS_PROMPT_VERSION`, default `ragops-prompt-v1`.
- Model version: `RAGOPS_MODEL`, default deterministic local model.
- Provider abstraction prevents accidental external calls in default mode.
- Every answer is composed from selected evidence and citations.
- If evidence is missing, unsafe, cross-tenant, or insufficient, the workflow
  abstains.
- Document text is untrusted and cannot override workflow rules.
- Audit events store metadata, not full confidential source text.
- PII is masked before telemetry and unsafe output.

## Review Requirements

Critical and high findings from the separate review pass must be fixed or
accepted as documented residual risk before release.
