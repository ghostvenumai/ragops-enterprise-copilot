# Health and readiness

`/health` is process liveness and performs no dependency probes. `/ready` is the production traffic gate; mandatory PostgreSQL, Redis, Qdrant, OIDC and model configuration must be valid before production startup. Detailed dependency errors stay out of public responses.
