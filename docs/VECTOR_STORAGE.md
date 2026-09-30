# Vector storage (Phase 5)

`VectorIndex` is the only contract used by workers and retrieval. The
deterministic implementation is for tests/demo; `QdrantVectorIndex` is the
production adapter and is configured explicitly with a URL, collection,
dimension and schema version. Production never falls back to in-memory vectors.

Payloads carry tenant, workspace, collection, document and version IDs, access
level, lifecycle status, content hash, chunk index and citation metadata. Since
ENT-11.1 they also carry `chunk_text` (the wording the chunk was embedded from)
and `embedding_model`; both are additive and optional in the schema, but the
productive retriever rejects chunks without them. `content_hash` is the SHA-256
of the chunk text. A point without a tenant is rejected before it is written. The
point ID is a canonical UUID built from the first 128 bits of the SHA-256 digest
of tenant, version, chunk index and content hash. It is deterministic and uses
a Qdrant-supported ID representation, so retries and partial batch failures
converge through upsert rather than duplicate points.

The Qdrant filter is built centrally and includes tenant, indexed document and
version status, access level and optional authorized workspace/collection sets.
It also evaluates `valid_from` and `valid_to` inside Qdrant: open-ended
versions match an empty `valid_to`, and bounded versions match only before the
end timestamp. Qdrant payload indexes use keyword indexes for identity/status
fields and datetime indexes for validity fields.
The deterministic adapter applies the same filter before scoring. Deletion APIs
remove vectors by tenant plus document/version; soft-deactivated records are
also excluded by lifecycle filters.

Qdrant and PostgreSQL do not share an ACID transaction. Indexing therefore
marks the document indexed only after the vector upsert succeeds; operational
reconciliation remains required for crashed distributed transactions.

## Phase 10H RC live gate

`make test-qdrant-integration` runs `scripts/qdrant_live_gate.py`. It starts
only `rc-qdrant` under a unique Compose project, obtains its dynamically
published `127.0.0.1` REST port from that exact service, and exercises the real
`QdrantVectorIndex` with deterministic embeddings and synthetic tenant/lifecycle
fixtures. It checks partial-write recovery, repeated upserts, authorized
tenant/workspace/collection/access/validity filters, stale and inactive records,
and tenant-scoped deletion. The test deletes its unique collection and removes
only its exact RC service; it never selects production collections or uses
`--remove-orphans`. The service is ephemeral and memory-backed; no Qdrant data
survives container removal. Evidence separates scenario and cleanup status.
