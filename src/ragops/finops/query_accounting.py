"""Tenant-scoped budget reservation and usage booking for the productive query path.

Before a provider runs, ``prepare`` resolves the caller's user, the accounting records of
every model the routing decision may execute and the tenant's current budget, and reserves
the most expensive outcome together with a ``prepared`` row of the durable execution ledger
(``QueryExecution``) in one committed transaction. ``begin_provider`` then commits
``provider_may_run``; only after that commit may the provider run, so a missing marker proves
that this workflow never invoked it. After the provider answered, ``record_receipt`` commits
the reported usage with the execution-time price snapshot, and ``finalize`` books the usage
record, commits the reservation and marks the ledger ``accounted`` in one transaction.

If booking fails, nothing is booked and the reservation stays ``reserved``: it keeps
consuming budget until ``finops.reconciliation`` resolves it from the ledger (a durable
receipt is booked exactly as the workflow would have booked it; a provider that may have run
without a receipt stays held). Reservations carry no expiry for the same reason. All reads
and writes are scoped to the verified tenant.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from ragops.auth.identity import AuthenticatedUserContext
from ragops.finops.reservation import BudgetReservationService, ReservationError
from ragops.llm.providers import LLMUsage
from ragops.modeling.router import RoutingClass, RoutingDecision
from ragops.modeling.usage import MONEY_QUANTUM, UsageService, to_money
from ragops.persistence.database import transaction
from ragops.persistence.models import (
    BudgetReservation,
    ModelConfiguration,
    ProviderConfiguration,
    QueryExecution,
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


class ExecutionState(StrEnum):
    PREPARED = "prepared"  # reserved; the provider has certainly not been invoked
    PROVIDER_MAY_RUN = "provider_may_run"  # committed before the provider is invoked
    RECEIPT_RECORDED = "receipt_recorded"  # reported usage and price snapshot are durable
    ACCOUNTED = "accounted"  # usage record, reservation commit and ledger in one transaction
    RELEASED = "released"  # the provider certainly did not bill this attempt


OPEN_EXECUTION_STATES = (
    ExecutionState.PREPARED,
    ExecutionState.PROVIDER_MAY_RUN,
    ExecutionState.RECEIPT_RECORDED,
)


def _rowcount(result: Any) -> int:
    """Rows changed by an UPDATE; reliable on PostgreSQL and SQLite."""
    return int(result.rowcount)


def reservation_key(correlation_id: UUID, attempt: int) -> str:
    """The first attempt keeps the ENT-11.3 key; a retry after a release gets a fresh one."""
    return f"query:{correlation_id}" if attempt == 1 else f"query:{correlation_id}:{attempt}"


def parse_reservation_key(key: str) -> tuple[UUID, int] | None:
    """Correlation and attempt of a query reservation key; None for any other reservation."""
    parts = key.split(":")
    if parts[0] != "query" or len(parts) not in (2, 3):
        return None
    try:
        attempt = int(parts[2]) if len(parts) == 3 else 1
        if attempt < 2 and len(parts) == 3:
            return None
        return UUID(parts[1]), attempt
    except ValueError:
        return None


def ambiguity_reason(category: Any) -> str:
    """Stable, non-sensitive reason why a provider attempt may have billed."""
    value = str(getattr(category, "value", category) or "unknown").lower()
    return value if value.startswith("provider") else f"provider_{value}"


def receipt_cost(execution: QueryExecution) -> Decimal | None:
    """The cost that follows from the receipt's own tokens and price snapshot."""
    values = (
        execution.input_tokens,
        execution.output_tokens,
        execution.input_price,
        execution.output_price,
    )
    if any(value is None for value in values):
        return None
    assert execution.input_tokens is not None and execution.output_tokens is not None
    assert execution.input_price is not None and execution.output_price is not None
    return (
        Decimal(execution.input_tokens) * Decimal(execution.input_price)
        + Decimal(execution.output_tokens) * Decimal(execution.output_price)
    ).quantize(MONEY_QUANTUM)


