from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

from ragops.config.settings import Settings
from ragops.vector.embedding import DeterministicEmbeddingProvider
from ragops.vector.index import (
    AuthorizedVectorScope,
    DeterministicVectorIndex,
    VectorPayload,
    build_authorized_vector_filter,
    deterministic_vector_id,
)


def payload(
    tenant: str,
    version: str = "v1",
    *,
    access: str = "internal",
    status: str = "indexed",
    valid_to: str | None = None,
) -> VectorPayload:
    now = datetime.now(UTC)
    return VectorPayload(
        tenant,
        "w1",
        "c1",
        "d1",
        version,
        f"{version}:0",
        access,
        status,
        "indexed",
        "hash-" + version,
        0,
        "doc.txt",
        now.isoformat(),
        now.isoformat(),
        valid_to,
        "Document",
    )  # type: ignore[arg-type]


def test_deterministic_ids_and_upsert_are_idempotent() -> None:
    index = DeterministicVectorIndex()
    item = payload("tenant-a")
    vector = [1.0] + [0.0] * 63
    assert deterministic_vector_id(item) == deterministic_vector_id(item)
    UUID(deterministic_vector_id(item))
    index.upsert_chunks([(vector, item), (vector, item)])
    assert index.count() == 1


def test_filter_enforces_tenant_scope_access_lifecycle_and_validity() -> None:
    index = DeterministicVectorIndex()
    vector = [1.0] + [0.0] * 63
    expired = (datetime.now(UTC) - timedelta(minutes=1)).isoformat()
    index.upsert_chunks(
        [
            (vector, payload("tenant-a")),
            (vector, payload("tenant-b")),
            (vector, payload("tenant-a", "v2", status="inactive")),
            (vector, payload("tenant-a", "v3", access="restricted")),
            (vector, payload("tenant-a", "v4", valid_to=expired)),
        ]
    )
    scope = AuthorizedVectorScope("tenant-a", "viewer")
    hits = index.search(vector, scope)
    assert [hit.payload.document_version_id for hit in hits] == ["v1"]
    query_filter = build_authorized_vector_filter(scope)
    assert {condition["key"] for condition in query_filter["must"]} >= {
        "tenant_id",
        "document_status",
        "version_status",
        "access_level",
    }


def test_workspace_and_collection_scope_cannot_be_broadened() -> None:
    index = DeterministicVectorIndex()
    item = payload("tenant-a")
    index.upsert_chunks([([1.0] + [0.0] * 63, item)])
    assert (
        index.search(
            [1.0] + [0.0] * 63,
            AuthorizedVectorScope("tenant-a", "admin", workspace_ids=frozenset({"w2"})),
        )
        == []
    )
    assert index.search([1.0] + [0.0] * 63, AuthorizedVectorScope("tenant-b", "admin")) == []


def test_embedding_dimension_is_strict() -> None:
    provider = DeterministicEmbeddingProvider(8)
    assert len(provider.embed(["alpha"])[0]) == 8
    with pytest.raises(ValueError):
        DeterministicVectorIndex(8).upsert_chunks([([0.0] * 7, payload("tenant-a"))])


def test_production_vector_configuration_fails_closed() -> None:
    with pytest.raises(RuntimeError, match="VECTOR_PROVIDER"):
        Settings(environment="production").validate_vector_configuration()
    with pytest.raises(RuntimeError, match="QDRANT_URL"):
        Settings(vector_provider="qdrant").validate_vector_configuration()
