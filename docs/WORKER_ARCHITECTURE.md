# Worker architecture

The API persists a job before enqueueing it. The worker consumes a message,
claims the tenant-scoped job, updates stage/progress, invokes the vector-index
interface and commits `indexed` only after the index call succeeds. Duplicate
delivery is safe because terminal jobs are no-ops and version IDs/content hashes
are stable.

Redis lists provide simple durable delivery with AOF enabled in the prepared
Compose profile. Redis and PostgreSQL are separate systems; no distributed ACID
claim is implied. Production crash recovery requires lease expiry and an
authorized integration gate.
