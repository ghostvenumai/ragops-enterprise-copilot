# Rate limiting

Limits are keyed by the authoritative tenant and user from the verified identity
(OIDC `tenant_id` and `sub`) plus an endpoint class, never by IP or by request headers
or body fields. Authentication runs first, so a 401 consumes no quota. Classes:
`rag` (`/v1/query`, document reads), `upload`, `ingestion` (jobs, retry, cancel,
reindex, ingest), `knowledge` (workspaces, collections, versions, delete),
`evaluation`, `router_simulation` and `administration` (all other admin routes,
audit events and costs). Only `/health`, `/ready` and `/metrics` are exempt.

The algorithm is a fixed window: `RAGOPS_RATE_LIMIT_REQUESTS` per
`RAGOPS_RATE_LIMIT_WINDOW_SECONDS` for each bucket, with no separate burst allowance
and no per-tenant or per-route overrides. It is not a sliding window: a caller can use
the full limit at the end of one window and again at the start of the next. The
limit and window must be integers in 1..100000 and 1..86400.

`RAGOPS_RATE_LIMIT_BACKEND=memory` is a per-process adapter for local and test use.
Production requires `redis`: one Lua script reads the counter, increments it only
while it is below the limit, sets the window expiry on the first request and heals a
key without expiry, so concurrent requests and multiple app instances share one
atomic counter and denied retries neither inflate the counter nor extend the window.
Redis keys are `<namespace>:<sha256>` of the identity, so they contain no raw tenant
or user identifier and delimiter characters cannot make two identities collide.

Every limited response carries `RateLimit-Limit`, `RateLimit-Remaining` and
`RateLimit-Reset` (seconds). An exceeded limit returns 429 with a positive
`Retry-After`; the request stops before any retrieval or provider call. If Redis does
not answer within `RAGOPS_RATE_LIMIT_TIMEOUT_SECONDS` (at most 5 s), the request fails
closed with 503 and `Retry-After: 1` instead of being allowed or misreported as 429.
The `rate_limiting` RC gate (`scripts/rate_limiting_gate.py`) proves this against the
RC Redis service.
