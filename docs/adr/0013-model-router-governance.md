# ADR 0013: deterministic model routing and bounded fallback

Use a rule-based router over an approved catalog. This keeps selection explainable, deterministic and inexpensive. A finite fallback list is generated after the same tenant and capability checks; execution tracks visited provider/model pairs and never retries permanent policy or authentication failures.
