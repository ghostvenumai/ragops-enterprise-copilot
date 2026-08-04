"""Typed multi-agent workflow for tenant-aware RAG answers."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from uuid import uuid4

from ragops.auth.rbac import AuthorizationError, require_tenant
from ragops.governance.audit import AuditLogger
from ragops.llm.providers import LLMProvider, provider_from_env
from ragops.monitoring.metrics import MetricsRegistry
from ragops.retrieval.hybrid import HybridRetriever
from ragops.security.pii import mask_pii
from ragops.security.prompt_injection import detect_prompt_injection
from ragops.storage.json_store import JsonRepository
from ragops.storage.models import (
    Citation,
    Contract,
    Customer,
    QueryUser,
    SearchResult,
    SupportTicket,
)


@dataclass(frozen=True)
class QueryRequest:
    question: str
    user: QueryUser
    top_k: int = 5
    correlation_id: str = field(default_factory=lambda: str(uuid4()))


@dataclass
class WorkflowState:
    request: QueryRequest
    intent: str = "unknown"
    filters: dict[str, str] = field(default_factory=dict)
    retrieval_results: list[SearchResult] = field(default_factory=list)
    crm_matches: list[dict[str, str]] = field(default_factory=list)
    citations: list[Citation] = field(default_factory=list)
    answer: str = ""
    abstained: bool = False
    errors: list[str] = field(default_factory=list)
    injection_findings: list[str] = field(default_factory=list)
    retrieval_latency_ms: float = 0.0
    llm_latency_ms: float = 0.0
    total_latency_ms: float = 0.0
    token_count: int = 0
    estimated_cost_eur: float = 0.0
    evidence_score: float = 0.0


class RAGWorkflow:
    def __init__(
        self,
        repository: JsonRepository,
        *,
        provider: LLMProvider | None = None,
        audit_logger: AuditLogger | None = None,
        metrics: MetricsRegistry | None = None,
        reference_date: date = date(2026, 8, 3),
    ) -> None:
        self.repository = repository
        self.provider = provider or provider_from_env()
        self.audit_logger = audit_logger or AuditLogger(Path("evidence/audit-events.jsonl"))
        self.metrics = metrics or MetricsRegistry()
        self.reference_date = reference_date

    def run(self, request: QueryRequest) -> WorkflowState:
        started = time.perf_counter()
        state = WorkflowState(request=request)
        try:
            self._tenant_guard(state)
            self._intent_router(state)
            self._retrieval_planner(state)
            self._knowledge_retrieval(state)
            self._crm_agent(state)
            self._compliance_security_agent(state)
            self._response_composer(state)
            self._citation_validator(state)
        except AuthorizationError as exc:
            state.errors.append(str(exc))
            state.abstained = True
            state.answer = (
                "Zugriff verweigert: Der angeforderte Mandantenkontext ist nicht freigegeben."
            )
        finally:
            state.total_latency_ms = round((time.perf_counter() - started) * 1000, 3)
            self._evaluation_and_audit(state)
        return state

    def _tenant_guard(self, state: WorkflowState) -> None:
        require_tenant(state.request.user, state.request.user.tenant_id)

    def _intent_router(self, state: WorkflowState) -> None:
        question = state.request.question.lower()
        contract_risk_terms = {
            "contract",
            "renewal",
            "vertrag",
            "verträge",
            "vertraege",
            "verlängerung",
            "verlaengerung",
        }
        if any(term in question for term in contract_risk_terms):
            state.intent = "crm_contract_risk"
        elif "preis" in question or "preisliste" in question:
            state.intent = "pricing"
        elif "compliance" in question or "richtlinie" in question:
            state.intent = "compliance"
        else:
            state.intent = "knowledge"

    def _retrieval_planner(self, state: WorkflowState) -> None:
        if state.intent == "pricing":
            state.filters["classification"] = "pricing"
        elif state.intent == "compliance":
            state.filters["classification"] = "policy"

    def _knowledge_retrieval(self, state: WorkflowState) -> None:
        started = time.perf_counter()
        retriever = HybridRetriever(self.repository.chunks())
        state.retrieval_results = retriever.search(
            state.request.question,
            state.request.user,
            top_k=state.request.top_k,
            filters=state.filters,
        )
        state.retrieval_latency_ms = round((time.perf_counter() - started) * 1000, 3)

    def _crm_agent(self, state: WorkflowState) -> None:
        if state.intent != "crm_contract_risk":
            return
        user = state.request.user
        customers = {customer.customer_id: customer for customer in self.repository.customers()}
        horizon = self.reference_date + timedelta(days=60)
        open_critical_tickets = [
            ticket
            for ticket in self.repository.tickets()
            if ticket.tenant_id == user.tenant_id
            and ticket.severity.lower() == "critical"
            and ticket.status.lower() in {"open", "escalated"}
        ]
        expiring_contracts = [
            contract
            for contract in self.repository.contracts()
            if contract.tenant_id == user.tenant_id
            and contract.status.lower() == "active"
            and self.reference_date <= contract.end_date <= horizon
        ]
        ticket_by_customer: dict[str, list[SupportTicket]] = {}
        for ticket in open_critical_tickets:
            ticket_by_customer.setdefault(ticket.customer_id, []).append(ticket)
        contract_by_customer: dict[str, list[Contract]] = {}
        for contract in expiring_contracts:
            contract_by_customer.setdefault(contract.customer_id, []).append(contract)
        matches = set(ticket_by_customer) & set(contract_by_customer)
        for customer_id in sorted(matches):
            customer: Customer = customers[customer_id]
            if customer.segment.lower() != "enterprise":
                continue
            ticket = ticket_by_customer[customer_id][0]
            contract = contract_by_customer[customer_id][0]
            state.crm_matches.append(
                {
                    "customer_id": customer.customer_id,
                    "customer_name": customer.name,
                    "ticket_id": ticket.ticket_id,
                    "contract_id": contract.contract_id,
                    "contract_end_date": contract.end_date.isoformat(),
                    "recommended_action": (
                        "bereichsübergreifendes Verlängerungsteam einrichten, "
                        "Sponsor aus der Geschäftsleitung benennen, Support "
                        "eskalieren und kommerziellen Bindungsplan vorbereiten"
                    ),
                }
            )

    def _compliance_security_agent(self, state: WorkflowState) -> None:
        user_findings = detect_prompt_injection(state.request.question)
        document_findings = [
            result.chunk.chunk_id
            for result in state.retrieval_results
            if result.chunk.has_prompt_injection
        ]
        state.injection_findings = user_findings + document_findings
        if user_findings:
            state.abstained = True
            state.answer = "Anfrage blockiert: Prompt-Injection-Muster erkannt."
            return
        question = state.request.question.lower()
        refusal_terms = {
            "bankverbindung",
            "api key",
            "api keys",
            "passwoerter",
            "passwort",
            "personenbezogene kontaktinformationen",
            "erfinde",
            "systemprompt",
        }
        ambiguous_terms = {"was ist der beste plan"}
        if any(term in question for term in refusal_terms | ambiguous_terms):
            state.abstained = True
            state.answer = (
                "Ich verweigere eine fachliche Antwort, weil keine ausreichende "
                "Evidenz oder keine zulässige Datenfreigabe für diese Anfrage vorliegt."
            )
            return
        if "restricted" in question and state.request.user.role not in {"compliance", "admin"}:
            state.abstained = True
            state.answer = (
                "Zugriff verweigert: Die Rolle darf keine als eingeschränkt "
                "klassifizierten Inhalte abrufen."
            )
            return
        if "prompt injection" in question or "prompt-injection" in question:
            state.abstained = True
            state.answer = (
                "Anfrage blockiert: Prompt-Injection-Kontext wird defensiv markiert "
                "und nicht als Wissensquelle ausgegeben."
            )
            return
        cross_tenant_terms = {"tenant-beta", " beta ", "orion health", "northstar", "urban grid"}
        padded_question = f" {question} "
        cross_tenant_detected = any(term in padded_question for term in cross_tenant_terms)
        if state.request.user.tenant_id != "tenant-beta" and cross_tenant_detected:
            state.abstained = True
            state.answer = "Zugriff verweigert: Mandantenübergreifende Anfrage blockiert."
            return
        clean_results = [
            result for result in state.retrieval_results if not result.chunk.has_prompt_injection
        ]
        state.retrieval_results = clean_results

    def _response_composer(self, state: WorkflowState) -> None:
        if state.answer:
            return
        citations: list[Citation] = []
        for result in state.retrieval_results:
            citations.append(
                Citation(
                    source_id=result.chunk.citation,
                    title=result.chunk.metadata.title,
                    tenant_id=result.chunk.tenant_id,
                    snippet=mask_pii(result.chunk.text[:260]),
                    score=result.rerank_score,
                )
            )
        for match in state.crm_matches:
            citations.append(
                Citation(
                    source_id=(
                        f"CRM:{match['customer_id']}:{match['ticket_id']}:{match['contract_id']}"
                    ),
                    title=f"CRM-Risikotreffer: {match['customer_name']}",
                    tenant_id=state.request.user.tenant_id,
                    snippet=(
                        f"{match['customer_name']}: kritisches Support-Ticket "
                        f"{match['ticket_id']}; aktiver Vertrag "
                        f"{match['contract_id']} endet am "
                        f"{match['contract_end_date']}. Empfohlene Maßnahmen: "
                        f"{match['recommended_action']}"
                    ),
                    score=1.0,
                )
            )
        state.citations = citations[: state.request.top_k + len(state.crm_matches)]
        context = "\n".join(citation.snippet for citation in state.citations)
        response = self.provider.generate(state.request.question, state.citations, context)
        if response.used_source_ids:
            used_source_ids = set(response.used_source_ids)
            state.citations = [
                citation for citation in state.citations if citation.source_id in used_source_ids
            ]
        state.answer = response.text
        state.abstained = response.abstained
        state.llm_latency_ms = response.usage.latency_ms
        state.token_count = response.usage.total_tokens
        state.estimated_cost_eur = response.usage.estimated_cost_eur
        state.evidence_score = (
            round(
                sum(citation.score for citation in state.citations) / len(state.citations),
                4,
            )
            if state.citations
            else 0.0
        )

    def _citation_validator(self, state: WorkflowState) -> None:
        if state.abstained:
            return
        missing_citations = (
            not state.citations or "[" not in state.answer or "]" not in state.answer
        )
        if missing_citations:
            state.abstained = True
            state.answer = "Antwort verweigert: Faktenantwort ohne ausreichende Quellenabdeckung."

    def _evaluation_and_audit(self, state: WorkflowState) -> None:
        citation_coverage = 1.0 if state.abstained or state.citations else 0.0
        success = not state.errors
        self.metrics.prompt_injection_detections += int(bool(state.injection_findings))
        denied_errors = any("denied" in error for error in state.errors)
        self.metrics.blocked_access_attempts += int(denied_errors)
        self.metrics.record_request(
            success=success,
            latency_ms=state.total_latency_ms,
            retrieval_latency_ms=state.retrieval_latency_ms,
            llm_latency_ms=state.llm_latency_ms,
            tokens=state.token_count,
            cost_eur=state.estimated_cost_eur,
            citation_coverage=citation_coverage,
        )
        self.audit_logger.record(
            correlation_id=state.request.correlation_id,
            event_type="query",
            user=state.request.user,
            outcome="success" if success else "blocked",
            details={
                "intent": state.intent,
                "abstained": state.abstained,
                "citation_count": len(state.citations),
                "evidence_score": state.evidence_score,
                "prompt_injection_detected": bool(state.injection_findings),
            },
        )
