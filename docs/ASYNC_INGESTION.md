# Asynchronous ingestion (Phase 4)

Uploads create the document version and an `IngestionJob`, then return `202`
with `document_id`, `version_id` and `job_id`. A queue message carries only
stable IDs and the trusted tenant. The worker revalidates persisted
relationships before processing.

Stages are validation, extraction, normalization, chunking, embedding,
indexing and finalization. Status transitions are queued, running, retrying,
completed, failed or cancelled. Retryable failures are bounded (default three)
and expose only a redacted safe message. Non-retryable validation and tenant
relationship errors stop immediately.

The job is committed before its message is enqueued. If the queue is unavailable the
upload returns `503` and the job is `failed` with the retryable code `queue_unavailable`.

The in-memory adapter is deterministic test infrastructure. Redis integration is
provided by `RedisIngestionQueue` with reliable per-worker processing lists; see
[WORKER_ARCHITECTURE.md](WORKER_ARCHITECTURE.md) for acknowledgement, redelivery and
backoff.
