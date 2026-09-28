# Worker architecture

The API commits a job before enqueueing it, so a worker never sees a message for a job it
cannot read. If the queue is unavailable the upload answers `503` and the committed job is
marked `failed` with `queue_unavailable`; it stays visible and can be retried through the
retry API.

Delivery is at-least-once with reliable Redis lists. `BLMOVE` moves a message atomically
into the worker's own `<queue>:processing:<worker-id>` list. The worker acknowledges
(`LREM`) only after the database transaction committed; on a failure it records the
attempt and releases the message back to the queue, or acknowledges it once the job is
terminal. After a Redis outage or a restart the worker first moves its unacknowledged
messages back (`requeue_inflight`), so no message is lost.

Duplicate delivery is safe: `completed`, `cancelled` and `failed` jobs are no-ops, and
version IDs and content hashes are stable, so re-indexing overwrites the same vector
points. If indexing succeeded but the database step did not commit, the worker removes
that version's vectors again, so no partial active version remains.

Dependency failures are classified (`database_transient`, `redis_unavailable`,
`qdrant_unavailable`) and bounded by the job's `max_attempts`; the attempt count is
persisted in a fresh session so it survives the failed transaction. Between failures the
worker sleeps with capped exponential backoff and full jitter
(`RAGOPS_WORKER_BACKOFF_MAX_SECONDS`, default 5 s), which rules out a busy loop during an
outage. `SIGTERM`/`SIGINT` set a stop event; the current message finishes or is released,
then the clients close.

Redis and PostgreSQL are separate systems; no distributed ACID claim is implied.
