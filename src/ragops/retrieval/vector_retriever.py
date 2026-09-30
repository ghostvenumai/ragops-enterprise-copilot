"""Productive, tenant-isolated vector retrieval.

The only tenant this module ever uses is the one of the verified
``AuthenticatedUserContext``. There is no parameter through which a request could name
another tenant, and there is no second source of documents: when the vector index
cannot answer, retrieval fails closed instead of answering from anywhere else.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from ragops.auth.identity import AuthenticatedUserContext
from ragops.auth.rbac import ROLE_MAX_ACCESS, can_access_level
from ragops.vector.embedding import EMBEDDING_MODEL, EmbeddingProvider
from ragops.vector.index import AuthorizedVectorScope, VectorHit, VectorIndex

MAX_TOP_K = 20
TENANT_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
SecuritySignal = Callable[[str, Mapping[str, object]], None]
_security_log = logging.getLogger("ragops.security")


class RetrievalError(RuntimeError):
    """Retrieval did not produce a trustworthy result; the request must fail closed."""

    status_code = 503


class RetrievalUnavailable(RetrievalError):
    """The vector index could not be queried."""


class EmbeddingContractError(RetrievalError):
    """Query and stored embeddings are not provably compatible."""


class IncompleteChunkError(RetrievalError):
    """A stored chunk lacks the text or metadata a citation needs."""


class TenantBoundaryViolation(RetrievalError):
    """The index returned a chunk of another tenant despite the mandatory filter."""


class InvalidTenantScope(RetrievalError):
    """The caller is not a verified identity with a usable tenant and role."""

    status_code = 403


@dataclass(frozen=True)
class RetrievedChunk:
    """A tenant-verified chunk with the metadata a citation needs."""

    tenant_id: str
    document_id: str
    document_version_id: str
    chunk_id: str
    chunk_index: int
    text: str
    score: float
    source_name: str
    title: str | None
    page_number: int | None


def authoritative_scope(context: AuthenticatedUserContext) -> AuthorizedVectorScope:
    """The immutable search scope of a verified identity; nothing else can supply a tenant."""
    if not isinstance(context, AuthenticatedUserContext):
        raise InvalidTenantScope("a verified identity is required")
    tenant = context.tenant_id
    if not isinstance(tenant, str) or TENANT_ID.fullmatch(tenant) is None:
        raise InvalidTenantScope("the verified identity has no usable tenant")
    try:
        role = context.role
    except (IndexError, TypeError):
        raise InvalidTenantScope("the verified identity has no usable role") from None
    if role not in ROLE_MAX_ACCESS:
        raise InvalidTenantScope("the verified identity has no usable role")
    return AuthorizedVectorScope(tenant_id=tenant, role=role)


def _log_signal(event: str, details: Mapping[str, object]) -> None:
    _security_log.error("%s %s", event, dict(details))


class VectorRetriever:
    """Embeds a query and searches the vector index inside the caller's tenant only."""

    def __init__(
        self,
        index: VectorIndex,
        embedding: EmbeddingProvider,
        *,
        security_signal: SecuritySignal | None = None,
    ) -> None:
        if embedding.model != EMBEDDING_MODEL:
            raise EmbeddingContractError("the query embedding model is not approved")
        self._index, self._embedding = index, embedding
        self._signal = security_signal or _log_signal

    def retrieve(
        self, context: AuthenticatedUserContext, query: str, top_k: int
    ) -> list[RetrievedChunk]:
        scope = authoritative_scope(context)
        if isinstance(top_k, bool) or not isinstance(top_k, int) or not 1 <= top_k <= MAX_TOP_K:
            raise ValueError(f"top_k must be between 1 and {MAX_TOP_K}")
        vector = self._embedding.embed([query])[0]
        if len(vector) != self._index.dimension or self._embedding.dimension != len(vector):
            raise EmbeddingContractError("query and index embedding dimensions differ")
        if not any(vector):
            return []  # nothing to search for: no context, and no index call
        try:
            hits = list(self._index.search(vector, scope, limit=top_k))
        except Exception:  # noqa: BLE001 - every index failure is the same closed outcome
            raise RetrievalUnavailable("vector index unavailable") from None
        foreign = [hit for hit in hits if hit.payload.tenant_id != scope.tenant_id]
        if foreign:
            # Defense in depth behind the server-side filter: nothing of this result is used.
            self._signal(
                "vector_tenant_boundary_violation",
                {
                    "authoritative_tenant": scope.tenant_id,
                    "foreign_hits": len(foreign),
                    "returned_hits": len(hits),
                },
            )
            raise TenantBoundaryViolation("vector index returned a chunk outside the tenant")
        chunks = [self._chunk(hit, scope) for hit in hits[:top_k]]
        return sorted(chunks, key=lambda item: (-item.score, item.document_id, item.chunk_index))

    def _chunk(self, hit: VectorHit, scope: AuthorizedVectorScope) -> RetrievedChunk:
        payload = hit.payload
        if payload.embedding_model != self._embedding.model:
            raise EmbeddingContractError("a stored chunk was embedded with another model")
        if not can_access_level(scope.role, payload.access_level):
            raise IncompleteChunkError("a stored chunk is outside the caller's access level")
        if (
            not isinstance(payload.chunk_text, str)
            or not payload.chunk_text.strip()
            or not payload.document_id
            or not payload.document_version_id
            or not payload.chunk_id
        ):
            raise IncompleteChunkError("a stored chunk lacks text or citation metadata")
        return RetrievedChunk(
            tenant_id=payload.tenant_id,
            document_id=payload.document_id,
            document_version_id=payload.document_version_id,
            chunk_id=payload.chunk_id,
            chunk_index=payload.chunk_index,
            text=payload.chunk_text,
            score=round(float(hit.score), 6),
            source_name=payload.source_name,
            title=payload.title,
            page_number=payload.page_number,
        )
