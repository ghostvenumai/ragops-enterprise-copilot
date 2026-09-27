from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from ragops.finops.reservation import BudgetReservationService, ReservationError
from ragops.persistence.models import Tenant, TenantBudget


def test_reservation_idempotency_and_release(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'reservation.db'}")
    with engine.begin() as connection:
        cfg = Config("alembic.ini")
        cfg.attributes["connection"] = connection
        command.upgrade(cfg, "head")
    with Session(engine) as session:
        tenant = Tenant(id="tenant-a", name="A")
        session.add(tenant)
        session.flush()
        budget = TenantBudget(
            tenant_id="tenant-a",
            period_start=date(2026, 1, 1),
            period_end=date(2026, 2, 1),
            budget_amount=Decimal("10"),
            enforcement_mode="hard_limit",
            hard_limit_threshold=Decimal("1"),
        )
        session.add(budget)
        session.flush()
        service = BudgetReservationService(session, "tenant-a")
        first = service.reserve(budget.id, Decimal("6"), "request-1")
        assert service.reserve(budget.id, Decimal("6"), "request-1").id == first.id
        with __import__("pytest").raises(ReservationError):
            service.reserve(budget.id, Decimal("6"), "request-2")
        service.release(first)
        session.commit()
    engine.dispose()
