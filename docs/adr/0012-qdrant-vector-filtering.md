# ADR 0012: Qdrant persistence and mandatory tenant filtering

Status: accepted design; live Qdrant execution is environment-blocked.

Use Qdrant behind `VectorIndex` with a deterministic in-memory contract for
local tests. Qdrant payloads include authoritative tenant/resource/lifecycle
metadata and deterministic UUID point IDs derived from SHA-256. Every search constructs a filter
from the verified tenant and role before calling Qdrant. Workspace and
collection selections are intersected with server-authorized scope.

Document/version deactivation is enforced by lifecycle payload filters and
explicit delete methods. Upserts are idempotent, so retries after partial batch
writes converge without duplicate vectors. PostgreSQL and Qdrant remain separate
systems; final indexed state is committed only after successful upsert and a
reconciliation process is required for crash recovery.

Phase 10H validates this adapter against an isolated, disposable Qdrant
`v1.15.1` service using a unique collection and Compose project. The production
adapter remains the system under test; the fixture uses deterministic
embeddings. Qdrant filters enforce both validity boundaries in-server, with
empty `valid_to` representing an open interval. RC Qdrant data uses tmpfs and is
deleted after each run; it is intentionally not durable because the gate only
verifies adapter/query behavior. Production durability remains the separately
configured persistent Qdrant deployment's responsibility.
