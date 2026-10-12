"""Synthetic query executions stopped at each provider/accounting boundary (ENT-11.6).

A "crash" is modelled faithfully: the workflow steps up to the boundary run for real and
nothing after them runs. No provider is ever called here; usage values are synthetic.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session
from tests.security.finops_helpers import DETERMINISTIC_MODEL, provision
from tests.security.oidc_helpers import ISSUER

from ragops.auth.identity import AuthenticatedUserContext
from ragops.finops.query_accounting import AccountingTicket, QueryAccounting
from ragops.finops.reconciliation import ReconciliationService
from ragops.llm.providers import LLMUsage
from ragops.modeling.router import RoutingClass, RoutingDecision
from ragops.persistence.models import (
    BudgetReservation,
    QueryExecution,
    QueryIdempotencyRecord,
    UsageRecord,
)
from ragops.workflows.query_idempotency import (
    Claim,
    IdempotencyScope,
    KeyDigest,
    QueryIdempotencyStore,
    request_fingerprint,
)

HMAC_KEY = "reconciliation-test-hmac-secret-0123456789-abcdef"
DIGEST = KeyDigest(HMAC_KEY)
QUESTION = "Welche Freigaberegel gilt?"
PROMPT_TOKENS, COMPLETION_TOKENS = 120, 40


def context(tenant: str) -> AuthenticatedUserContext:
    return AuthenticatedUserContext(
        user_id=f"user-{tenant}",
        tenant_id=tenant,
        roles=("viewer",),
        issuer=ISSUER,
        subject=f"user-{tenant}",
    )


def decision() -> RoutingDecision:
    return RoutingDecision(
        routing_class=RoutingClass.SIMPLE,
        provider_id="deterministic",
        model_id=DETERMINISTIC_MODEL,
        reason_codes=("short_question", "few_sources"),
        estimated_input_tokens=PROMPT_TOKENS,
        estimated_output_tokens=256,
        estimated_cost=0.00532,
    )


def usage(reported: bool = True) -> LLMUsage:
    if not reported:
        return LLMUsage(0, 0, 0, 0.0, 9.5, DETERMINISTIC_MODEL, reported=False)
    total = PROMPT_TOKENS + COMPLETION_TOKENS
    return LLMUsage(PROMPT_TOKENS, COMPLETION_TOKENS, total, 0.0, 9.5, DETERMINISTIC_MODEL)


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 10, 12, 9, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: int) -> None:
        self.now += timedelta(seconds=seconds)


@dataclass
class Started:
    tenant: str
    ticket: AccountingTicket
    scope: IdempotencyScope | None
    claim: Claim | None


@dataclass
class Scenario:
    engine: Engine
    tenants: set[str] = field(default_factory=set)

    def __post_init__(self) -> None:
        self.accounting = QueryAccounting(lambda: self.engine)
        self.store = QueryIdempotencyStore(lambda: self.engine)

    def tenant(self, tenant: str) -> None:
        if tenant not in self.tenants:
            provision(self.engine, tenant, issuer=ISSUER, subject=f"user-{tenant}")
            self.tenants.add(tenant)

    def start(self, tenant: str = "tenant-a", key: str | None = "retry-key-1") -> Started:
        """Claim the key, cross the idempotency billing boundary and reserve the budget."""
        self.tenant(tenant)
        scope = claim = None
        correlation = None
        if key is not None:
            scope = IdempotencyScope.for_query(context(tenant), key, DIGEST)
            claim = self.store.claim(scope, request_fingerprint(QUESTION, 3))
            self.store.enter_billing(scope, claim)
            correlation = claim.correlation_id
        from uuid import uuid4

        ticket = self.accounting.prepare(
            context(tenant),
            decision(),
            input_tokens=PROMPT_TOKENS,
            output_tokens=256,
            correlation_id=correlation or uuid4(),
        )
        return Started(tenant, ticket, scope, claim)

    # ------------------------------------------------------------------ crash windows

    def crash_before_boundary(self, tenant: str = "tenant-a", **kw: Any) -> Started:
        return self.start(tenant, **kw)

    def crash_after_boundary(self, tenant: str = "tenant-a", **kw: Any) -> Started:
        started = self.start(tenant, **kw)
        self.accounting.begin_provider(started.ticket)
        return started

    def provider_timeout(self, tenant: str = "tenant-a", **kw: Any) -> Started:
        started = self.crash_after_boundary(tenant, **kw)
        self.accounting.note_ambiguous(started.ticket, "provider_timeout")
        if started.scope is not None and started.claim is not None:
            self.store.hold(started.scope, started.claim)
        return started

    def receipt_then_crash(self, tenant: str = "tenant-a", **kw: Any) -> Started:
        started = self.crash_after_boundary(tenant, **kw)
        self.accounting.record_receipt(
            started.ticket, "deterministic", DETERMINISTIC_MODEL, usage(), 0, 9.5
        )
        if started.scope is not None and started.claim is not None:
            self.store.hold(started.scope, started.claim)
        return started

    def service(self, tenant: str = "tenant-a", **kw: Any) -> ReconciliationService:
        kw.setdefault("grace_seconds", 0)
        return ReconciliationService(lambda: self.engine, tenant, **kw)

    # ------------------------------------------------------------------ observations

    def snapshot(self, tenant: str = "tenant-a") -> dict[str, Any]:
        with Session(self.engine) as session:
            return {
                "reservations": sorted(
                    (row.status, str(row.final_amount))
                    for row in session.scalars(
                        select(BudgetReservation).where(BudgetReservation.tenant_id == tenant)
                    )
                ),
                "usage": sorted(
                    (str(row.correlation_id), row.input_tokens, row.output_tokens, str(row.cost))
                    for row in session.scalars(
                        select(UsageRecord).where(UsageRecord.tenant_id == tenant)
                    )
                ),
                "ledger": sorted(
                    (row.attempt, row.state, row.resolution or "")
                    for row in session.scalars(
                        select(QueryExecution).where(QueryExecution.tenant_id == tenant)
                    )
                ),
                "idempotency": sorted(
                    row.state
                    for row in session.scalars(
                        select(QueryIdempotencyRecord).where(
                            QueryIdempotencyRecord.tenant_id == tenant
                        )
                    )
                ),
            }

    def count(self, model: Any, tenant: str = "tenant-a", **filters: Any) -> int:
        with Session(self.engine) as session:
            query = select(func.count()).select_from(model).where(model.tenant_id == tenant)
            for name, value in filters.items():
                query = query.where(getattr(model, name) == value)
            return int(session.scalar(query) or 0)

    def reservation(self, started: Started) -> BudgetReservation:
        with Session(self.engine) as session:
            row = session.get(BudgetReservation, (started.tenant, started.ticket.reservation_id))
            assert row is not None
            session.expunge(row)
            return row


def reservation_id(started: Started) -> UUID:
    return started.ticket.reservation_id
