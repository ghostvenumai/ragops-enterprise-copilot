# ADR 0016: Isolated Qdrant live release-candidate gate

Status: accepted.

The Qdrant release-candidate gate starts the pinned Qdrant `v1.15.1` image as
`rc-qdrant` in `docker-compose.rc.yml`, scoped to a per-run Compose project.
Only REST port 6333 is published, on a Docker-assigned `127.0.0.1` port. The
gate discovers it through `compose port` for the exact project and service;
it does not inspect unrelated containers or use container IPs.

The gate exercises `QdrantVectorIndex` itself with deterministic embeddings,
unique synthetic collection names and synthetic tenant/lifecycle payloads. It
checks idempotent point IDs, partial-upsert convergence, server-side tenant and
scope filters, validity, stale lifecycle exclusion, and tenant-scoped deletion.
It removes only its collection and exact RC service. Compose has no persistent
volume, so disposable RC data is memory-backed and container removal clears
it. Production vectors and storage configuration are unchanged.

The result is BLOCKED only when the Docker/host service prerequisite is absent,
FAIL when bootstrap, readiness, a contract assertion, or cleanup fails, and
PASS only when all scenario and cleanup checks pass. Evidence distinguishes
scenario outcome from cleanup outcome and omits endpoint URLs, service logs,
credentials, vector contents, and unrelated container metadata.
