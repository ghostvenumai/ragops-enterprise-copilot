"""Productive query workflow: vector retrieval, bounded context, grounded generation.

Retrieval runs only through ``VectorRetriever`` inside the verified tenant. A question with
instruction-like content, an empty retrieval and an empty context all abstain without a
provider call; retrieval failures and tenant violations are audited and raised so that the
API answers fail closed. Nothing here can read the local demo corpus.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from uuid import uuid4

from ragops.auth.identity import AuthenticatedUserContext
from ragops.governance.audit import AuditLogger
from ragops.llm.providers import LLMProvider
from ragops.modeling.router import ProviderError
from ragops.monitoring.metrics import MetricsRegistry
from ragops.retrieval.context import ContextChunk, ContextLimits, build_context
from ragops.retrieval.vector_retriever import (
    EmbeddingContractError,
    IncompleteChunkError,
    InvalidTenantScope,
    RetrievalError,
    RetrievalUnavailable,
    TenantBoundaryViolation,
    VectorRetriever,
)
from ragops.security.pii import mask_pii
from ragops.security.prompt_injection import detect_prompt_injection
from ragops.storage.models import Citation, QueryUser

NO_CONTEXT_ANSWER = (
    "Ich verweigere eine fachliche Antwort, weil keine ausreichende "
    "Evidenz im zulässigen Mandantenkontext gefunden wurde."
)
BLOCKED_ANSWER = "Anfrage blockiert: Prompt-Injection-Muster erkannt."
UNGROUNDED_ANSWER = "Antwort verweigert: Faktenantwort ohne ausreichende Quellenabdeckung."
_OUTCOMES: tuple[tuple[type[RetrievalError], str], ...] = (
    (TenantBoundaryViolation, "tenant_violation"),
    (EmbeddingContractError, "embedding_contract"),
    (IncompleteChunkError, "incomplete_chunk"),
    (InvalidTenantScope, "invalid_scope"),
    (RetrievalUnavailable, "unavailable"),
)


class GenerationUnavailable(RuntimeError):
    """The provider could not answer from a valid context; the request fails closed."""

    status_code = 503


@dataclass(frozen=True)
class VectorQueryResult:
    correlation_id: str
    answer: str
    abstained: bool
    citations: tuple[ContextChunk, ...]
    evidence_score: float
    tokens: int
    estimated_cost_eur: float
    retrieval_latency_ms: float
    llm_latency_ms: float
    prompt_injection_detected: bool
    outcome: str


def retrieval_outcome(error: RetrievalError) -> str:
    """Stable audit category; failures, violations and empty results never look alike."""
    return next((name for kind, name in _OUTCOMES if isinstance(error, kind)), "unavailable")


class VectorQueryService:
    def __init__(
        self,
        retriever: VectorRetriever,
        provider: LLMProvider,
        limits: ContextLimits,
        audit: AuditLogger,
        metrics: MetricsRegistry,
    ) -> None:
        self.retriever, self.provider, self.limits = retriever, provider, limits
        self.audit, self.metrics = audit, metrics

    def run(
        self, context: AuthenticatedUserContext, question: str, top_k: int
    ) -> VectorQueryResult:
        started = time.perf_counter()
        correlation_id = str(uuid4())
        user = QueryUser(user_id=context.user_id, tenant_id=context.tenant_id, role=context.role)
        if detect_prompt_injection(question):
            return self._finish(
                user, correlation_id, started, "blocked", answer=BLOCKED_ANSWER, injection=True
            )
        retrieval_started = time.perf_counter()
        try:
            hits = self.retriever.retrieve(context, question, top_k)
            built = build_context(
                hits, tenant_id=context.tenant_id, top_k=top_k, limits=self.limits
            )
        except RetrievalError as exc:
            self._failed(user, correlation_id, started, exc)
            raise
        retrieval_ms = round((time.perf_counter() - retrieval_started) * 1000, 3)
        if not built.chunks:
            return self._finish(
                user,
                correlation_id,
                started,
                "no_context",
                answer=NO_CONTEXT_ANSWER,
                retrieval_ms=retrieval_ms,
            )
        citations = [
            Citation(
                source_id=chunk.source_id,
                title=chunk.title,
                tenant_id=chunk.tenant_id,
                snippet=mask_pii(chunk.text[:260]),
                score=chunk.score,
            )
            for chunk in built.chunks
        ]
        try:
            response = self.provider.generate(question, citations, built.text)
        except ProviderError as exc:
            self._audit(user, correlation_id, "failed", "generation_unavailable", 0)
            raise GenerationUnavailable("generation unavailable") from exc
        used = set(response.used_source_ids)
        cited = tuple(chunk for chunk in built.chunks if not used or chunk.source_id in used)
        answer, abstained, outcome = response.text, response.abstained, "answered"
        if not abstained and (not cited or "[" not in answer or "]" not in answer):
            answer, abstained, outcome = UNGROUNDED_ANSWER, True, "ungrounded"
        if abstained:
            cited = ()
        return self._finish(
            user,
            correlation_id,
            started,
            outcome,
            answer=answer,
            abstained=abstained,
            citations=cited,
            tokens=response.usage.total_tokens,
            cost=response.usage.estimated_cost_eur,
            retrieval_ms=retrieval_ms,
            llm_ms=response.usage.latency_ms,
        )

    def _failed(
        self, user: QueryUser, correlation_id: str, started: float, error: RetrievalError
    ) -> None:
        outcome = retrieval_outcome(error)
        if isinstance(error, TenantBoundaryViolation):
            self.audit.record(
                correlation_id=correlation_id,
                event_type="vector_tenant_boundary_violation",
                user=user,
                outcome="blocked",
                details={
                    "foreign_hits": error.foreign_hits,
                    "returned_hits": error.returned_hits,
                },
            )
        self.metrics.blocked_access_attempts += int(outcome == "tenant_violation")
        self._record_metrics(started, success=False)
        self._audit(user, correlation_id, "failed", outcome, 0)

    def _finish(
        self,
        user: QueryUser,
        correlation_id: str,
        started: float,
        outcome: str,
        *,
        answer: str,
        abstained: bool = True,
        citations: tuple[ContextChunk, ...] = (),
        injection: bool = False,
        tokens: int = 0,
        cost: float = 0.0,
        retrieval_ms: float = 0.0,
        llm_ms: float = 0.0,
    ) -> VectorQueryResult:
        self.metrics.prompt_injection_detections += int(injection)
        self._record_metrics(
            started,
            success=True,
            retrieval_ms=retrieval_ms,
            llm_ms=llm_ms,
            tokens=tokens,
            cost=cost,
        )
        self._audit(
            user, correlation_id, "blocked" if injection else "success", outcome, len(citations)
        )
        score = round(sum(c.score for c in citations) / len(citations), 4) if citations else 0.0
        return VectorQueryResult(
            correlation_id=correlation_id,
            answer=answer,
            abstained=abstained,
            citations=citations,
            evidence_score=score,
            tokens=tokens,
            estimated_cost_eur=cost,
            retrieval_latency_ms=retrieval_ms,
            llm_latency_ms=llm_ms,
            prompt_injection_detected=injection,
            outcome=outcome,
        )

    def _record_metrics(
        self,
        started: float,
        *,
        success: bool,
        retrieval_ms: float = 0.0,
        llm_ms: float = 0.0,
        tokens: int = 0,
        cost: float = 0.0,
    ) -> None:
        self.metrics.record_request(
            success=success,
            latency_ms=round((time.perf_counter() - started) * 1000, 3),
            retrieval_latency_ms=retrieval_ms,
            llm_latency_ms=llm_ms,
            tokens=tokens,
            cost_eur=cost,
            citation_coverage=1.0 if success else 0.0,
        )

    def _audit(
        self, user: QueryUser, correlation_id: str, outcome: str, retrieval: str, citations: int
    ) -> None:
        self.audit.record(
            correlation_id=correlation_id,
            event_type="query",
            user=user,
            outcome=outcome,
            details={
                "query_mode": "vector",
                "retrieval_outcome": retrieval,
                "citation_count": citations,
            },
        )
