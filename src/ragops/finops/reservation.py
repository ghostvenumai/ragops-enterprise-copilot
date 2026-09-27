from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ragops.persistence.models import BudgetReservation, TenantBudget, UsageRecord


class ReservationError(ValueError):
    pass


class BudgetReservationService:
    """PostgreSQL-safe reservation service; callers must commit the transaction."""

    def __init__(self, session: Session, tenant_id: str) -> None:
        if not tenant_id:
            raise ReservationError("tenant is required")
        self.session, self.tenant_id = session, tenant_id

    def reserve(
        self, budget_id: UUID, estimated_amount: Decimal, idempotency_key: str
    ) -> BudgetReservation:
        if estimated_amount < 0 or not idempotency_key:
            raise ReservationError("invalid reservation")
        existing = self.session.scalar(
            select(BudgetReservation).where(
                BudgetReservation.tenant_id == self.tenant_id,
                BudgetReservation.idempotency_key == idempotency_key,
            )
        )
        if existing is not None:
            return existing
        budget = self.session.scalar(
            select(TenantBudget)
            .where(TenantBudget.tenant_id == self.tenant_id, TenantBudget.id == budget_id)
            .with_for_update()
        )
        if budget is None or budget.currency != "EUR":
            raise ReservationError("budget not found or currency mismatch")
        committed = (
            self.session.scalar(
                select(func.coalesce(func.sum(UsageRecord.cost), 0)).where(
                    UsageRecord.tenant_id == self.tenant_id,
                    UsageRecord.created_at >= budget.period_start,
                    UsageRecord.created_at < budget.period_end,
                )
            )
            or 0
        )
        reserved = (
            self.session.scalar(
                select(func.coalesce(func.sum(BudgetReservation.estimated_amount), 0)).where(
                    BudgetReservation.tenant_id == self.tenant_id,
                    BudgetReservation.budget_id == budget.id,
                    BudgetReservation.status == "reserved",
                )
            )
            or 0
        )
        hard_limit = budget.budget_amount * (budget.hard_limit_threshold or Decimal("1"))
        if (
            Decimal(str(committed)) + Decimal(str(reserved)) + estimated_amount > hard_limit
            and budget.enforcement_mode == "hard_limit"
        ):
            raise ReservationError("budget hard limit exceeded")
        reservation = BudgetReservation(
            tenant_id=self.tenant_id,
            budget_id=budget.id,
            idempotency_key=idempotency_key,
            estimated_amount=estimated_amount,
            currency=budget.currency,
        )
        self.session.add(reservation)
        self.session.flush()
        return reservation

    def commit(
        self, reservation: BudgetReservation, actual_amount: Decimal | None = None
    ) -> BudgetReservation:
        self._owned(reservation)
        if reservation.status != "reserved":
            return reservation
        amount = actual_amount if actual_amount is not None else reservation.estimated_amount
        if amount < 0:
            raise ReservationError("invalid final amount")
        reservation.final_amount = amount
        reservation.status = "committed"
        reservation.committed_at = datetime.now(UTC)
        return reservation

    def release(self, reservation: BudgetReservation) -> BudgetReservation:
        self._owned(reservation)
        if reservation.status == "reserved":
            reservation.status = "released"
        return reservation

    def _owned(self, reservation: BudgetReservation) -> None:
        if reservation.tenant_id != self.tenant_id:
            raise ReservationError("reservation not found")
