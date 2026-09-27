from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from ragops.finops.service import hard_limit_ratio, valid_amount
from ragops.persistence.models import BudgetReservation, TenantBudget, UsageRecord


class ReservationError(ValueError):
    pass


def _utc(value: datetime) -> datetime:
    # SQLite returns naive datetimes for timezone-aware columns; they are stored as UTC.
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


class BudgetReservationService:
    """PostgreSQL-safe reservation service; callers must commit the transaction.

    Lifecycle: reserved -> committed | released | expired. Repeating the same
    terminal transition is idempotent; any other transition out of a terminal
    state is rejected. Usage records remain the source of truth for spend, so
    commit only closes the reservation and never books usage itself.
    """

    def __init__(self, session: Session, tenant_id: str) -> None:
        if not tenant_id:
            raise ReservationError("tenant is required")
        self.session, self.tenant_id = session, tenant_id

    def reserve(
        self,
        budget_id: UUID,
        estimated_amount: Decimal,
        idempotency_key: str,
        *,
        expires_at: datetime | None = None,
        now: datetime | None = None,
    ) -> BudgetReservation:
        if not valid_amount(estimated_amount) or not idempotency_key:
            raise ReservationError("invalid reservation")
        if expires_at is not None and expires_at.tzinfo is None:
            raise ReservationError("reservation expiry must be timezone-aware")
        now = now or datetime.now(UTC)
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
                    or_(
                        BudgetReservation.expires_at.is_(None),
                        BudgetReservation.expires_at > now,
                    ),
                )
            )
            or 0
        )
        hard_limit = budget.budget_amount * hard_limit_ratio(budget)
        # Same boundary as preflight: reaching the hard threshold is denied.
        if (
            Decimal(str(committed)) + Decimal(str(reserved)) + estimated_amount >= hard_limit
            and budget.enforcement_mode == "hard_limit"
        ):
            raise ReservationError("budget hard limit exceeded")
        reservation = BudgetReservation(
            tenant_id=self.tenant_id,
            budget_id=budget.id,
            idempotency_key=idempotency_key,
            estimated_amount=estimated_amount,
            currency=budget.currency,
            expires_at=expires_at,
        )
        self.session.add(reservation)
        self.session.flush()
        return reservation

    def commit(
        self,
        reservation: BudgetReservation,
        actual_amount: Decimal | None = None,
        *,
        now: datetime | None = None,
    ) -> BudgetReservation:
        self._owned(reservation)
        if reservation.status == "committed":
            return reservation
        if reservation.status != "reserved" or self._expired(reservation, now):
            raise ReservationError("reservation is not active")
        amount = actual_amount if actual_amount is not None else reservation.estimated_amount
        if not valid_amount(amount):
            raise ReservationError("invalid final amount")
        reservation.final_amount = amount
        reservation.status = "committed"
        reservation.committed_at = now or datetime.now(UTC)
        return reservation

    def release(self, reservation: BudgetReservation) -> BudgetReservation:
        self._owned(reservation)
        if reservation.status == "released":
            return reservation
        if reservation.status != "reserved":
            raise ReservationError("reservation is not active")
        reservation.status = "released"
        return reservation

    def expire_stale(self, now: datetime | None = None) -> int:
        """Persist the expired state for this tenant's reservations past their expiry."""
        now = now or datetime.now(UTC)
        stale = self.session.scalars(
            select(BudgetReservation).where(
                BudgetReservation.tenant_id == self.tenant_id,
                BudgetReservation.status == "reserved",
                BudgetReservation.expires_at.is_not(None),
                BudgetReservation.expires_at <= now,
            )
        ).all()
        for reservation in stale:
            reservation.status = "expired"
        self.session.flush()
        return len(stale)

    @staticmethod
    def _expired(reservation: BudgetReservation, now: datetime | None) -> bool:
        expires_at = reservation.expires_at
        return expires_at is not None and _utc(expires_at) <= (now or datetime.now(UTC))

    def _owned(self, reservation: BudgetReservation) -> None:
        if reservation.tenant_id != self.tenant_id:
            raise ReservationError("reservation not found")
