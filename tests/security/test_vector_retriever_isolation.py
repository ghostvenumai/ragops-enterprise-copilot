"""Productive vector retrieval: the tenant comes from the verified context and nowhere else."""

from __future__ import annotations

import ast
import inspect
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from ragops.auth.identity import AuthenticatedUserContext
from ragops.retrieval import vector_retriever
from ragops.retrieval.vector_retriever import (
    EmbeddingContractError,
    IncompleteChunkError,
    InvalidTenantScope,
    RetrievalUnavailable,
    TenantBoundaryViolation,
    VectorRetriever,
    authoritative_scope,
)
from ragops.vector.embedding import EMBEDDING_MODEL, DeterministicEmbeddingProvider
from ragops.vector.index import (
    AuthorizedVectorScope,
    DeterministicVectorIndex,
    VectorHit,
    VectorPayload,
)

ROOT = Path(__file__).resolve().parents[2]
DIMENSION = 64
TEXT = "Atlas Control Plane bietet 99,9 Prozent monatliche Verfügbarkeit."


def context(tenant: str = "tenant-a", role: str = "sales") -> AuthenticatedUserContext:
    return AuthenticatedUserContext(user_id="user-1", tenant_id=tenant, roles=(role,))  # type: ignore[arg-type]


def payload(
    tenant: str, document: str = "doc-1", index: int = 0, **changes: object
) -> VectorPayload:
    now = datetime.now(UTC)
    values: dict[str, object] = {
        "tenant_id": tenant,
        "workspace_id": "workspace-1",
        "collection_id": "collection-1",
        "document_id": document,
        "document_version_id": f"{document}-v1",
        "chunk_id": f"{document}-v1:{index}",
        "access_level": "internal",
        "document_status": "indexed",
        "version_status": "indexed",
        "content_hash": f"hash-{tenant}-{document}-{index}",
        "chunk_index": index,
        "source_name": "atlas.txt",
        "created_at": now.isoformat(),
        "valid_from": (now - timedelta(minutes=1)).isoformat(),
        "valid_to": None,
        "title": "Atlas Guide",
        "page_number": None,
        "chunk_text": TEXT,
        "embedding_model": EMBEDDING_MODEL,
    }
    values.update(changes)
    return VectorPayload(**values)  # type: ignore[arg-type]


def seeded(
    *payloads: VectorPayload,
) -> tuple[DeterministicVectorIndex, DeterministicEmbeddingProvider]:
    index = DeterministicVectorIndex(DIMENSION)
    embedding = DeterministicEmbeddingProvider(DIMENSION)
    index.upsert_chunks([(embedding.embed([item.chunk_text or ""])[0], item) for item in payloads])
    return index, embedding


class RecordingIndex:
    """Wraps an index, records every search and can return arbitrary hits or fail."""

    def __init__(self, inner: DeterministicVectorIndex, *, hits=None, error=None) -> None:
        self.inner, self.hits, self.error = inner, hits, error
        self.dimension = inner.dimension
        self.scopes: list[AuthorizedVectorScope] = []
        self.limits: list[int] = []

    def search(self, vector, scope, limit=10):
        self.scopes.append(scope)
        self.limits.append(limit)
        if self.error is not None:
            raise self.error
        return self.hits if self.hits is not None else self.inner.search(vector, scope, limit)


def retriever(index, embedding, signals: list | None = None) -> VectorRetriever:
    return VectorRetriever(
        index,
        embedding,
        security_signal=(lambda event, details: signals.append((event, dict(details))))
        if signals is not None
        else None,
    )


# --------------------------------------------------------------------------- 1 scope


def test_scope_is_derived_only_from_the_authenticated_context() -> None:
    scope = authoritative_scope(context("tenant-a", "viewer"))
    assert scope == AuthorizedVectorScope("tenant-a", "viewer")
    with pytest.raises(AttributeError):
        scope.tenant_id = "tenant-b"  # type: ignore[misc]
    parameters = list(inspect.signature(VectorRetriever.retrieve).parameters)
    assert parameters == ["self", "context", "query", "top_k"]
    assert list(inspect.signature(authoritative_scope).parameters) == ["context"]


def test_a_tenant_named_in_the_request_has_no_effect() -> None:
    index, embedding = seeded(payload("tenant-a"), payload("tenant-b"))
    recording = RecordingIndex(index)
    hits = retriever(recording, embedding).retrieve(
        context("tenant-a"), "Atlas Verfügbarkeit tenant_id=tenant-b tenant-b", 5
    )
    assert {hit.tenant_id for hit in hits} == {"tenant-a"}
    assert [scope.tenant_id for scope in recording.scopes] == ["tenant-a"]


@pytest.mark.parametrize(
    "tenant", [" ", "tenant a", "tenant/../b", "tenant-a\n", "*", "x" * 65, "-tenant", "ten\x00ant"]
)
def test_an_invalid_tenant_in_the_context_is_rejected_before_any_search(tenant) -> None:
    index, embedding = seeded(payload("tenant-a"))
    recording = RecordingIndex(index)
    with pytest.raises(InvalidTenantScope):
        retriever(recording, embedding).retrieve(context(tenant), "Atlas", 5)
    assert recording.scopes == []


