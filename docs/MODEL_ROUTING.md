# Model routing

RAGOps classifies requests deterministically as `SIMPLE`, `STANDARD`, `COMPLEX` or `HIGH_RISK` from retrieval and workflow signals. `LLMModelRouter` filters the catalog by tenant policy, capabilities, context window, risk approval and cost ceiling, then selects the least-cost eligible model. Decisions are immutable and expose bounded reason codes rather than hidden reasoning.

Fallbacks are an explicit, finite chain. Only transient provider failures (timeout, rate limit, unavailable or provider error) may advance it; visited pairs prevent loops. Policy and capability constraints are applied to every candidate.

Precedence, as implemented: client-requested providers or models are ignored
(`CLIENT_SELECTION_IGNORED`); tenant policy filters apply first (allowed providers
and models, maximum routing tier, cost ceiling); then capability and routing-class
requirements; the lowest estimated cost wins, ties break on `fallback_priority` and
then provider/model id. A `HIGH_RISK` request may use a model that is either
platform-approved (`high_risk_allowed`) or listed in the tenant's
`high_risk_models`; a tenant restricts high-risk routing through `allowed_models`.
If no candidate remains, routing fails closed (`FallbackPolicyError`, HTTP 409
`NO_ELIGIBLE_MODEL` on the simulation API). Catalog entries without a provider or
model id are rejected.

Current scope: the router is exercised by `POST /v1/admin/model-router/simulate`.
`/v1/query` still generates through the single provider from `provider_from_env()`,
and tenant model policies and the catalog are in-process state; the
`model_configurations` and `tenant_model_policies` tables are not yet read by the
router. The `model_router` RC gate (`scripts/model_router_gate.py`) proves these
routing invariants without provider calls and records both limitations.
