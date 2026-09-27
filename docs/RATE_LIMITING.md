# Rate limiting

The deterministic adapter keys limits by authoritative tenant, user and endpoint class. Production should use Redis and fail closed when the configured limiter is mandatory. Limit classes include RAG, upload, ingestion operations, evaluation, administration and router simulation.
