from __future__ import annotations

import json
import os
import threading
from datetime import date
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, delete, func, select
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from ragops.finops.reservation import BudgetReservationService, ReservationError
from ragops.persistence.models import BudgetReservation, Tenant, TenantBudget


@pytest.mark.integration
def test_postgres_budget_reservation_race() -> None:
    raw = os.getenv("RAGOPS_TEST_CONCURRENCY_DATABASE_URL") or os.getenv("RAGOPS_TEST_DATABASE_URL")
    if not raw:
        pytest.skip("RAGOPS_TEST_CONCURRENCY_DATABASE_URL/RAGOPS_TEST_DATABASE_URL absent")
    url = make_url(raw)
    if (
        url.drivername != "postgresql+psycopg"
        or not url.database
        or not url.database.startswith("ragops_test_")
    ):
        pytest.fail("Concurrency gate requires an isolated ragops_test_* PostgreSQL database")
    engine = create_engine(url, hide_parameters=True, connect_args={"connect_timeout": 5})
    tenant_id = f"race-{uuid4().hex[:16]}"
    with Session(engine) as session, session.begin():
        session.add(Tenant(id=tenant_id, name="Concurrency Test"))
        budget = TenantBudget(
            tenant_id=tenant_id,
            period_start=date(2026, 1, 1),
            period_end=date(2026, 2, 1),
            budget_amount=Decimal("10.00"),
            hard_limit_threshold=Decimal("1.00"),
            enforcement_mode="hard_limit",
        )
        session.add(budget)
        session.flush()
        budget_id = budget.id
    barrier = threading.Barrier(2)
    outcomes: list[str] = []

    def attempt(index: int) -> None:
        with Session(engine) as session:
            barrier.wait()
            try:
                BudgetReservationService(session, tenant_id).reserve(
                    budget_id, Decimal("6.00"), f"race-{index}"
                )
                session.commit()
                outcomes.append("accepted")
            except ReservationError:
                session.rollback()
                outcomes.append("denied")

    threads = [threading.Thread(target=attempt, args=(index,)) for index in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    with Session(engine) as session:
        total = session.scalar(
            select(func.coalesce(func.sum(BudgetReservation.estimated_amount), 0)).where(
                BudgetReservation.tenant_id == tenant_id, BudgetReservation.status == "reserved"
            )
        )
        assert outcomes.count("accepted") == 1 and outcomes.count("denied") == 1
        assert Decimal(str(total or 0)) <= Decimal("10.00")
        print(
            "CONCURRENCY_RESULT "
            + json.dumps(
                {
                    "hard_limit": "10.00",
                    "attempts": 2,
                    "amount_each": "6.00",
                    "accepted": outcomes.count("accepted"),
                    "denied": outcomes.count("denied"),
                    "final_reserved": str(total or 0),
                    "invariant_ok": Decimal(str(total or 0)) <= Decimal("10.00"),
                }
            )
        )
        session.execute(delete(BudgetReservation).where(BudgetReservation.tenant_id == tenant_id))
        session.execute(delete(TenantBudget).where(TenantBudget.tenant_id == tenant_id))
        session.execute(delete(Tenant).where(Tenant.id == tenant_id))
        session.commit()
    engine.dispose()
