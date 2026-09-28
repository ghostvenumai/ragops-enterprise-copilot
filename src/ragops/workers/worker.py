"""Bounded, idempotent ingestion worker orchestration."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from ragops.persistence.models import Document, DocumentVersion, IngestionJob
from ragops.vector.index import VectorIndex

JOB_STATES = frozenset({"queued", "running", "retrying", "completed", "failed", "cancelled"})
STAGES = (
    "validation",
    "extraction",
    "normalization",
    "chunking",
    "embedding",
    "indexing",
    "finalization",
)
RETRYABLE = frozenset(
    {"redis_unavailable", "provider_timeout", "qdrant_unavailable", "database_transient"}
)


class NoopVectorIndex:
    def upsert(self, tenant_id: str, document_version_id: UUID, content_hash: str) -> None:
        return None

    def delete_version_vectors(self, tenant_id: str, version_id: str) -> int:
        return 0


class IngestionWorker:
    def __init__(
        self,
        session: Session,
        vector_index: VectorIndex | None = None,
        worker_id: str | None = None,
        max_attempts: int = 3,
        audit: Callable[[str, IngestionJob], None] | None = None,
    ) -> None:
        self.session = session
        self.vector_index = vector_index or NoopVectorIndex()
        self.worker_id = worker_id or f"worker-{uuid4()}"
        self.max_attempts = max_attempts
        self.audit = audit or (lambda _event, _job: None)

    def create_job(
        self,
        tenant_id: str,
        document_id: UUID,
        version_id: UUID,
        correlation_id: UUID | None = None,
    ) -> IngestionJob:
        document = self.session.scalar(
            select(Document).where(Document.tenant_id == tenant_id, Document.id == document_id)
        )
        version = self.session.scalar(
            select(DocumentVersion).where(
                DocumentVersion.tenant_id == tenant_id,
                DocumentVersion.id == version_id,
                DocumentVersion.document_id == document_id,
            )
        )
        if document is None or version is None:
            raise ValueError("document/version relationship is invalid")
        job = IngestionJob(
            tenant_id=tenant_id,
            document_id=document_id,
            document_version_id=version_id,
            correlation_id=correlation_id or uuid4(),
            status="queued",
            state="queued",
            stage="validation",
            max_attempts=self.max_attempts,
            updated_at=datetime.now(UTC),
        )
        self.session.add(job)
        self.session.flush()
        self.audit("ingestion_job_created", job)
        return job

    def process(
        self,
        job_id: UUID,
        tenant_id: str,
        *,
        fail_stage: str | None = None,
        fail_code: str = "provider_timeout",
    ) -> IngestionJob:
        job = self.session.scalar(
            select(IngestionJob).where(
                IngestionJob.tenant_id == tenant_id, IngestionJob.id == job_id
            )
        )
        if job is None:
            raise ValueError("job not found")
        # Terminal jobs are no-ops on redelivery; a failed job runs again only via retry().
        if job.status in {"completed", "cancelled", "failed"}:
            return job
        if job.status == "running":
            return job  # duplicate delivery is idempotently ignored
        if job.attempt_count >= job.max_attempts:
            job.status = job.state = "failed"
            job.error_code = "retry_exhausted"
            job.error_message_redacted = "ingestion retry limit reached"
            return job
        now = datetime.now(UTC)
        job.status = job.state = "running"
        job.attempt_count = job.attempts = (job.attempt_count or 0) + 1
        job.started_at = job.started_at or now
        job.worker_id = self.worker_id
        job.updated_at = now
        self.audit("ingestion_started", job)
        document = self.session.scalar(
            select(Document).where(Document.tenant_id == tenant_id, Document.id == job.document_id)
        )
        version = self.session.scalar(
            select(DocumentVersion).where(
                DocumentVersion.tenant_id == tenant_id,
                DocumentVersion.id == job.document_version_id,
                DocumentVersion.document_id == job.document_id,
            )
        )
        if document is None or version is None:
            return self._fail(job, "invalid_relationship", retryable=False)
        document.status = "processing"
        indexed = False
        try:
            for index, stage in enumerate(STAGES, start=1):
                job.stage = stage
                job.progress = index * 14
                job.updated_at = datetime.now(UTC)
                self.audit("ingestion_stage_changed", job)
                if fail_stage == stage:
                    return self._fail(job, fail_code, retryable=fail_code in RETRYABLE)
                if stage == "indexing":
                    self.vector_index.upsert(tenant_id, version.id, version.content_hash)
                    indexed = True
            document.status = "indexed"
            version.ingestion_status = "indexed"
            job.status = job.state = "completed"
            job.stage = "finalization"
            job.progress = 100
            job.completed_at = datetime.now(UTC)
            job.updated_at = job.completed_at
            self.session.flush()
        except Exception:
            if indexed:
                self.remove_vectors(tenant_id, version.id)
            raise
        self.audit("ingestion_completed", job)
        return job

    def remove_vectors(self, tenant_id: str, version_id: UUID) -> None:
        """Best-effort compensation: vectors of an uncommitted version must not stay visible."""
        try:
            self.vector_index.delete_version_vectors(tenant_id, str(version_id))
        except Exception:  # noqa: BLE001, S110 - retried by the next delivery; upsert is idempotent
            pass

    def record_failure(self, job_id: UUID, tenant_id: str, code: str) -> IngestionJob:
        """Persist one failed attempt after the processing transaction was rolled back."""
        job = self.session.scalar(
            select(IngestionJob).where(
                IngestionJob.tenant_id == tenant_id, IngestionJob.id == job_id
            )
        )
        if job is None:
            raise ValueError("job not found")
        if job.status in {"completed", "cancelled", "failed"}:
            return job
        job.attempt_count = job.attempts = min((job.attempt_count or 0) + 1, job.max_attempts)
        job.worker_id = self.worker_id
        return self._fail(job, code, retryable=code in RETRYABLE)

    def cancel(self, job_id: UUID, tenant_id: str) -> IngestionJob:
        job = self.session.scalar(
            select(IngestionJob).where(
                IngestionJob.tenant_id == tenant_id, IngestionJob.id == job_id
            )
        )
        if job is None:
            raise ValueError("job not found")
        if job.status in {"completed", "failed"}:
            raise ValueError("finished job cannot be cancelled")
        job.status = job.state = "cancelled"
        job.updated_at = datetime.now(UTC)
        self.session.flush()
        self.audit("ingestion_cancelled", job)
        return job

    def retry(self, job_id: UUID, tenant_id: str) -> IngestionJob:
        job = self.session.scalar(
            select(IngestionJob).where(
                IngestionJob.tenant_id == tenant_id, IngestionJob.id == job_id
            )
        )
        if job is None:
            raise ValueError("job not found")
        if job.status not in {"failed", "retrying"} or job.attempt_count >= job.max_attempts:
            raise ValueError("job is not retryable")
        job.status = "queued"
        job.state = "queued"
        job.updated_at = datetime.now(UTC)
        self.session.flush()
        self.audit("ingestion_retry", job)
        return job

    def _fail(self, job: IngestionJob, code: str, *, retryable: bool) -> IngestionJob:
        job.error_code = job.failure_code = code
        job.error_message_redacted = "ingestion failed"
        job.updated_at = datetime.now(UTC)
        if retryable and job.attempt_count < job.max_attempts:
            # Phase-1 state constraint predates the explicit status column;
            # retain queued state for compatibility while status is retrying.
            job.status = "retrying"
            job.state = "queued"
        else:
            job.status = job.state = "failed"
            job.completed_at = datetime.now(UTC)
        self.session.flush()
        self.audit("ingestion_retry" if job.status == "retrying" else "ingestion_failed", job)
        return job
