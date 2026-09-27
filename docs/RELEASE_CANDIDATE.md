# Release-candidate gate

Phase 10 is verification-only. `make rc-gate` records commit, host preflight, Compose output and one machine-readable status per mandatory live gate under `evidence/product-v1/rc/`. Phase 10B/10C uses `make rc-live-gate`; set `RAGOPS_RC_GATES=postgresql,redis_worker,qdrant` to rerun selected gates without deleting previous evidence. The Qdrant gate is also directly runnable with `make test-qdrant-integration`; it bootstraps only the isolated `rc-qdrant` service and its own ephemeral collection. A gate is `PASS` only after execution on an authorized host; unavailable Docker, PostgreSQL, Redis, Qdrant, OIDC or provider services remain `BLOCKED`. No RC tag is created while any mandatory gate is blocked.

Copy [.env.integration.example](../.env.integration.example) outside the repository, fill secrets manually, and set `RAGOPS_ALLOW_PAID_INTEGRATION_TESTS=1` only for an intentionally bounded paid-provider test.
