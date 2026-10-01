"""Live Qdrant: worker ingestion and tenant-isolated retrieval on a real server.

Skipped unless RAGOPS_TEST_QDRANT_URL points at a disposable Qdrant; the test creates and
deletes its own collection and uses only synthetic text and the local embedding provider.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest

pytest.importorskip("qdrant_client", reason="install the pinned vector extra")
pytest.importorskip("alembic", reason="Install declared persistence extra; product gate blocks")

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from ragops.auth.identity import AuthenticatedUserContext
from ragops.knowledge.service import KnowledgeManagementService, LocalDocumentBlobStore
from ragops.persistence.database import transaction
from ragops.persistence.models import Tenant
from ragops.retrieval.vector_retriever import RetrievalUnavailable, VectorRetriever
from ragops.vector.embedding import EMBEDDING_MODEL, DeterministicEmbeddingProvider
from ragops.vector.index import QdrantVectorIndex, VectorPayload
from ragops.workers.worker import IngestionWorker

pytestmark = pytest.mark.integration
URL = os.getenv("RAGOPS_TEST_QDRANT_URL")
DIMENSION = 64
TEXT = " ".join(
    f"Abschnitt {number}: Das Orbit-Verfahren {number} beschreibt die Freigabe von Wartungen."
    for number in range(50)
)


def context(tenant: str, role: str = "viewer") -> AuthenticatedUserContext:
    return AuthenticatedUserContext(user_id="u", tenant_id=tenant, roles=(role,))  # type: ignore[arg-type]


@pytest.fixture
def index() -> Iterator[QdrantVectorIndex]:
    if not URL:
        pytest.skip("RAGOPS_TEST_QDRANT_URL is not configured")
    live = QdrantVectorIndex(
        URL, f"ragops_test_ent11_{uuid4().hex[:10]}", DIMENSION, timeout_seconds=5
    )
    live.ensure_schema()
    try:
        yield live
    finally:
        live.client.delete_collection(live.collection)
        assert not live.client.collection_exists(live.collection)
        live.client.close()


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    database = create_engine(f"sqlite:///{tmp_path / 'live.db'}")
    with database.begin() as connection:
        cfg = Config("alembic.ini")
        cfg.attributes["connection"] = connection
        command.upgrade(cfg, "head")
    yield database
    database.dispose()


def ingest(engine: Engine, index: QdrantVectorIndex, tmp_path: Path, tenant: str) -> str:
    with transaction(engine) as session:
        session.add(Tenant(id=tenant, name=tenant))
        session.flush()
        service = KnowledgeManagementService(
            session, tenant, "user", LocalDocumentBlobStore(tmp_path / "blobs")
        )
        workspace = service.create_workspace("W")
        collection = service.create_collection(workspace.id, "C")
        result = service.upload(
            workspace.id,
            collection.id,
            "Orbit Handbuch",
            "orbit",
            "orbit.txt",
            "text/plain",
            TEXT.encode(),
        )
        job = IngestionWorker(session).create_job(tenant, result.document.id, result.version.id)
        job_id, document_id = job.id, str(result.document.id)
    with Session(engine) as session:
        done = IngestionWorker(session, vector_index=index).process(job_id, tenant)
        session.commit()
        assert done.status == "completed"
    return document_id


def points(index: QdrantVectorIndex) -> list:
    return index.client.scroll(index.collection, limit=1000, with_payload=True, with_vectors=True)[
        0
    ]


def test_worker_chunks_are_real_and_isolated_on_live_qdrant(index, engine, tmp_path) -> None:
    documents = {
        tenant: ingest(engine, index, tmp_path, tenant) for tenant in ("tenant-a", "tenant-b")
    }
    stored = points(index)
    assert len(stored) >= 6
    for point in stored:
        assert any(value != 0.0 for value in point.vector)
        assert point.payload["chunk_text"] and point.payload["embedding_model"] == EMBEDDING_MODEL
        assert point.payload["document_id"] and point.payload["workspace_id"]
    by_tenant = {
        tenant: sorted(
            (p.payload["chunk_index"], p.vector) for p in stored if p.payload["tenant_id"] == tenant
        )
        for tenant in documents
    }
    assert [v for _, v in by_tenant["tenant-a"]] == [v for _, v in by_tenant["tenant-b"]]
    retriever = VectorRetriever(index, DeterministicEmbeddingProvider(DIMENSION))
    own_chunks = len(by_tenant["tenant-a"])
    for tenant, document in documents.items():
        hits = retriever.retrieve(context(tenant), "Was beschreibt das Orbit-Verfahren 7?", 20)
        assert len(hits) == min(own_chunks, 20)
        assert {(hit.tenant_id, hit.document_id) for hit in hits} == {(tenant, document)}
        assert all("Orbit-Verfahren" in hit.text for hit in hits)
    assert retriever.retrieve(context("tenant-c"), "Orbit-Verfahren", 20) == []


def test_live_filter_enforces_validity_status_and_access(index) -> None:
    embedding = DeterministicEmbeddingProvider(DIMENSION)
    now = datetime.now(UTC)
    vector = embedding.embed(["Orbit Freigabe Wartung"])[0]

    def payload(name: str, **changes: object) -> VectorPayload:
        values: dict[str, object] = {
            "tenant_id": "tenant-a",
            "workspace_id": "w",
            "collection_id": "c",
            "document_id": name,
            "document_version_id": f"{name}-v",
            "chunk_id": f"{name}-v:0",
            "access_level": "internal",
            "document_status": "indexed",
            "version_status": "indexed",
            "content_hash": name,
            "chunk_index": 0,
            "source_name": "orbit.txt",
            "created_at": now.isoformat(),
            "valid_from": (now - timedelta(minutes=5)).isoformat(),
            "valid_to": None,
            "title": "Orbit",
            "page_number": None,
            "chunk_text": "Orbit Freigabe Wartung",
            "embedding_model": EMBEDDING_MODEL,
        }
        values.update(changes)
        return VectorPayload(**values)  # type: ignore[arg-type]

    index.upsert_chunks(
        [
            (vector, payload("open-ended")),
            (vector, payload("still-valid", valid_to=(now + timedelta(days=1)).isoformat())),
            (vector, payload("expired", valid_to=(now - timedelta(seconds=30)).isoformat())),
            (vector, payload("future", valid_from=(now + timedelta(days=1)).isoformat())),
            (vector, payload("processing", document_status="processing")),
            (vector, payload("superseded", version_status="superseded")),
            (vector, payload("restricted", access_level="restricted")),
            (vector, payload("foreign", tenant_id="tenant-b")),
        ]
    )
    retriever = VectorRetriever(index, embedding)
    viewer = {hit.document_id for hit in retriever.retrieve(context("tenant-a"), "Orbit", 20)}
    assert viewer == {"open-ended", "still-valid"}
    compliance = retriever.retrieve(context("tenant-a", "compliance"), "Orbit", 20)
    assert {hit.document_id for hit in compliance} == {"open-ended", "still-valid", "restricted"}


def test_unreachable_qdrant_fails_closed() -> None:
    dead = QdrantVectorIndex("http://127.0.0.1:9", "ragops_test_dead", DIMENSION, timeout_seconds=1)
    retriever = VectorRetriever(dead, DeterministicEmbeddingProvider(DIMENSION))
    with pytest.raises(RetrievalUnavailable):
        retriever.retrieve(context("tenant-a"), "Orbit", 5)
    dead.client.close()


def test_productive_query_endpoint_answers_from_live_qdrant(index, engine, tmp_path) -> None:
    """The real app in vector mode builds its own Qdrant index from settings."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from fastapi.testclient import TestClient
    from tests.security.oidc_helpers import AUDIENCE, ISSUER, bearer

    from ragops.api.app import create_app
    from ragops.config.settings import Settings
    from ragops.llm.providers import DeterministicTestProvider

    documents = {
        tenant: ingest(engine, index, tmp_path, tenant) for tenant in ("tenant-a", "tenant-b")
    }
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = private.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()
    public_pem = (
        private.public_key()
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        .decode()
    )
    settings = Settings(
        environment="test",
        identity_provider="oidc",
        oidc_issuer=ISSUER,
        oidc_audience=AUDIENCE,
        oidc_public_key=public_pem,
        evidence_dir=tmp_path / "evidence",
        data_dir=tmp_path / "no-demo-corpus",
        vector_provider="qdrant",
        qdrant_url=URL,
        qdrant_collection=index.collection,
        embedding_dimension=DIMENSION,
    )
    calls: list[str] = []

    class Counting(DeterministicTestProvider):
        def generate(self, question, citations, context):
            calls.append(context)
            return super().generate(question, citations, context)

    with TestClient(create_app(settings, llm_provider=Counting())) as client:
        for tenant, document in documents.items():
            response = client.post(
                "/v1/query",
                json={"question": "Was beschreibt das Orbit-Verfahren 7?", "tenant_id": "tenant-x"},
                headers=bearer(private_pem, tenant, ["viewer"]),
            )
            assert response.status_code == 200
            body = response.json()
            assert body["abstained"] is False and body["citations"]
            assert {(c["tenant_id"], c["document_id"]) for c in body["citations"]} == {
                (tenant, document)
            }
        empty = client.post(
            "/v1/query",
            json={"question": "Was beschreibt das Orbit-Verfahren 7?"},
            headers=bearer(private_pem, "tenant-c", ["viewer"]),
        )
        assert empty.status_code == 200 and empty.json()["abstained"] is True
        assert empty.json()["citations"] == []
    assert len(calls) == 2 and all("Orbit-Verfahren" in context for context in calls)