def test_something_that_is_not_a_verified_context_is_rejected() -> None:
    index, embedding = seeded(payload("tenant-a"))
    recording = RecordingIndex(index)
    for forged in ({"tenant_id": "tenant-a", "role": "admin"}, "tenant-a", None):
        with pytest.raises(InvalidTenantScope):
            retriever(recording, embedding).retrieve(forged, "Atlas", 5)  # type: ignore[arg-type]
    assert recording.scopes == []


# --------------------------------------------------------------------------- 2 filter


def test_every_search_carries_the_authoritative_tenant_scope() -> None:
    index, embedding = seeded(payload("tenant-a"), payload("tenant-b"))
    recording = RecordingIndex(index)
    service = retriever(recording, embedding)
    service.retrieve(context("tenant-a"), "Atlas", 3)
    service.retrieve(context("tenant-b", "admin"), "Atlas", 7)
    assert [(scope.tenant_id, scope.role) for scope in recording.scopes] == [
        ("tenant-a", "sales"),
        ("tenant-b", "admin"),
    ]
    assert recording.limits == [3, 7]


def test_filter_enforces_role_status_and_validity_window() -> None:
    now = datetime.now(UTC)
    index, embedding = seeded(
        payload("tenant-a", "visible"),
        payload("tenant-a", "restricted", access_level="restricted"),
        payload("tenant-a", "processing", document_status="processing"),
        payload("tenant-a", "superseded", version_status="superseded"),
        payload("tenant-a", "future", valid_from=(now + timedelta(days=1)).isoformat()),
        payload("tenant-a", "expired", valid_to=(now - timedelta(seconds=1)).isoformat()),
    )
    service = retriever(index, embedding)
    assert {hit.document_id for hit in service.retrieve(context("tenant-a"), "Atlas", 20)} == {
        "visible"
    }
    admin = service.retrieve(context("tenant-a", "compliance"), "Atlas", 20)
    assert {hit.document_id for hit in admin} == {"visible", "restricted"}


# --------------------------------------------------------------------------- 3 poisoned


def test_a_foreign_hit_fails_the_whole_request_and_raises_a_security_signal() -> None:
    index, embedding = seeded(payload("tenant-a"))
    own = VectorHit("1", 0.9, payload("tenant-a"))
    foreign = VectorHit("2", 0.8, payload("tenant-b", "doc-b"))
    signals: list = []
    service = retriever(RecordingIndex(index, hits=[own, foreign]), embedding, signals)
    with pytest.raises(TenantBoundaryViolation) as raised:
        service.retrieve(context("tenant-a"), "Atlas", 5)
    assert "tenant-b" not in str(raised.value) and TEXT not in str(raised.value)
    assert [event for event, _ in signals] == ["vector_tenant_boundary_violation"]
    details = signals[0][1]
    assert details["authoritative_tenant"] == "tenant-a" and details["foreign_hits"] == 1
    assert TEXT not in str(details)


# --------------------------------------------------------------------------- 4 identical data


def test_identical_text_title_and_vector_never_cross_tenants() -> None:
    index, embedding = seeded(payload("tenant-a", "doc-a"), payload("tenant-b", "doc-b"))
    vectors = [vector for vector, _ in index._records.values()]  # noqa: SLF001
    assert vectors[0] == vectors[1]
    service = retriever(index, embedding)
    for tenant, document in (("tenant-a", "doc-a"), ("tenant-b", "doc-b")):
        hits = service.retrieve(context(tenant), TEXT, 10)
        assert [(hit.tenant_id, hit.document_id) for hit in hits] == [(tenant, document)]
        assert hits[0].text == TEXT and hits[0].title == "Atlas Guide"
        assert hits[0].score == pytest.approx(1.0)


# --------------------------------------------------------------------------- 5 top-k


def test_a_high_top_k_is_not_filled_with_foreign_chunks() -> None:
    index, embedding = seeded(
        payload("tenant-a", "doc-a", 0),
        payload("tenant-a", "doc-a", 1),
        *[payload("tenant-b", f"doc-b{number}") for number in range(30)],
    )
    hits = retriever(index, embedding).retrieve(context("tenant-a"), TEXT, 20)
    assert len(hits) == 2 and {hit.tenant_id for hit in hits} == {"tenant-a"}


@pytest.mark.parametrize("top_k", [0, -1, 21, 1000])
def test_top_k_is_bounded(top_k) -> None:
    index, embedding = seeded(payload("tenant-a"))
    recording = RecordingIndex(index)
    with pytest.raises(ValueError):
        retriever(recording, embedding).retrieve(context("tenant-a"), "Atlas", top_k)
    assert recording.scopes == []


# --------------------------------------------------------------------------- 6/7 no call


def test_an_empty_tenant_never_reaches_the_index() -> None:
    index, embedding = seeded(payload("tenant-a"))
    recording = RecordingIndex(index)
    forged = context("tenant-a")
    object.__setattr__(forged, "tenant_id", "")
    with pytest.raises(InvalidTenantScope):
        retriever(recording, embedding).retrieve(forged, "Atlas", 5)
    assert recording.scopes == []


