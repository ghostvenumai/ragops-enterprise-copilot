# Security Notes: Prompt-Injection Defense

## Threat model

Prompt injection reaches this system on two paths:

1. **Direct**: the user's question itself carries an instruction-override or
   data-exfiltration attempt.
2. **Indirect**: ingested document text carries hidden instructions that would
   be executed if the text were passed to a generative model as trusted
   context.

## Layered defenses

| Layer | Mechanism | Location |
|---|---|---|
| 1 | Pattern detection on the user question (fail-closed abstain) | `security/prompt_injection.py`, `workflows/state_machine.py` |
| 2 | Per-chunk injection flag at ingestion time | `ingestion/pipeline.py` |
| 3 | Flagged chunks excluded from retrieval results before composition | `workflows/state_machine.py` |
| 4 | Structural tenant isolation in retrieval (filters, RBAC) — independent of detection | `retrieval/hybrid.py`, `auth/rbac.py` |
| 5 | Deterministic default provider that never interprets document text as instructions | `llm/providers.py` |
| 6 | Citations HTML-escaped before dashboard rendering | `apps/dashboard/dashboard.py` |

## Detection coverage

The detector normalizes input (NFKC, strips zero-width/bidi/control
characters, collapses whitespace) and then applies:

- **Phrase patterns** (EN/DE/FR/ES): instruction override, role-play
  jailbreaks, system-prompt extraction, exfiltration, cross-tenant probing,
  privileged-override claims, citation manipulation, hidden-instruction
  markers, code/command execution requests, and requests for
  hidden/confidential document dumps or security-control bypasses.
- **Compact matching**: input is lowercased, leet-mapped (`1→i`, `3→e`, …),
  filler words removed, and stripped to alphanumerics so letter-spacing
  (`i-g-n-o-r-e`), dotting, and leet-speak evasions still match.
- **Hidden channels**: HTML comments are scanned separately; base64 tokens
  (≥24 chars) are decoded and recursively re-scanned.

Regression tests for every covered attack class live in
`tests/unit/test_prompt_injection.py`, and the external audit probe
(`portfolio_audit/audit_scripts/ragops_security_probe.py`) passes with
detection + abstention + zero foreign citations on all cases.

## Honest limits

Pattern-based detection cannot guarantee zero false negatives against an
adaptive adversary — novel phrasings, other languages, or semantic
paraphrases can evade any static list. The guarantees this system actually
relies on are structural:

- Tenant isolation and RBAC are enforced in retrieval regardless of whether
  the detector fires (layer 4).
- The shipped default provider is deterministic and never executes
  instructions found in document text (layer 5); the OpenAI/Azure providers
  are unimplemented stubs.

If a real generative provider is added, document text must be delimited as
untrusted data in the prompt, and detection should be complemented with an
LLM-based classifier or a dedicated guard model before production use.
