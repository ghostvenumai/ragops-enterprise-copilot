"""Tenant-scoped budget reservation and usage booking for the productive query path.

Before a provider runs, ``prepare`` resolves the caller's user, the accounting records of
every model the routing decision may execute and the tenant's current budget, and reserves
the most expensive outcome in its own committed transaction. After the provider answered,
``finalize`` books the usage record and closes the reservation in one transaction. If that
transaction fails, nothing is booked and the reservation stays ``reserved``: it keeps
consuming budget until an operator reconciles it. Reservations carry no expiry for the
same reason. All reads and writes are scoped to the verified tenant.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError

from ragops.auth.identity import AuthenticatedUserContext
from ragops.finops.reservation import BudgetReservationService, ReservationError
from ragops.llm.providers import LLMUsage
from ragops.modeling.router import RoutingDecision
from ragops.modeling.usage import MONEY_QUANTUM, UsageService, to_money
from ragops.persistence.database import transaction
from ragops.persistence.models import (
    BudgetReservation,
    ModelConfiguration,
    ProviderConfiguration,
    TenantBudget,
    UsageRecord,
    User,
)

# A provider of this kind runs locally; a price of zero is correct for it and nowhere else.
FREE_PROVIDER_KINDS = frozenset({"deterministic"})
HARD_LIMIT_MESSAGE = "budget hard limit exceeded"


class AccountingError(RuntimeError):
    """Accounting could not be completed; the request fails closed."""

    status_code = 503
    detail = "accounting unavailable"


class AccountingNotConfigured(AccountingError):
    """The tenant lacks the user, model or budget records accounting needs."""

    detail = "accounting not configured"


class BudgetLimitExceeded(AccountingError):
    status_code = 429
    detail = "budget_limit_exceeded"


class UsageUnavailable(AccountingError):
    """The provider reported no token counts; unknown usage is never booked as zero."""


@dataclass(frozen=True)
class AccountedModel:
    provider_id: str
    model_id: str
    configuration_id: UUID
    input_price: Decimal
    output_price: Decimal

    def cost(self, input_tokens: int, output_tokens: int) -> Decimal:
        return (
            Decimal(input_tokens) * self.input_price + Decimal(output_tokens) * self.output_price
        ).quantize(MONEY_QUANTUM)


@dataclass(frozen=True)
class AccountingTicket:
    tenant_id: str
    correlation_id: UUID
    user_id: UUID
    reservation_id: UUID
    estimated_amount: Decimal
    decision: RoutingDecision
    models: Mapping[tuple[str, str], AccountedModel]


@dataclass(frozen=True)
class BookedUsage:
    record_id: UUID
    input_tokens: int
    output_tokens: int
    cost: Decimal


class QueryAccounting:
    def __init__(
        self,
        engine: Callable[[], Engine],
        *,
        today: Callable[[], date] = lambda: datetime.now(UTC).date(),
    ) -> None:
        self._engine, self._today = engine, today

    def prepare(
        self,
        context: AuthenticatedUserContext,
        decision: RoutingDecision,
        *,
        input_tokens: int,
        output_tokens: int,
        correlation_id: UUID,
    ) -> AccountingTicket:
        """Resolve accounting references and reserve the budget; commits before returning."""
        tenant = context.tenant_id
        try:
            with transaction(self._engine()) as session:
                user = session.scalar(
                    select(User).where(
                        User.tenant_id == tenant,
                        User.issuer == context.issuer,
                        User.subject == (context.subject or context.user_id),
                        User.active.is_(True),
                    )
                )
                if user is None:
                    raise AccountingNotConfigured("user is not provisioned for this tenant")
                chain = ((decision.provider_id, decision.model_id), *decision.fallback_chain)
                models: dict[tuple[str, str], AccountedModel] = {}
                for provider_id, model_id in chain:
                    row = session.execute(
                        select(ModelConfiguration, ProviderConfiguration.kind)
                        .join(
                            ProviderConfiguration,
                            (ProviderConfiguration.tenant_id == ModelConfiguration.tenant_id)
                            & (ProviderConfiguration.id == ModelConfiguration.provider_id),
                        )
                        .where(
                            ModelConfiguration.tenant_id == tenant,
                            ProviderConfiguration.name == provider_id,
                            ModelConfiguration.name == model_id,
                        )
                    ).first()
                    if row is None or row[0].enabled is False:
                        continue
                    model, kind = row
                    priced = model.input_price > 0 or model.output_price > 0
                    if not priced and kind not in FREE_PROVIDER_KINDS:
                        continue  # a paid model without prices can never be accounted
                    models[(provider_id, model_id)] = AccountedModel(
                        provider_id, model_id, model.id, model.input_price, model.output_price
                    )
                if chain[0] not in models:
                    raise AccountingNotConfigured("the routed model has no accounting record")
                # Only accountable models may run; the routing order is otherwise unchanged.
                decision = replace(
                    decision,
                    fallback_chain=tuple(item for item in chain[1:] if item in models),
                )
                today = self._today()
                budget = session.scalar(
                    select(TenantBudget)
                    .where(
                        TenantBudget.tenant_id == tenant,
                        TenantBudget.period_start <= today,
                        TenantBudget.period_end > today,
                    )
                    .order_by(TenantBudget.period_start.desc())
                )
                if budget is None:
                    raise AccountingNotConfigured("no budget covers the current period")
                # Only one attempt is billed (fallback follows only non-billed failures),
                # so the most expensive reachable model bounds the cost of this query.
                estimate = max(
                    models[item].cost(input_tokens, output_tokens)
                    for item in (
                        (decision.provider_id, decision.model_id),
                        *decision.fallback_chain,
                    )
                )
                try:
                    reservation = BudgetReservationService(session, tenant).reserve(
                        budget.id, estimate, f"query:{correlation_id}"
                    )
                except ReservationError as exc:
                    if str(exc) == HARD_LIMIT_MESSAGE:
                        raise BudgetLimitExceeded(HARD_LIMIT_MESSAGE) from None
                    raise AccountingNotConfigured("budget cannot be reserved") from None
                return AccountingTicket(
                    tenant_id=tenant,
                    correlation_id=correlation_id,
                    user_id=user.id,
                    reservation_id=reservation.id,
                    estimated_amount=reservation.estimated_amount,
                    decision=decision,
                    models=models,
                )
        except AccountingError:
            raise
        except (SQLAlchemyError, ValueError, OSError) as exc:
            raise AccountingError("accounting preparation failed") from exc

    def finalize(
        self,
        ticket: AccountingTicket,
        provider_id: str,
        model_id: str,
        usage: LLMUsage,
        fallback_count: int,
        latency_ms: float,
    ) -> BookedUsage:
        """Book the usage and close the reservation atomically; idempotent per query."""
        if not usage.reported:
            raise UsageUnavailable("provider reported no usage")
        model = ticket.models.get((provider_id, model_id))
        if model is None:
            raise AccountingError("the executed model has no accounting record")
        try:
            with transaction(self._engine()) as session:
                reservation = session.get(
                    BudgetReservation, (ticket.tenant_id, ticket.reservation_id)
                )
                if reservation is None:
                    raise AccountingError("reservation not found")
                service = BudgetReservationService(session, ticket.tenant_id)
                existing = session.scalar(
                    select(UsageRecord).where(
                        UsageRecord.tenant_id == ticket.tenant_id,
                        UsageRecord.correlation_id == ticket.correlation_id,
                    )
                )
                if existing is None:
                    cost = model.cost(usage.prompt_tokens, usage.completion_tokens)
                    existing = UsageService(
                        session, ticket.tenant_id, ticket.user_id, model.configuration_id
                    ).record(
                        replace(ticket.decision, provider_id=provider_id, model_id=model_id),
                        input_tokens=usage.prompt_tokens,
                        output_tokens=usage.completion_tokens,
                        actual_cost=to_money(cost),
                        latency_ms=latency_ms,
                        correlation_id=ticket.correlation_id,
                        fallback_count=fallback_count,
                    )
                service.commit(reservation, existing.cost)
                return BookedUsage(
                    existing.id, existing.input_tokens, existing.output_tokens, existing.cost
                )
        except AccountingError:
            raise
        except (SQLAlchemyError, ReservationError, ValueError, OSError, RuntimeError) as exc:
            raise AccountingError("usage could not be recorded") from exc

    def release(self, ticket: AccountingTicket) -> None:
        """Return the reserved budget after a request the provider certainly did not bill."""
        try:
            with transaction(self._engine()) as session:
                reservation = session.get(
                    BudgetReservation, (ticket.tenant_id, ticket.reservation_id)
                )
                if reservation is None:
                    raise AccountingError("reservation not found")
                BudgetReservationService(session, ticket.tenant_id).release(reservation)
        except AccountingError:
            raise
        except (SQLAlchemyError, ReservationError, ValueError, OSError) as exc:
            raise AccountingError("reservation could not be released") from exc
