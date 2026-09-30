"""The ingestion worker writes real, complete, retrievable chunks - never placeholders."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

pytest.importorskip("alembic", reason="Install declared persistence extra; product gate blocks")

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from ragops.auth.identity import AuthenticatedUserContext
from ragops.ingestion.extract import DocumentExtractionError, extract_segments
from ragops.ingestion.normalization import chunk_words
from ragops.knowledge.service import KnowledgeManagementService, LocalDocumentBlobStore
from ragops.persistence.database import transaction
from ragops.persistence.models import Document, DocumentVersion, IngestionJob, Tenant
from ragops.retrieval.vector_retriever import VectorRetriever
from ragops.vector.embedding import (
    EMBEDDING_MODEL,
    DeterministicEmbeddingProvider,
    embedding_provider_for,
)
from ragops.vector.index import DeterministicVectorIndex, VectorPayload
from ragops.workers.worker import IngestionWorker, build_chunk_vectors

DIMENSION = 64
LONG_TEXT = " ".join(
    f"Absatz {number}: Das Nexus-Protokoll {number} regelt die Eskalation kritischer Vorfälle."
    for number in range(60)
)


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    database = create_engine(f"sqlite:///{tmp_path / 'ingestion.db'}")
    with database.begin() as connection:
        cfg = Config("alembic.ini")
        cfg.attributes["connection"] = connection
        command.upgrade(cfg, "head")
    yield database
    database.dispose()


def upload(engine: Engine, tmp_path: Path, tenant: str, content: bytes, name: str = "doc.txt"):
    with transaction(engine) as session:
        if session.get(Tenant, tenant) is None:
            session.add(Tenant(id=tenant, name=tenant))
            session.flush()
        service = KnowledgeManagementService(
            session, tenant, "user", LocalDocumentBlobStore(tmp_path / "blobs")
        )
        workspace = service.create_workspace(f"W-{tenant}")
        collection = service.create_collection(workspace.id, "C")
        mime = "application/pdf" if name.endswith(".pdf") else "text/plain"
        result = service.upload(
            workspace.id, collection.id, "Nexus Handbuch", "nexus", name, mime, content
        )
        job = IngestionWorker(session).create_job(tenant, result.document.id, result.version.id)
        return job.id, result.document.id, result.version.id


def process(engine: Engine, index, tenant: str, job_id):
    with Session(engine) as session:
        job = IngestionWorker(session, vector_index=index).process(job_id, tenant)
        session.commit()
        return job.status, job.error_code


def payloads(index: DeterministicVectorIndex) -> list[VectorPayload]:
    return sorted(
        (payload for _, payload in index._records.values()),  # noqa: SLF001
        key=lambda item: item.chunk_index,
    )


def test_worker_writes_several_real_chunks_with_complete_metadata(engine, tmp_path) -> None:
    index = DeterministicVectorIndex(DIMENSION)
    job_id, document_id, version_id = upload(engine, tmp_path, "tenant-a", LONG_TEXT.encode())
    assert process(engine, index, "tenant-a", job_id) == ("completed", None)
    stored = payloads(index)
    assert len(stored) >= 3
    assert [item.chunk_index for item in stored] == list(range(len(stored)))
    with Session(engine) as session:
        document = session.get(Document, ("tenant-a", document_id))
        version = session.get(DocumentVersion, ("tenant-a", version_id))
        assert (document.status, version.ingestion_status) == ("indexed", "indexed")
        for item in stored:
            assert item.tenant_id == "tenant-a"
            assert item.workspace_id == str(document.workspace_id)
            assert item.collection_id == str(document.collection_id)
            assert item.document_id == str(document_id)
            assert item.document_version_id == str(version_id)
            assert item.chunk_id == f"{version_id}:{item.chunk_index}"
            assert item.access_level == document.access_level
            assert (item.document_status, item.version_status) == ("indexed", "indexed")
            assert item.source_name == "doc.txt" and item.title == "Nexus Handbuch"
            assert item.embedding_model == EMBEDDING_MODEL
            assert item.chunk_text and item.chunk_text in LONG_TEXT
            assert len(item.content_hash) == 64 and item.valid_from and item.valid_to is None
    assert len({item.content_hash for item in stored}) == len(stored)


def test_worker_never_writes_null_vector_placeholders(engine, tmp_path) -> None:
    index = DeterministicVectorIndex(DIMENSION)
    job_id, _, _ = upload(engine, tmp_path, "tenant-a", LONG_TEXT.encode())
    process(engine, index, "tenant-a", job_id)
    for vector, payload in index._records.values():  # noqa: SLF001
        assert any(value != 0.0 for value in vector) and len(vector) == DIMENSION
        assert payload.document_id and payload.workspace_id and payload.collection_id
        assert payload.chunk_text
    assert not hasattr(index, "upsert")
    assert not hasattr(IngestionWorker(None).vector_index, "upsert")  # type: ignore[arg-type]


def test_ingested_chunks_are_retrievable_with_the_query_embedding(engine, tmp_path) -> None:
    index = DeterministicVectorIndex(DIMENSION)
    for tenant in ("tenant-a", "tenant-b"):
        job_id, _, _ = upload(engine, tmp_path, tenant, LONG_TEXT.encode())
        process(engine, index, tenant, job_id)
    retriever = VectorRetriever(index, DeterministicEmbeddingProvider(DIMENSION))
    context = AuthenticatedUserContext(user_id="u", tenant_id="tenant-a", roles=("viewer",))
    hits = retriever.retrieve(context, "Was regelt das Nexus-Protokoll 17 zur Eskalation?", 5)
    assert hits and {hit.tenant_id for hit in hits} == {"tenant-a"}
    assert "Nexus-Protokoll" in hits[0].text and hits[0].title == "Nexus Handbuch"


def test_reprocessing_is_idempotent_and_a_new_version_replaces_the_old_chunks(
    engine, tmp_path
) -> None:
    index = DeterministicVectorIndex(DIMENSION)
    job_id, document_id, first_version = upload(engine, tmp_path, "tenant-a", LONG_TEXT.encode())
    process(engine, index, "tenant-a", job_id)
    count = index.count()
    with Session(engine) as session:
        vectors = build_chunk_vectors(
            session.get(Document, ("tenant-a", document_id)),
            session.get(DocumentVersion, ("tenant-a", first_version)),
            DeterministicEmbeddingProvider(DIMENSION),
        )
    index.upsert_chunks(vectors)
    assert index.count() == count
    with transaction(engine) as session:
        service = KnowledgeManagementService(
            session, "tenant-a", "user", LocalDocumentBlobStore(tmp_path / "blobs")
        )
        document = session.get(Document, ("tenant-a", document_id))
        result = service.upload(
            document.workspace_id,
            document.collection_id,
            "Nexus Handbuch",
            "nexus",
            "doc.txt",
            "text/plain",
            b"Neue Fassung: Das Nexus-Protokoll gilt ab sofort in Version zwei.",
        )
        second_job = (
            IngestionWorker(session).create_job("tenant-a", document_id, result.version.id).id
        )
        second_version = result.version.id
    assert process(engine, index, "tenant-a", second_job) == ("completed", None)
    assert {item.document_version_id for item in payloads(index)} == {str(second_version)}


@pytest.mark.parametrize(
    ("content", "code"), [(b"   \n\t  ", "empty_document"), (b"?!... --- ***", "empty_document")]
)
def test_a_document_without_indexable_text_fails_the_job_and_writes_nothing(
    engine, tmp_path, content, code
) -> None:
    index = DeterministicVectorIndex(DIMENSION)
    job_id, document_id, _ = upload(engine, tmp_path, "tenant-a", content)
    assert process(engine, index, "tenant-a", job_id) == ("failed", code)
    assert index.count() == 0
    with Session(engine) as session:
        assert session.get(Document, ("tenant-a", document_id)).status != "indexed"
        job = session.scalar(select(IngestionJob).where(IngestionJob.id == job_id))
        assert job.error_message_redacted == "ingestion failed"


def test_missing_mandatory_metadata_fails_closed(engine, tmp_path) -> None:
    job_id, document_id, version_id = upload(engine, tmp_path, "tenant-a", LONG_TEXT.encode())
    embedding = DeterministicEmbeddingProvider(DIMENSION)
    with Session(engine) as session:
        document = session.get(Document, ("tenant-a", document_id))
        version = session.get(DocumentVersion, ("tenant-a", version_id))
        for target, field, value in (
            (document, "access_level", "secret"),
            (document, "workspace_id", None),
            (version, "filename", ""),
            (version, "tenant_id", "tenant-b"),
        ):
            original = getattr(target, field)
            with session.no_autoflush:
                setattr(target, field, value)
                with pytest.raises(ValueError):
                    build_chunk_vectors(document, version, embedding)
                setattr(target, field, original)
        session.rollback()


def test_index_failure_leaves_no_vectors_and_stays_retryable(engine, tmp_path) -> None:
    class Failing(DeterministicVectorIndex):
        def upsert_chunks(self, vectors):
            super().upsert_chunks(vectors[:1])
            raise ConnectionError("qdrant down")

    index = Failing(DIMENSION)
    job_id, _, _ = upload(engine, tmp_path, "tenant-a", LONG_TEXT.encode())
    with Session(engine) as session, pytest.raises(ConnectionError):
        IngestionWorker(session, vector_index=index).process(job_id, "tenant-a")
    assert index.count() == 0


def test_index_rejects_a_chunk_without_a_tenant() -> None:
    index = DeterministicVectorIndex(DIMENSION)
    payload = VectorPayload(
        "", "w", "c", "d", "v", "v:0", "internal", "indexed", "indexed", "h", 0, "n", "t", "t"
    )
    with pytest.raises(ValueError):
        index.upsert_chunks([([1.0] + [0.0] * (DIMENSION - 1), payload)])
    assert index.count() == 0


def test_pdf_pages_keep_their_page_number() -> None:
    from scripts.browser_e2e_gate import synthetic_pdf

    segments = extract_segments("bericht.pdf", synthetic_pdf("Nexus Bericht Seite eins"))
    assert segments == [(1, "Nexus Bericht Seite eins")]
    assert extract_segments("notiz.txt", b"Hallo  Welt\n") == [(None, "Hallo  Welt\n")]
    for name, content in (("x.exe", b"MZ"), ("kaputt.pdf", b"%PDF-1.4 broken"), ("x.txt", b"\xff")):
        with pytest.raises(DocumentExtractionError):
            extract_segments(name, content)


def test_chunks_keep_the_original_wording_and_overlap() -> None:
    words = [f"Wort{number}," for number in range(250)]
    chunks = chunk_words(" ".join(words), chunk_size=120, overlap=20)
    assert [len(chunk.split()) for chunk in chunks] == [120, 120, 50]
    assert chunks[0].split()[0] == "Wort0," and chunks[1].split()[0] == "Wort100,"
    assert chunk_words("  \n ") == []
    with pytest.raises(ValueError):
        chunk_words("a b", chunk_size=5, overlap=5)


def test_embedding_contract_is_explicit_and_shared_by_query_and_documents() -> None:
    provider = DeterministicEmbeddingProvider(DIMENSION)
    assert provider.model == EMBEDDING_MODEL == "deterministic-hash-v1"
    document, query = provider.embed(["Nexus-Protokoll: Eskalation!", "nexus protokoll eskalation"])
    assert document == query and len(document) == DIMENSION
    assert provider.embed(["?!"])[0] == [0.0] * DIMENSION

    class Configured:
        embedding_provider, embedding_model, embedding_dimension = (
            "deterministic",
            EMBEDDING_MODEL,
            DIMENSION,
        )

    assert embedding_provider_for(Configured()).dimension == DIMENSION
    for provider_name, model in (("openai", EMBEDDING_MODEL), ("deterministic", "other-v1")):
        Configured.embedding_provider, Configured.embedding_model = provider_name, model
        with pytest.raises(RuntimeError):
            embedding_provider_for(Configured())