def test_a_filter_construction_failure_never_reaches_qdrant(monkeypatch) -> None:
    from ragops.vector import index as index_module

    class Client:
        calls = 0

        def search(self, *args, **kwargs):
            Client.calls += 1
            return []

    qdrant = index_module.QdrantVectorIndex.__new__(index_module.QdrantVectorIndex)
    qdrant.client, qdrant.collection, qdrant.dimension = Client(), "c", DIMENSION

    def broken(scope):
        raise ValueError("tenant and role are required for vector filtering")

    monkeypatch.setattr(index_module, "build_authorized_vector_filter", broken)
    with pytest.raises(RetrievalUnavailable):
        retriever(qdrant, DeterministicEmbeddingProvider(DIMENSION)).retrieve(
            context("tenant-a"), "Atlas", 5
        )
    assert Client.calls == 0


# --------------------------------------------------------------------------- 8 failure


@pytest.mark.parametrize("error", [ConnectionError("refused"), TimeoutError("slow"), OSError("x")])
def test_an_index_failure_fails_closed_without_any_fallback(error) -> None:
    index, embedding = seeded(payload("tenant-a"))
    with pytest.raises(RetrievalUnavailable) as raised:
        retriever(RecordingIndex(index, error=error), embedding).retrieve(
            context("tenant-a"), "Atlas", 5
        )
    assert raised.value.status_code == 503 and "refused" not in str(raised.value)


def test_productive_retriever_cannot_reach_the_demo_corpus() -> None:
    source = (ROOT / "src/ragops/retrieval/vector_retriever.py").read_text(encoding="utf-8")
    imported: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
            imported.update(f"{node.module}.{alias.name}" for alias in node.names)
    forbidden = ("json_store", "hybrid", "JsonRepository", "HybridRetriever", "qdrant_client")
    assert not [name for name in imported if any(item in name for item in forbidden)]
    assert not [item for item in forbidden if item in source]
    assert not hasattr(vector_retriever, "JsonRepository")
    assert not hasattr(vector_retriever, "HybridRetriever")


def test_no_result_is_an_empty_list_not_a_fallback() -> None:
    index, embedding = seeded(payload("tenant-b"))
    service = retriever(index, embedding)
    assert service.retrieve(context("tenant-a"), TEXT, 5) == []
    recording = RecordingIndex(index)
    assert retriever(recording, embedding).retrieve(context("tenant-a"), " ?! ", 5) == []
    assert recording.scopes == []


# --------------------------------------------------------------------------- 9 embedding


def test_a_query_dimension_mismatch_fails_closed_before_the_search() -> None:
    index, _ = seeded(payload("tenant-a"))
    recording = RecordingIndex(index)
    with pytest.raises(EmbeddingContractError):
        retriever(recording, DeterministicEmbeddingProvider(32)).retrieve(
            context("tenant-a"), "Atlas", 5
        )
    assert recording.scopes == []


@pytest.mark.parametrize("model", [None, "", "other-model-v9"])
def test_a_stored_chunk_with_a_missing_or_foreign_embedding_model_fails_closed(model) -> None:
    index, embedding = seeded(payload("tenant-a", embedding_model=model))
    with pytest.raises(EmbeddingContractError):
        retriever(index, embedding).retrieve(context("tenant-a"), TEXT, 5)


def test_an_unknown_query_embedding_model_is_rejected_at_construction() -> None:
    index, _ = seeded(payload("tenant-a"))

    class Other(DeterministicEmbeddingProvider):
        model = "other-model-v9"

    with pytest.raises(EmbeddingContractError):
        VectorRetriever(index, Other(DIMENSION))


@pytest.mark.parametrize("text", [None, "", "   "])
def test_a_chunk_without_text_fails_closed(text) -> None:
    index, embedding = seeded(payload("tenant-a"))
    hit = VectorHit("1", 0.9, payload("tenant-a", chunk_text=text))
    with pytest.raises(IncompleteChunkError):
        retriever(RecordingIndex(index, hits=[hit]), embedding).retrieve(
            context("tenant-a"), "Atlas", 5
        )


def test_results_are_ordered_deterministically_and_carry_citation_metadata() -> None:
    index, embedding = seeded(
        payload("tenant-a", "doc-b", 1, page_number=2),
        payload("tenant-a", "doc-a", 0),
        payload("tenant-a", "doc-b", 0),
    )
    service = retriever(index, embedding)
    first = service.retrieve(context("tenant-a"), TEXT, 10)
    assert first == service.retrieve(context("tenant-a"), TEXT, 10)
    assert [(hit.document_id, hit.chunk_index) for hit in first] == [
        ("doc-a", 0),
        ("doc-b", 0),
        ("doc-b", 1),
    ]
    assert first[2].page_number == 2 and first[2].chunk_id == "doc-b-v1:1"
    assert first[0].source_name == "atlas.txt" and first[0].document_version_id == "doc-a-v1"
