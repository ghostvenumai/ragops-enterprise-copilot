"""Bounded, idempotent ingestion worker orchestration."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from ragops.auth.rbac import ACCESS_RANK
from ragops.ingestion.extract import DocumentExtractionError, extract_segments
from ragops.ingestion.normalization import chunk_words, tokenize
from ragops.persistence.models import Document, DocumentVersion, IngestionJob
from ragops.vector.embedding import DeterministicEmbeddingProvider, EmbeddingProvider
from ragops.vector.index import VectorIndex, VectorPayload

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


CHUNK_WORDS, CHUNK_OVERLAP = 120, 20


class IngestionContentError(ValueError):
    """The document cannot be indexed as it is; retrying the same content cannot help."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class NoopVectorIndex:
    """Used when no vector store is configured (local development without Qdrant)."""

    dimension = 64

    def upsert_chunks(self, vectors: list[tuple[list[float], VectorPayload]]) -> int:
        return len(vectors)

    def delete_version_vectors(self, tenant_id: str, version_id: str) -> int:
        return 0


def _utc_iso(value: datetime) -> str:
    # SQLite returns naive datetimes for timezone-aware columns; they are stored as UTC.
    return (value if value.tzinfo is not None else value.replace(tzinfo=UTC)).isoformat()


def build_chunk_vectors(
    document: Document, version: DocumentVersion, embedding: EmbeddingProvider
) -> list[tuple[list[float], VectorPayload]]:
    """Extract, chunk and embed one document version into complete vector payloads.

    Every identifier comes from the persisted document and version. Missing mandatory
    metadata, unreadable content and content without indexable text fail closed.
    """
    identifiers = (
        document.tenant_id,
        document.id,
        document.workspace_id,
        document.collection_id,
        version.id,
        version.filename,
        version.valid_from,
        embedding.model,
    )
    if (
        any(value is None or value == "" for value in identifiers)
        or version.tenant_id != document.tenant_id
        or version.document_id != document.id
        or document.access_level not in ACCESS_RANK
    ):
        raise IngestionContentError("metadata_incomplete")
    try:
        segments = extract_segments(version.filename, bytes(version.content))
    except DocumentExtractionError:
        raise IngestionContentError("extraction_failed") from None
    chunks = [
        (page, chunk)
        for page, text in segments
        for chunk in chunk_words(text, CHUNK_WORDS, CHUNK_OVERLAP)
        if tokenize(chunk)
    ]
    if not chunks:
        raise IngestionContentError("empty_document")
    created = datetime.now(UTC).isoformat()
    vectors = embedding.embed([chunk for _, chunk in chunks])
    return [
        (
            vector,
            VectorPayload(
                tenant_id=document.tenant_id,
                workspace_id=str(document.workspace_id),
                collection_id=str(document.collection_id),
                document_id=str(document.id),
                document_version_id=str(version.id),
                chunk_id=f"{version.id}:{index}",
                access_level=document.access_level,  # type: ignore[arg-type]
                document_status="indexed",
                version_status="indexed",
                content_hash=hashlib.sha256(chunk.encode("utf-8")).hexdigest(),
                chunk_index=index,
                source_name=version.filename,
                created_at=created,
                valid_from=_utc_iso(version.valid_from),
                valid_to=_utc_iso(version.valid_to) if version.valid_to else None,
                title=document.title or None,
                page_number=page,
                chunk_text=chunk,
                embedding_model=embedding.model,
            ),
        )
        for index, ((page, chunk), vector) in enumerate(zip(chunks, vectors, strict=True))
    ]


class IngestionWorker:
    def __init__(
        self,
        session: Session,
        vector_index: VectorIndex | None = None,
        worker_id: str | None = None,
        max_attempts: int = 3,
        audit: Callable[[str, IngestionJob], None] | None = None,
        embedding: EmbeddingProvider | None = None,
    ) -> None:
        self.session = session
        self.vector_index = vector_index or NoopVectorIndex()
        # Documents are embedded in the dimension of the index they are written to.
        self.embedding = embedding or DeterministicEmbeddingProvider(self.vector_index.dimension)
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
        vectors: list[tuple[list[float], VectorPayload]] = []
        try:
            for index, stage in enumerate(STAGES, start=1):
                job.stage = stage
                job.progress = index * 14
                job.updated_at = datetime.now(UTC)
                self.audit("ingestion_stage_changed", job)
                if fail_stage == stage:
                    return self._fail(job, fail_code, retryable=fail_code in RETRYABLE)
                if stage == "embedding":
                    try:
                        vectors = build_chunk_vectors(document, version, self.embedding)
                    except IngestionContentError as exc:
                        document.status = "failed"
                        return self._fail(job, exc.code, retryable=False)
                if stage == "indexing":
                    self._index(tenant_id, document, version, vectors)
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

    def _index(
        self,
        tenant_id: str,
        document: Document,
        version: DocumentVersion,
        vectors: list[tuple[list[float], VectorPayload]],
    ) -> None:
        """Replace this version's chunks, then drop the chunks of the versions it supersedes."""
        self.vector_index.delete_version_vectors(tenant_id, str(version.id))
        try:
            written = self.vector_index.upsert_chunks(vectors)
            if written != len(vectors):
                raise RuntimeError("vector index accepted fewer chunks than were sent")
        except Exception:
            self.remove_vectors(tenant_id, version.id)
            raise
        superseded = self.session.scalars(
            select(DocumentVersion.id).where(
                DocumentVersion.tenant_id == tenant_id,
                DocumentVersion.document_id == document.id,
                DocumentVersion.id != version.id,
            )
        )
        for other in superseded:
            self.vector_index.delete_version_vectors(tenant_id, str(other))

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
