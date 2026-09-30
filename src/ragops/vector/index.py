"""Tenant-filtered vector index abstraction with deterministic and Qdrant adapters."""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol
from uuid import UUID

from ragops.auth.rbac import ROLE_MAX_ACCESS
from ragops.storage.models import AccessLevel, Role

VECTOR_SCHEMA_VERSION = "v1"


@dataclass(frozen=True)
class VectorPayload:
    tenant_id: str
    workspace_id: str
    collection_id: str
    document_id: str
    document_version_id: str
    chunk_id: str
    access_level: AccessLevel
    document_status: str
    version_status: str
    content_hash: str
    chunk_index: int
    source_name: str
    created_at: str
    valid_from: str
    valid_to: str | None = None
    title: str | None = None
    page_number: int | None = None
    # Additive in ENT-11: the text the chunk was embedded from and the embedding model.
    chunk_text: str | None = None
    embedding_model: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


@dataclass(frozen=True)
class AuthorizedVectorScope:
    tenant_id: str
    role: Role
    workspace_ids: frozenset[str] = frozenset()
    collection_ids: frozenset[str] = frozenset()


@dataclass(frozen=True)
class VectorHit:
    vector_id: str
    score: float
    payload: VectorPayload


class VectorIndex(Protocol):
    dimension: int

    def upsert_chunks(self, vectors: list[tuple[list[float], VectorPayload]]) -> int: ...
    def search(
        self, vector: list[float], scope: AuthorizedVectorScope, limit: int = 10
    ) -> list[VectorHit]: ...
    def delete_version_vectors(self, tenant_id: str, version_id: str) -> int: ...
    def delete_document_vectors(self, tenant_id: str, document_id: str) -> int: ...
    def ensure_schema(self) -> None: ...
    def health(self) -> bool: ...


def deterministic_vector_id(payload: VectorPayload) -> str:
    stable = ":".join(
        [
            payload.tenant_id,
            payload.document_version_id,
            str(payload.chunk_index),
            payload.content_hash,
        ]
    )
    # Qdrant accepts UUID or uint64 point IDs; map the stable digest to a
    # canonical UUID instead of sending an invalid 64-character point ID.
    digest = hashlib.sha256(stable.encode("utf-8")).digest()
    return str(UUID(bytes=digest[:16]))


def require_tenant_payloads(vectors: list[tuple[list[float], VectorPayload]]) -> None:
    """No point is ever written without the tenant it belongs to."""
    if any(
        not isinstance(payload.tenant_id, str) or not payload.tenant_id for _, payload in vectors
    ):
        raise ValueError("vector payload requires a tenant")


def build_authorized_vector_filter(scope: AuthorizedVectorScope) -> dict[str, Any]:
    """Build the mandatory server-side filter before vector search."""
    if not scope.tenant_id or not scope.role:
        raise ValueError("tenant and role are required for vector filtering")
    access = [
        level
        for level, rank in {"public": 0, "internal": 1, "confidential": 2, "restricted": 3}.items()
        if rank
        <= {"public": 0, "internal": 1, "confidential": 2, "restricted": 3}[
            ROLE_MAX_ACCESS[scope.role]
        ]
    ]
    must: list[dict[str, Any]] = [
        {"key": "tenant_id", "match": {"value": scope.tenant_id}},
        {"key": "document_status", "match": {"value": "indexed"}},
        {"key": "version_status", "match": {"value": "indexed"}},
        {"key": "access_level", "match": {"any": access}},
        {"key": "valid_from", "range": {"lte": datetime.now(UTC).isoformat()}},
    ]
    valid_now = datetime.now(UTC).isoformat()
    should = [
        {"is_empty": {"key": "valid_to"}},
        {"key": "valid_to", "range": {"gt": valid_now}},
    ]
    if scope.workspace_ids:
        must.append({"key": "workspace_id", "match": {"any": sorted(scope.workspace_ids)}})
    if scope.collection_ids:
        must.append({"key": "collection_id", "match": {"any": sorted(scope.collection_ids)}})
    return {"must": must, "should": should}


