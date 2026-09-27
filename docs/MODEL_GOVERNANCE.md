# Model governance

Tenant policies constrain providers, models, routing tier, high-risk allowlists, fallback and per-request cost ceilings. Client requests cannot force a provider, premium model, routing class or cost ceiling. High-risk requests require an approved high-risk model and retain citation validation and abstention.

`UsageRecord` stores normalized provider/model, routing class, token counts when reported, estimated cost, actual cost, latency and fallback count. Synthetic deterministic pricing is for demo/testing only.
