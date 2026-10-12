"""Bounded, idempotent ingestion worker orchestration."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from ragops.auth.rbac import ACCESS_RANK
from ragops.ingestion.extract import DocumentExtractionError
from ragops.ingestion.normalization import chunk_spans, tokenize
from ragops.ingestion.ocr import OcrEngineUnavailable, OcrLanguageUnavailable
from ragops.ingestion.pages import (
    DocumentExtraction,
    DocumentExtractor,
    ExtractionMethod,
    PageExtraction,
)
from ragops.persistence.models import (
    Document,
    DocumentChunkSource,
    DocumentPageExtraction,
    DocumentVersion,
    IngestionJob,
)
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


@dataclass(frozen=True)
class SourcedChunk:
    """One chunk of one page with the OCR lines its words came from."""

    page: PageExtraction
    text: str
    lines: tuple[int, ...]
    method: str


@dataclass(frozen=True)
class ChunkBuild:
    extraction: DocumentExtraction
    chunks: tuple[SourcedChunk, ...]
    vectors: list[tuple[list[float], VectorPayload]]


def _page_chunks(page: PageExtraction) -> list[SourcedChunk]:
    """Chunks never span pages; each keeps the OCR lines of exactly its own words."""
    words = page.words
    out: list[SourcedChunk] = []
    for start, end in chunk_spans(len(words), CHUNK_WORDS, CHUNK_OVERLAP):
        window = words[start:end]
        text = " ".join(word.text for word in window)
        if not tokenize(text):
            continue
        lines = tuple(dict.fromkeys(word.line for word in window if word.line is not None))
        native = any(word.line is None for word in window)
        if lines and native:
            method = ExtractionMethod.HYBRID.value
        elif lines:
            method = ExtractionMethod.OCR.value
        else:
            method = ExtractionMethod.NATIVE_TEXT.value
        out.append(SourcedChunk(page, text, lines, method))
    return out


def build_chunks(
    document: Document,
    version: DocumentVersion,
    embedding: EmbeddingProvider,
    extractor: DocumentExtractor | None = None,
) -> ChunkBuild:
    """Extract, chunk and embed one document version into complete vector payloads.

    Every identifier comes from the persisted document and version. Missing mandatory
    metadata and unreadable content fail closed. Only pages that passed extraction are
    chunked; pages needing review are reported, never indexed.
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
        extraction = (extractor or DocumentExtractor()).extract(
            version.filename, bytes(version.content)
        )
    except (OcrEngineUnavailable, OcrLanguageUnavailable) as exc:
        raise IngestionContentError(exc.code) from None
    except DocumentExtractionError:
        raise IngestionContentError("extraction_failed") from None
    chunks = tuple(
        chunk for page in extraction.pages if page.indexable for chunk in _page_chunks(page)
    )
    created = datetime.now(UTC).isoformat()
    vectors = embedding.embed([chunk.text for chunk in chunks]) if chunks else []
    return ChunkBuild(
        extraction,
        chunks,
        [
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
                    content_hash=hashlib.sha256(chunk.text.encode("utf-8")).hexdigest(),
                    chunk_index=index,
                    source_name=version.filename,
                    created_at=created,
                    valid_from=_utc_iso(version.valid_from),
                    valid_to=_utc_iso(version.valid_to) if version.valid_to else None,
                    title=document.title or None,
                    page_number=chunk.page.page_number,
                    chunk_text=chunk.text,
                    embedding_model=embedding.model,
                    extraction_method=chunk.method,
                    ocr_quality=chunk.page.quality.state if chunk.lines else None,
                    page_width=chunk.page.width if extraction.paginated else None,
                    page_height=chunk.page.height if extraction.paginated else None,
                    coordinate_space=chunk.page.coordinate_space if extraction.paginated else None,
                    source_regions=chunk.lines,
                ),
            )
            for index, (chunk, vector) in enumerate(zip(chunks, vectors, strict=True))
        ],
    )