def _allowed(payload: VectorPayload, scope: AuthorizedVectorScope) -> bool:
    query = build_authorized_vector_filter(scope)
    values = {item["key"]: item["match"] for item in query["must"] if "match" in item}
    if payload.tenant_id != values["tenant_id"]["value"]:
        return False
    if payload.document_status != "indexed" or payload.version_status != "indexed":
        return False
    if payload.access_level not in values["access_level"]["any"]:
        return False
    now = datetime.now(UTC)
    try:
        if datetime.fromisoformat(payload.valid_from) > now:
            return False
        if payload.valid_to and datetime.fromisoformat(payload.valid_to) <= now:
            return False
    except ValueError:
        return False
    if scope.workspace_ids and payload.workspace_id not in scope.workspace_ids:
        return False
    return not scope.collection_ids or payload.collection_id in scope.collection_ids


class DeterministicVectorIndex:
    """In-memory Qdrant-like contract for deterministic tests and demo mode."""

    def __init__(self, dimension: int = 64) -> None:
        if dimension < 1:
            raise ValueError("vector dimension must be positive")
        self.dimension = dimension
        self._records: dict[str, tuple[list[float], VectorPayload]] = {}

    def upsert_chunks(self, vectors: list[tuple[list[float], VectorPayload]]) -> int:
        require_tenant_payloads(vectors)
        if any(len(vector) != self.dimension for vector, _ in vectors):
            raise ValueError("embedding dimension mismatch")
        for vector, payload in vectors:
            self._records[deterministic_vector_id(payload)] = (list(vector), payload)
        return len(vectors)

    def search(
        self, vector: list[float], scope: AuthorizedVectorScope, limit: int = 10
    ) -> list[VectorHit]:
        if len(vector) != self.dimension:
            raise ValueError("embedding dimension mismatch")
        hits: list[VectorHit] = []
        for vector_id, (candidate, payload) in self._records.items():
            if _allowed(payload, scope):
                norm = math.sqrt(sum(value * value for value in vector)) * math.sqrt(
                    sum(value * value for value in candidate)
                )
                score = (
                    sum(a * b for a, b in zip(vector, candidate, strict=True)) / norm
                    if norm
                    else 0.0
                )
                hits.append(VectorHit(vector_id, score, payload))
        return sorted(hits, key=lambda hit: hit.score, reverse=True)[:limit]

    def delete_version_vectors(self, tenant_id: str, version_id: str) -> int:
        keys = [
            key
            for key, (_, payload) in self._records.items()
            if payload.tenant_id == tenant_id and payload.document_version_id == version_id
        ]
        for key in keys:
            del self._records[key]
        return len(keys)

    def delete_document_vectors(self, tenant_id: str, document_id: str) -> int:
        keys = [
            key
            for key, (_, payload) in self._records.items()
            if payload.tenant_id == tenant_id and payload.document_id == document_id
        ]
        for key in keys:
            del self._records[key]
        return len(keys)

    def ensure_schema(self) -> None:
        return None

    def health(self) -> bool:
        return True

    def count(self) -> int:
        return len(self._records)


