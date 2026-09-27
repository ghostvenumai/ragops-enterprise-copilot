# ADR 0011: Redis list queue and bounded ingestion worker

Status: accepted design; live Redis execution is environment-blocked.

Use a small queue abstraction with a deterministic in-memory implementation for
tests and Redis lists (`RPUSH`/`BLPOP`) for deployment. This is simpler than
Celery or Dramatiq for the current single job type, keeps the operational
footprint small, and leaves retries and persistence visible in the domain.

`IngestionWorker` claims jobs through tenant-scoped database reads and writes
explicit status/stage/progress fields. Processing is idempotent: completed or
cancelled jobs are no-ops, and vector writes receive the stable version ID and
content hash. Retryable errors are bounded by `max_attempts`; invalid input and
relationship errors fail permanently. Redis and PostgreSQL cannot form one ACID
transaction, so a job remains `processing` until the vector write succeeds and
the final database transition commits. Recovery of a crashed worker is a later
operational gate and must use leases/claiming in PostgreSQL.

Redis is internal-only in Compose, has bounded memory and optional AOF queue
durability. It is not used as a secret store.
