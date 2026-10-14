"""Productive query workflow: vector retrieval, bounded context, routed and accounted generation.

Retrieval runs only through ``VectorRetriever`` inside the verified tenant. A question with
instruction-like content, an empty retrieval and an empty context all abstain without a
route, reservation or provider call; retrieval failures and tenant violations are audited
and raised so that the API answers fail closed. Nothing here can read the local demo corpus.

Generation always goes through ``LLMModelRouter``: route, reserve the budget, durably mark
the provider boundary, execute the policy chain, commit the usage receipt, then book the
usage and close the reservation. The provider is invoked only after the boundary marker
committed; if the marker cannot be written the reservation is returned and the provider is
never called. An answer is returned only after its accounting committed. Fallback is
limited to failures the provider certainly did not bill (rate limit, model unavailable), so
a logical query is billed exactly once.

External processing passes the privacy egress gate twice (ENT-13.1): a precheck right
after routing denies an unauthorized primary model before anything is reserved and drops
unauthorized external fallback candidates; the authoritative check runs per attempt at the
provider boundary on the exact payload and yields the permit the adapter requires. A denial
never switches to another candidate and is never billed.

A caller may pass the logical ``correlation_id`` (an idempotent retry keeps its identity) and
a ``QueryProgress``: its ``before_billing`` hook runs right before the budget reservation, the
first step that may lead to a billed provider call, and ``stage`` tells the caller how far a
failed query got relative to that boundary.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from enum import StrEnum
from uuid import UUID, uuid4

from ragops.auth.identity import AuthenticatedUserContext
from ragops.finops.query_accounting import (
    AccountingError,
    AccountingNotConfigured,
    AccountingTicket,
    BookedUsage,
    BudgetLimitExceeded,
    QueryAccounting,
    ambiguity_reason,
)
from ragops.governance.audit import AuditLogger
from ragops.llm.providers import LLMResponse
from ragops.modeling.router import (
    ComplexitySignals,
    FallbackPolicyError,
    LLMModelRouter,
    ProviderError,
    ProviderErrorCategory,
    RoutingDecision,
)
from ragops.monitoring.metrics import MetricsRegistry
from ragops.privacy.egress import (
    DataClass,
    EgressDenied,
    EgressPermit,
    EgressRequest,
    PrivacyEgressGuard,
    Purpose,
    Reason,
    is_external,
)
from ragops.retrieval.context import ContextChunk, ContextLimits, build_context, estimate_tokens
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


# The provider rejected these requests before processing them: nothing was billed.
NOT_BILLED = frozenset(
    {
        ProviderErrorCategory.RATE_LIMIT,
        ProviderErrorCategory.MODEL_UNAVAILABLE,
        ProviderErrorCategory.AUTHENTICATION,
        ProviderErrorCategory.INVALID_REQUEST,
        ProviderErrorCategory.CONTEXT_LIMIT,
        ProviderErrorCategory.POLICY_DENIED,
    }
)
# Fallback only after failures that were certainly not billed; a timeout or provider error
# may already have cost money that one usage record per query could not represent.
QUERY_FALLBACK = frozenset(
    {ProviderErrorCategory.RATE_LIMIT, ProviderErrorCategory.MODEL_UNAVAILABLE}
)


class ExecutionStage(StrEnum):
    PREPARE = "prepare"  # retrieval, context and routing: nothing reserved or billed
    RESERVE = "reserve"  # budget reserved; the provider boundary is not yet marked
    PROVIDER = "provider"  # the boundary marker committed: the provider may run and bill
    ACCOUNTING = "accounting"  # the provider answered; usage is being booked
    COMPLETE = "complete"


@dataclass
class QueryProgress:
    """How far a query got relative to the billing boundary; read by the caller on failure."""

    before_billing: Callable[[], None] | None = None
    stage: ExecutionStage = ExecutionStage.PREPARE
    provider_category: ProviderErrorCategory | None = None


class GenerationUnavailable(RuntimeError):
    """The provider could not answer from a valid context; the request fails closed."""

    status_code = 503


class NoEligibleModel(RuntimeError):
    """No model satisfies the tenant's policy; nothing was reserved or executed."""

    status_code = 409


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
        router_factory: Callable[[], LLMModelRouter],
        limits: ContextLimits,
        audit: AuditLogger,
        metrics: MetricsRegistry,
        *,
        accounting: QueryAccounting | None,
        max_output_tokens: int,
        egress: PrivacyEgressGuard | None = None,
    ) -> None:
        if max_output_tokens < 1:
            raise ValueError("max_output_tokens must be positive")
        self.retriever, self.router_factory, self.limits = retriever, router_factory, limits
        self.audit, self.metrics = audit, metrics
        self.accounting, self.max_output_tokens = accounting, max_output_tokens
        # Without a guard every external provider is denied.
        self.egress = egress

    def run(
        self,
        context: AuthenticatedUserContext,
        question: str,
        top_k: int,
        *,
        correlation_id: str | None = None,
        progress: QueryProgress | None = None,
    ) -> VectorQueryResult:
        started = time.perf_counter()
        correlation_id = correlation_id or str(uuid4())
        progress = progress or QueryProgress()
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
        input_tokens = estimate_tokens(question) + built.estimated_tokens
        signals = ComplexitySignals(
            retrieved_chunks=len(built.chunks),
            source_count=len({chunk.document_id for chunk in built.chunks}),
            estimated_input_tokens=input_tokens,
            expected_output_tokens=self.max_output_tokens,
        )
        router = self.router_factory()
        try:
            decision = self._route(context.tenant_id, signals, router)
        except FallbackPolicyError:
            self._event(user, correlation_id, "model_route_rejected", "blocked", {})
            self._audit(user, correlation_id, "failed", "no_eligible_model", 0)
            raise NoEligibleModel("no eligible model") from None
        self._event(
            user,
            correlation_id,
            "model_route_selected",
            "success",
            {
                "provider_id": decision.provider_id,
                "model_id": decision.model_id,
                "routing_class": decision.routing_class.value,
                "fallback_candidates": len(decision.fallback_chain),
            },
        )
        data_classes = frozenset(
            {DataClass.USER_QUESTION}
            | {
                DataClass.DOCUMENT_TEXT
                if chunk.tenant_id == context.tenant_id
                and chunk.document_id
                and chunk.document_version_id
                else DataClass.UNCLASSIFIED
                for chunk in built.chunks
            }
        )

        def egress_request(provider_id: str, model_id: str) -> EgressRequest:
            return EgressRequest(
                tenant_id=context.tenant_id,
                correlation_id=correlation_id,
                purpose=Purpose.RAG_ANSWER,
                provider_id=provider_id,
                model_id=model_id,
                data_classes=data_classes,
            )

        decision = self._precheck_egress(user, correlation_id, decision, router, egress_request)
        if progress.before_billing is not None:
            progress.before_billing()
        progress.stage = ExecutionStage.RESERVE
        ticket = self._reserve(context, user, correlation_id, decision, input_tokens)
        self._begin_provider(user, correlation_id, ticket)
        progress.stage = ExecutionStage.PROVIDER

        def invoke(provider_id: str, model_id: str) -> tuple[str, str, LLMResponse]:
            adapter = router.providers.get(provider_id)
            if getattr(adapter, "model", None) != model_id:
                raise ProviderError(ProviderErrorCategory.MODEL_UNAVAILABLE)
            permit: dict[str, EgressPermit] = {}
            if is_external(adapter):
                # Authoritative check on the exact payload, as late as possible.
                try:
                    if self.egress is None:
                        raise EgressDenied(Reason.NO_GUARD)
                    permit["permit"] = self.egress.permit(
                        egress_request(provider_id, model_id), question, citations, built.text
                    )
                except EgressDenied as exc:
                    self._egress_event(user, correlation_id, provider_id, model_id, exc.reason)
                    raise ProviderError(ProviderErrorCategory.POLICY_DENIED) from None
                self._event(
                    user,
                    correlation_id,
                    "privacy_egress_authorized",
                    "success",
                    {"provider_id": provider_id, "model_id": model_id, "purpose": "rag_answer"},
                )
            generated: LLMResponse = adapter.generate(  # type: ignore[attr-defined]
                question, citations, built.text, **permit
            )
            return provider_id, model_id, generated

        try:
            (provider_id, model_id, response), fallback_count = router.execute_with_fallback(
                ticket.decision, invoke, fallback_categories=QUERY_FALLBACK
            )
        except ProviderError as exc:
            progress.provider_category = exc.category
            self._provider_failed(user, correlation_id, ticket, exc.category)
            if exc.category is ProviderErrorCategory.POLICY_DENIED:
                raise EgressDenied(Reason.NO_POLICY) from None
            raise GenerationUnavailable("generation unavailable") from exc
        progress.stage = ExecutionStage.ACCOUNTING
        if fallback_count:
            self._event(
                user,
                correlation_id,
                "provider_fallback_used",
                "success",
                {
                    "provider_id": provider_id,
                    "model_id": model_id,
                    "fallback_count": fallback_count,
                },
            )
        booked = self._book(
            user, correlation_id, ticket, provider_id, model_id, response, fallback_count
        )
        progress.stage = ExecutionStage.COMPLETE
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
            tokens=booked.input_tokens + booked.output_tokens,
            cost=float(booked.cost),
            retrieval_ms=retrieval_ms,
            llm_ms=response.usage.latency_ms,
        )

    def _route(
        self, tenant_id: str, signals: ComplexitySignals, router: LLMModelRouter | None = None
    ) -> RoutingDecision:
        """Routing policy is looked up for the verified tenant only."""
        return (router or self.router_factory()).route(tenant_id, signals)

    def _precheck_egress(
        self,
        user: QueryUser,
        correlation_id: str,
        decision: RoutingDecision,
        router: LLMModelRouter,
        egress_request: Callable[[str, str], EgressRequest],
    ) -> RoutingDecision:
        """Deny an unauthorized external primary before any reservation; drop unauthorized
        external fallbacks so that a denial can never be routed around."""

        def allowed(provider_id: str, model_id: str) -> bool:
            try:
                adapter = router.providers.get(provider_id)
            except ProviderError:
                return True  # not registered: it can only fail as unavailable, never send
            if not is_external(adapter):
                return True
            if self.egress is None:
                reason = Reason.NO_GUARD
            else:
                verdict = self.egress.decide(
                    egress_request(provider_id, model_id), stage="precheck"
                )
                if verdict.allowed:
                    return True
                reason = verdict.reason
            self._egress_event(user, correlation_id, provider_id, model_id, reason)
            return False

        if not allowed(decision.provider_id, decision.model_id):
            self._audit(user, correlation_id, "blocked", "egress_not_authorized", 0)
            raise EgressDenied(Reason.NO_POLICY)
        chain = tuple(item for item in decision.fallback_chain if allowed(*item))
        return replace(decision, fallback_chain=chain)

    def _egress_event(
        self,
        user: QueryUser,
        correlation_id: str,
        provider_id: str,
        model_id: str,
        reason: Reason,
    ) -> None:
        self._event(
            user,
            correlation_id,
            "privacy_egress_denied",
            "blocked",
            {
                "provider_id": provider_id,
                "model_id": model_id,
                "purpose": "rag_answer",
                "reason": reason.value,
            },
        )

    def _reserve(
        self,
        context: AuthenticatedUserContext,
        user: QueryUser,
        correlation_id: str,
        decision: RoutingDecision,
        input_tokens: int,
    ) -> AccountingTicket:
        try:
            if self.accounting is None:
                raise AccountingNotConfigured("no accounting database is configured")
            ticket = self.accounting.prepare(
                context,
                decision,
                input_tokens=input_tokens,
                output_tokens=self.max_output_tokens,
                correlation_id=UUID(correlation_id),
            )
        except BudgetLimitExceeded:
            self._event(
                user, correlation_id, "budget_rejected", "blocked", {"reason": "hard_limit"}
            )
            self._audit(user, correlation_id, "blocked", "budget_limit_exceeded", 0)
            raise
        except AccountingError as exc:
            self._event(
                user,
                correlation_id,
                "budget_reservation_failed",
                "failed",
                {"reason": type(exc).__name__},
            )
            self._audit(user, correlation_id, "failed", "accounting_unavailable", 0)
            raise
        self._event(
            user,
            correlation_id,
            "budget_reserved",
            "success",
            {"fallback_candidates": len(ticket.decision.fallback_chain)},
        )
        return ticket

    def _begin_provider(
        self, user: QueryUser, correlation_id: str, ticket: AccountingTicket
    ) -> None:
        """No marker, no provider: a lost marker returns the budget and fails closed."""
        assert self.accounting is not None
        try:
            self.accounting.begin_provider(ticket)
        except AccountingError:
            self._event(user, correlation_id, "provider_boundary_failed", "failed", {})
            try:
                self.accounting.release_unstarted(ticket)
                self._event(user, correlation_id, "reservation_released", "success", {})
            except AccountingError:
                # Still provably pre-provider: reconciliation releases it from the ledger.
                self._event(
                    user, correlation_id, "reservation_reconciliation_required", "failed", {}
                )
            self._audit(user, correlation_id, "failed", "accounting_unavailable", 0)
            raise

    def _provider_failed(
        self,
        user: QueryUser,
        correlation_id: str,
        ticket: AccountingTicket,
        category: ProviderErrorCategory,
    ) -> None:
        self._event(
            user,
            correlation_id,
            "provider_execution_failed",
            "failed",
            {"category": category.value},
        )
        assert self.accounting is not None
        if category in NOT_BILLED:
            try:
                self.accounting.release(ticket)
                self._event(user, correlation_id, "reservation_released", "success", {})
            except AccountingError:
                self._event(user, correlation_id, "reservation_reconciliation_failed", "failed", {})
        else:
            # The provider may have billed the attempt: the reservation keeps the budget.
            self.accounting.note_ambiguous(ticket, ambiguity_reason(category))
            self._event(
                user,
                correlation_id,
                "reservation_held",
                "success",
                {"reason": "provider_cost_unknown"},
            )
        self._audit(user, correlation_id, "failed", "generation_unavailable", 0)

    def _book(
        self,
        user: QueryUser,
        correlation_id: str,
        ticket: AccountingTicket,
        provider_id: str,
        model_id: str,
        response: LLMResponse,
        fallback_count: int,
    ) -> BookedUsage:
        assert self.accounting is not None
        try:
            booked = self.accounting.finalize(
                ticket,
                provider_id,
                model_id,
                response.usage,
                fallback_count,
                response.usage.latency_ms,
            )
        except AccountingError as exc:
            # The generated answer is dropped; it never reaches a log or the client.
            self._event(
                user,
                correlation_id,
                "usage_recording_failed",
                "failed",
                {"reason": type(exc).__name__},
            )
            self._audit(user, correlation_id, "failed", "accounting_unavailable", 0)
            raise
        self._event(
            user,
            correlation_id,
            "usage_recorded",
            "success",
            {
                "provider_id": provider_id,
                "model_id": model_id,
                "input_tokens": booked.input_tokens,
                "output_tokens": booked.output_tokens,
                "fallback_count": fallback_count,
            },
        )
        self._event(user, correlation_id, "reservation_finalized", "success", {})
        return booked

    def _event(
        self,
        user: QueryUser,
        correlation_id: str,
        event_type: str,
        outcome: str,
        details: dict[str, str | int | float | bool],
    ) -> None:
        self.audit.record(
            correlation_id=correlation_id,
            event_type=event_type,
            user=user,
            outcome=outcome,
            details=details,
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