class QdrantVectorIndex:
    """Qdrant adapter with controlled collection/schema and server-side filters."""

    def __init__(
        self,
        url: str,
        collection: str,
        dimension: int,
        api_key: str | None = None,
        schema_version: str = VECTOR_SCHEMA_VERSION,
        timeout_seconds: int | None = None,
    ) -> None:
        if not url.startswith(("http://", "https://")) or not collection or dimension < 1:
            raise ValueError("Qdrant URL, collection and positive dimension are required")
        if not re_safe_collection(collection):
            raise ValueError("Qdrant collection name is invalid")
        try:
            from qdrant_client import QdrantClient
        except ImportError as exc:  # pragma: no cover - dependency-gated integration
            raise RuntimeError("qdrant-client is required for QdrantVectorIndex") from exc
        if timeout_seconds is not None and not 1 <= timeout_seconds <= 60:
            raise ValueError("Qdrant timeout must be between 1 and 60 seconds")
        self.client = QdrantClient(url=url, api_key=api_key, timeout=timeout_seconds)
        self.collection, self.dimension, self.schema_version = collection, dimension, schema_version

    def ensure_schema(self) -> None:
        from qdrant_client.http import models

        if not self.client.collection_exists(self.collection):
            self.client.create_collection(
                self.collection,
                vectors_config=models.VectorParams(
                    size=self.dimension, distance=models.Distance.COSINE
                ),
            )
        for field in (
            "tenant_id",
            "workspace_id",
            "collection_id",
            "document_id",
            "document_version_id",
            "document_status",
            "version_status",
            "access_level",
        ):
            self.client.create_payload_index(
                self.collection, field, models.PayloadSchemaType.KEYWORD
            )
        for field in ("valid_from", "valid_to"):
            self.client.create_payload_index(
                self.collection, field, models.PayloadSchemaType.DATETIME
            )

    def upsert_chunks(self, vectors: list[tuple[list[float], VectorPayload]]) -> int:
        from qdrant_client.http import models

        require_tenant_payloads(vectors)
        if any(len(vector) != self.dimension for vector, _ in vectors):
            raise ValueError("embedding dimension mismatch")
        points = [
            models.PointStruct(
                id=deterministic_vector_id(payload), vector=vector, payload=payload.as_dict()
            )
            for vector, payload in vectors
        ]
        self.client.upsert(self.collection, points=points, wait=True)
        return len(points)

    def search(
        self, vector: list[float], scope: AuthorizedVectorScope, limit: int = 10
    ) -> list[VectorHit]:
        from qdrant_client.http import models

        def to_condition(condition: dict[str, Any]) -> Any:
            if "range" in condition:
                range_values = condition["range"]
                range_type: Any = models.Range
                if condition["key"] in {"valid_from", "valid_to"}:
                    range_type = models.DatetimeRange
                    range_values = {
                        key: datetime.fromisoformat(value.replace("Z", "+00:00"))
                        for key, value in range_values.items()
                    }
                return models.FieldCondition(key=condition["key"], range=range_type(**range_values))
            if "is_empty" in condition:
                return models.IsEmptyCondition(
                    is_empty=models.PayloadField(key=condition["is_empty"]["key"])
                )
            match = condition["match"]
            if "value" in match:
                return models.FieldCondition(
                    key=condition["key"], match=models.MatchValue(value=match["value"])
                )
            return models.FieldCondition(
                key=condition["key"], match=models.MatchAny(any=match["any"])
            )

        built_filter = build_authorized_vector_filter(scope)
        conditions = [to_condition(item) for item in built_filter["must"]]
        valid_window = [to_condition(item) for item in built_filter["should"]]
        raw = self.client.search(
            self.collection,
            query_vector=vector,
            query_filter=models.Filter(must=conditions, should=valid_window),
            limit=limit,
        )
        return [
            VectorHit(
                str(item.id),
                float(item.score),
                VectorPayload(**(item.payload or {})),
            )
            for item in raw
        ]

    def delete_version_vectors(self, tenant_id: str, version_id: str) -> int:
        from qdrant_client.http import models

        selector = models.Filter(
            must=[
                models.FieldCondition(key="tenant_id", match=models.MatchValue(value=tenant_id)),
                models.FieldCondition(
                    key="document_version_id", match=models.MatchValue(value=version_id)
                ),
            ]
        )
        self.client.delete(
            self.collection, points_selector=models.FilterSelector(filter=selector), wait=True
        )
        return 0

    def delete_document_vectors(self, tenant_id: str, document_id: str) -> int:
        from qdrant_client.http import models

        selector = models.Filter(
            must=[
                models.FieldCondition(key="tenant_id", match=models.MatchValue(value=tenant_id)),
                models.FieldCondition(
                    key="document_id", match=models.MatchValue(value=document_id)
                ),
            ]
        )
        self.client.delete(
            self.collection, points_selector=models.FilterSelector(filter=selector), wait=True
        )
        return 0

    def health(self) -> bool:
        try:
            self.client.get_collections()
            return True
        except Exception:
            return False


def re_safe_collection(value: str) -> bool:
    return len(value) <= 100 and all(char.isalnum() or char in "-_" for char in value)