def build_chunk_vectors(
    document: Document,
    version: DocumentVersion,
    embedding: EmbeddingProvider,
    extractor: DocumentExtractor | None = None,
) -> list[tuple[list[float], VectorPayload]]:
    """Vectors of one version; content without indexable text fails closed."""
    built = build_chunks(document, version, embedding, extractor)
    if not built.vectors:
        raise IngestionContentError(
            "ocr_review_required" if built.extraction.review_required else "empty_document"
        )
    return built.vectors


def record_extraction(session: Session, version: DocumentVersion, built: ChunkBuild) -> None:
    """Replace this version's page report and chunk provenance (reprocessing is idempotent)."""
    for model in (DocumentChunkSource, DocumentPageExtraction):
        session.execute(
            delete(model).where(
                model.tenant_id == version.tenant_id,
                model.document_version_id == version.id,
            )
        )
    if not built.extraction.paginated:
        return
    for page in built.extraction.pages:
        if page.page_number is None:
            continue
        session.add(
            DocumentPageExtraction(
                tenant_id=version.tenant_id,
                document_version_id=version.id,
                page_number=page.page_number,
                classification=page.classification.value,
                extraction_method=page.method.value,
                schema_version=built.extraction.schema_version,
                coordinate_space=page.coordinate_space,
                width=page.width,
                height=page.height,
                unit=page.unit,
                engine=page.engine,
                engine_version=page.engine_version,
                config_version=page.config_version,
                language=page.language,
                quality_state=page.quality.state,
                mean_confidence=page.quality.mean_confidence,
                low_confidence_ratio=page.quality.low_confidence_ratio,
                implausible_ratio=page.quality.implausible_ratio,
                char_count=page.quality.char_count,
                word_count=page.quality.word_count,
                flags=list(page.quality.flags),
                transformations=list(page.transformations),
                regions=[box.as_list() for box in page.ocr_regions],
                lines=[
                    {
                        "line": line.index,
                        "box": line.box.as_list(),
                        "confidence": line.confidence,
                        "words": line.word_count,
                    }
                    for line in page.lines
                ],
                ocr_milliseconds=round(page.ocr_seconds * 1000),
            )
        )
    for index, chunk in enumerate(built.chunks):
        boxes = {line.index: line.box for line in chunk.page.lines}
        session.add(
            DocumentChunkSource(
                tenant_id=version.tenant_id,
                document_version_id=version.id,
                chunk_index=index,
                chunk_id=f"{version.id}:{index}",
                page_number=chunk.page.page_number,
                extraction_method=chunk.method,
                regions=[
                    {"line": line, "box": boxes[line].as_list()}
                    for line in chunk.lines
                    if line in boxes
                ],
            )
        )
    session.flush()


class IngestionWorker:
    def __init__(
        self,
        session: Session,
        vector_index: VectorIndex | None = None,
        worker_id: str | None = None,
        max_attempts: int = 3,
        audit: Callable[[str, IngestionJob], None] | None = None,
        embedding: EmbeddingProvider | None = None,
        extractor: DocumentExtractor | None = None,
    ) -> None:
        self.session = session
        self.extractor = extractor
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
        review = False
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
                        built = build_chunks(document, version, self.embedding, self.extractor)
                    except IngestionContentError as exc:
                        document.status = "failed"
                        return self._fail(job, exc.code, retryable=False)
                    record_extraction(self.session, version, built)
                    vectors = built.vectors
                    review = built.extraction.review_required
                    if not vectors:
                        document.status = "failed"
                        version.ingestion_status = "review_required" if review else "failed"
                        code = "ocr_review_required" if review else "empty_document"
                        return self._fail(job, code, retryable=False)
                if stage == "indexing":
                    self._index(tenant_id, document, version, vectors)
                    indexed = True
            document.status = "indexed"
            # Some pages could not be read reliably: indexed, but never presented as complete.
            version.ingestion_status = "review_required" if review else "indexed"
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