def settle_receipt(
    session: Session,
    execution: QueryExecution,
    reservation: BudgetReservation,
    now: datetime,
) -> UsageRecord:
    """Book a durable receipt exactly as the workflow books it; the caller holds both locks.

    Shared by ``finalize`` and reconciliation so that a recovered query produces the same
    usage record, at the execution-time prices, as an undisturbed one.
    """
    if (
        execution.provider_id is None
        or execution.model_id_text is None
        or execution.cost is None
        or execution.input_tokens is None
        or execution.output_tokens is None
        or execution.latency_ms is None
        or execution.fallback_count is None
    ):
        raise AccountingError("receipt is incomplete")
    decision = RoutingDecision(
        routing_class=RoutingClass(execution.routing_class),
        provider_id=execution.provider_id,
        model_id=execution.model_id_text,
        reason_codes=tuple(code for code in execution.routing_reason.split(",") if code),
        estimated_input_tokens=0,
        estimated_output_tokens=0,
        estimated_cost=float(execution.estimated_cost),
    )
    record = UsageService(
        session, execution.tenant_id, execution.user_id, execution.model_configuration_id
    ).record(
        decision,
        input_tokens=execution.input_tokens,
        output_tokens=execution.output_tokens,
        actual_cost=to_money(Decimal(execution.cost)),
        latency_ms=execution.latency_ms,
        correlation_id=execution.correlation_id,
        fallback_count=execution.fallback_count,
    )
    BudgetReservationService(session, execution.tenant_id).commit(reservation, record.cost)
    execution.state = ExecutionState.ACCOUNTED.value
    execution.updated_at = now
    session.flush()
    return record


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
    execution_id: UUID
    attempt: int = 1


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
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._engine, self._today, self._clock = engine, today, clock

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
                reusable, attempt = self._earlier_attempts(
                    session, tenant, correlation_id, estimate
                )
                if reusable is not None:
                    # The same logical query was prepared before and never crossed the
                    # boundary: its reservation still covers this attempt.
                    execution, reservation = reusable
                    execution.routing_class = decision.routing_class.value
                    execution.routing_reason = ",".join(decision.reason_codes)
                    execution.estimated_cost = to_money(decision.estimated_cost)
                    execution.updated_at = self._clock()
                    session.flush()
                    return self._ticket(
                        tenant, correlation_id, user.id, reservation, decision, models, execution
                    )
                key = reservation_key(correlation_id, attempt)
                if session.scalar(
                    select(BudgetReservation.id).where(
                        BudgetReservation.tenant_id == tenant,
                        BudgetReservation.idempotency_key == key,
                    )
                ):
                    # A reservation without ledger evidence (ENT-11.5) is never reused.
                    raise AccountingError("the logical query already holds a reservation")
                try:
                    reservation = BudgetReservationService(session, tenant).reserve(
                        budget.id, estimate, key
                    )
                except ReservationError as exc:
                    if str(exc) == HARD_LIMIT_MESSAGE:
                        raise BudgetLimitExceeded(HARD_LIMIT_MESSAGE) from None
                    raise AccountingNotConfigured("budget cannot be reserved") from None
                now = self._clock()
                execution = QueryExecution(
                    tenant_id=tenant,
                    correlation_id=correlation_id,
                    attempt=attempt,
                    reservation_id=reservation.id,
                    user_id=user.id,
                    state=ExecutionState.PREPARED.value,
                    routing_class=decision.routing_class.value,
                    routing_reason=",".join(decision.reason_codes),
                    estimated_cost=to_money(decision.estimated_cost),
                    created_at=now,
                    updated_at=now,
                )
                session.add(execution)
                session.flush()
                return self._ticket(
                    tenant, correlation_id, user.id, reservation, decision, models, execution
                )
        except AccountingError:
            raise
        except (SQLAlchemyError, ValueError, OSError) as exc:
            raise AccountingError("accounting preparation failed") from exc

    @staticmethod
    def _ticket(
        tenant: str,
        correlation_id: UUID,
        user_id: UUID,
        reservation: BudgetReservation,
        decision: RoutingDecision,
        models: Mapping[tuple[str, str], AccountedModel],
        execution: QueryExecution,
    ) -> AccountingTicket:
        return AccountingTicket(
            tenant_id=tenant,
            correlation_id=correlation_id,
            user_id=user_id,
            reservation_id=reservation.id,
            estimated_amount=reservation.estimated_amount,
            decision=decision,
            models=models,
            execution_id=execution.id,
            attempt=execution.attempt,
        )

    def _earlier_attempts(
        self, session: Session, tenant: str, correlation_id: UUID, estimate: Decimal
    ) -> tuple[tuple[QueryExecution, BudgetReservation] | None, int]:
        """Reuse or release earlier attempts that never crossed the boundary.

        A prepared attempt whose reservation still covers the new estimate is reused (one
        reservation per logical query); a smaller one is released and superseded. An attempt
        whose provider may have run, or that was booked, is never touched: the logical query
        must not execute twice.
        """
        earlier = session.scalars(
            select(QueryExecution)
            .where(
                QueryExecution.tenant_id == tenant,
                QueryExecution.correlation_id == correlation_id,
            )
            .order_by(QueryExecution.attempt)
            .with_for_update()
        ).all()
        reusable: tuple[QueryExecution, BudgetReservation] | None = None
        for execution in earlier:
            if execution.state == ExecutionState.RELEASED.value:
                continue
            if execution.state != ExecutionState.PREPARED.value:
                raise AccountingError("the logical query may already have been billed")
            reservation = self._locked_reservation(session, tenant, execution.reservation_id)
            if (
                reusable is None
                and reservation.status == "reserved"
                and Decimal(reservation.estimated_amount) >= estimate
            ):
                reusable = (execution, reservation)
                continue
            BudgetReservationService(session, tenant).release(reservation)
            now = self._clock()
            execution.state = ExecutionState.RELEASED.value
            execution.resolution, execution.resolved_at = "superseded", now
            execution.updated_at = now
        session.flush()
        return reusable, max((execution.attempt for execution in earlier), default=0) + 1

    @staticmethod
    def _locked_reservation(
        session: Session, tenant: str, reservation_id: UUID
    ) -> BudgetReservation:
        reservation = session.scalar(
            select(BudgetReservation)
            .where(BudgetReservation.tenant_id == tenant, BudgetReservation.id == reservation_id)
            .with_for_update()
        )
        if reservation is None:
            raise AccountingError("reservation not found")
        return reservation

    def _advance(
        self,
        ticket: AccountingTicket,
        source: ExecutionState,
        target: ExecutionState,
        **values: Any,
    ) -> None:
        """Conditional ledger transition in its own committed transaction; fails closed."""
        try:
            with transaction(self._engine()) as session:
                changed = _rowcount(
                    session.execute(
                        update(QueryExecution)
                        .where(
                            QueryExecution.tenant_id == ticket.tenant_id,
                            QueryExecution.id == ticket.execution_id,
                            QueryExecution.state == source.value,
                        )
                        .values(state=target.value, updated_at=self._clock(), **values)
                        .execution_options(synchronize_session=False)
                    )
                )
                if changed != 1:
                    raise AccountingError(f"query execution is not {source.value}")
        except AccountingError:
            raise
        except (SQLAlchemyError, ValueError, OSError) as exc:
            raise AccountingError("query execution could not be recorded") from exc

    def begin_provider(self, ticket: AccountingTicket) -> None:
        """Durably mark that the provider may run; the caller invokes it only afterwards."""
        self._advance(ticket, ExecutionState.PREPARED, ExecutionState.PROVIDER_MAY_RUN)

    def note_ambiguous(self, ticket: AccountingTicket, reason: str) -> bool:
        """Record why the outcome is unknown; diagnostic only, never changes the hold."""
        try:
            with transaction(self._engine()) as session:
                return bool(
                    _rowcount(
                        session.execute(
                            update(QueryExecution)
                            .where(
                                QueryExecution.tenant_id == ticket.tenant_id,
                                QueryExecution.id == ticket.execution_id,
                                QueryExecution.state == ExecutionState.PROVIDER_MAY_RUN.value,
                            )
                            .values(ambiguity_reason=reason[:32], updated_at=self._clock())
                            .execution_options(synchronize_session=False)
                        )
                    )
                )
        except (SQLAlchemyError, ValueError, OSError):
            return False

    def record_receipt(
        self,
        ticket: AccountingTicket,
        provider_id: str,
        model_id: str,
        usage: LLMUsage,
        fallback_count: int,
        latency_ms: float,
    ) -> Decimal:
        """Commit the reported usage with the execution-time prices before it is booked."""
        if not usage.reported:
            self.note_ambiguous(ticket, "usage_not_reported")
            raise UsageUnavailable("provider reported no usage")
        model = ticket.models.get((provider_id, model_id))
        if model is None:
            self.note_ambiguous(ticket, "unaccounted_model")
            raise AccountingError("the executed model has no accounting record")
        if usage.prompt_tokens < 0 or usage.completion_tokens < 0 or latency_ms < 0:
            self.note_ambiguous(ticket, "invalid_usage")
            raise UsageUnavailable("provider reported invalid usage")
        cost = model.cost(usage.prompt_tokens, usage.completion_tokens)
        self._advance(
            ticket,
            ExecutionState.PROVIDER_MAY_RUN,
            ExecutionState.RECEIPT_RECORDED,
            provider_id=provider_id,
            model_id_text=model_id,
            model_configuration_id=model.configuration_id,
            input_price=model.input_price,
            output_price=model.output_price,
            input_tokens=usage.prompt_tokens,
            output_tokens=usage.completion_tokens,
            cost=cost,
            latency_ms=latency_ms,
            fallback_count=fallback_count,
        )
        return cost

    def finalize(
        self,
        ticket: AccountingTicket,
        provider_id: str,
        model_id: str,
        usage: LLMUsage,
        fallback_count: int,
        latency_ms: float,
    ) -> BookedUsage:
        """Commit the receipt, then book usage, reservation and ledger in one transaction.

        Idempotent per attempt: an accounted attempt returns its booked usage, and an attempt
        whose receipt is already durable is booked from that receipt.
        """
        state, booked = self._booked(ticket)
        if booked is not None:
            return booked
        if state != ExecutionState.RECEIPT_RECORDED.value:
            self.record_receipt(ticket, provider_id, model_id, usage, fallback_count, latency_ms)
        try:
            with transaction(self._engine()) as session:
                # Lock order (reservation, then ledger) matches reconciliation.
                reservation = self._locked_reservation(
                    session, ticket.tenant_id, ticket.reservation_id
                )
                execution = session.scalar(
                    select(QueryExecution)
                    .where(
                        QueryExecution.tenant_id == ticket.tenant_id,
                        QueryExecution.id == ticket.execution_id,
                    )
                    .with_for_update()
                )
                if (
                    execution is None
                    or execution.state != ExecutionState.RECEIPT_RECORDED.value
                    or execution.reservation_id != reservation.id
                ):
                    raise AccountingError("receipt is no longer open")
                record = settle_receipt(session, execution, reservation, self._clock())
                return BookedUsage(
                    record.id, record.input_tokens, record.output_tokens, record.cost
                )
        except AccountingError:
            raise
        except (SQLAlchemyError, ReservationError, ValueError, OSError, RuntimeError) as exc:
            raise AccountingError("usage could not be recorded") from exc

    def _booked(self, ticket: AccountingTicket) -> tuple[str | None, BookedUsage | None]:
        try:
            with transaction(self._engine()) as session:
                execution = session.scalar(
                    select(QueryExecution).where(
                        QueryExecution.tenant_id == ticket.tenant_id,
                        QueryExecution.id == ticket.execution_id,
                    )
                )
                if execution is None:
                    raise AccountingError("query execution not found")
                if execution.state != ExecutionState.ACCOUNTED.value:
                    return execution.state, None
                record = session.scalar(
                    select(UsageRecord).where(
                        UsageRecord.tenant_id == ticket.tenant_id,
                        UsageRecord.correlation_id == ticket.correlation_id,
                    )
                )
                if record is None:
                    raise AccountingError("accounted execution without usage")
                booked = BookedUsage(
                    record.id, record.input_tokens, record.output_tokens, record.cost
                )
                return execution.state, booked
        except AccountingError:
            raise
        except (SQLAlchemyError, ValueError, OSError) as exc:
            raise AccountingError("usage could not be read") from exc

    def release(self, ticket: AccountingTicket) -> None:
        """Return the budget after a provider rejection that was certainly not billed."""
        self._release(
            ticket,
            (ExecutionState.PROVIDER_MAY_RUN, ExecutionState.PREPARED),
            "provider_rejected",
        )

    def release_unstarted(self, ticket: AccountingTicket) -> None:
        """Return the budget when the boundary marker could not be written (no provider call)."""
        self._release(ticket, (ExecutionState.PREPARED,), "boundary_not_recorded")

    def _release(
        self, ticket: AccountingTicket, sources: tuple[ExecutionState, ...], resolution: str
    ) -> None:
        try:
            with transaction(self._engine()) as session:
                reservation = self._locked_reservation(
                    session, ticket.tenant_id, ticket.reservation_id
                )
                execution = session.scalar(
                    select(QueryExecution)
                    .where(
                        QueryExecution.tenant_id == ticket.tenant_id,
                        QueryExecution.id == ticket.execution_id,
                    )
                    .with_for_update()
                )
                if execution is None or execution.state not in {s.value for s in sources}:
                    raise AccountingError("query execution cannot be released")
                BudgetReservationService(session, ticket.tenant_id).release(reservation)
                now = self._clock()
                execution.state = ExecutionState.RELEASED.value
                execution.resolution, execution.resolved_at = resolution, now
                execution.updated_at = now
        except AccountingError:
            raise
        except (SQLAlchemyError, ReservationError, ValueError, OSError) as exc:
            raise AccountingError("reservation could not be released") from exc
