# Model routing

RAGOps classifies requests deterministically as `SIMPLE`, `STANDARD`, `COMPLEX` or `HIGH_RISK` from retrieval and workflow signals. `LLMModelRouter` filters the catalog by tenant policy, capabilities, context window, risk approval and cost ceiling, then selects the least-cost eligible model. Decisions are immutable and expose bounded reason codes rather than hidden reasoning.

Fallbacks are an explicit, finite chain. Only transient provider failures (timeout, rate limit, unavailable or provider error) may advance it; visited pairs prevent loops. Policy and capability constraints are applied to every candidate.
