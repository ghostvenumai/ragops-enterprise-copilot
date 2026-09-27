"""FinOps accounting contracts on the real services with a migrated SQLite schema."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest

pytest.importorskip("alembic", reason="Install declared persistence extra; product gate blocks")

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session

from ragops.finops.reservation import BudgetReservationService, ReservationError
from ragops.finops.service import FinOpsService, budget_decision
from ragops.modeling.router import ComplexitySignals, LLMModelRouter, ModelSpec
from ragops.modeling.usage import UsageService
from ragops.persistence.models import (
    ModelConfiguration,
    ProviderConfiguration,
    Tenant,
    TenantBudget,
    User,
)

START, END = date(2026, 1, 1), date(2026, 2, 1)


@pytest.fixture
def session(tmp_path: Path) -> Iterator[Session]:
    engine = create_engine(f"sqlite:///{tmp_path / 'finops.db'}")

    @event.listens_for(engine, "connect")
    def enable_foreign_keys(connection: object, record: object) -> None:
        connection.execute("PRAGMA foreign_keys=ON")  # type: ignore[attr-defined]

    with engine.begin() as connection:
        cfg = Config("alembic.ini")
        cfg.attributes["connection"] = connection
        command.upgrade(cfg, "head")
    with Session(engine) as db:
        db.add(Tenant(id="tenant-a", name="A"))
        db.flush()
        yield db
    engine.dispose()


def budget(session: Session, **overrides: object) -> TenantBudget:
    values: dict[str, object] = {
        "tenant_id": "tenant-a",
        "period_start": START,
        "period_end": END,
        "budget_amount": Decimal("100"),
        "hard_limit_threshold": Decimal("1.2"),
        "enforcement_mode": "hard_limit",
    }
    values.update(overrides)
    record = TenantBudget(**values)
    session.add(record)
    session.flush()
    return record


@pytest.mark.parametrize(
    ("estimate", "decision"),
    [
        (Decimal("79.99999999"), "ALLOW"),
        (Decimal("80"), "ALLOW_WITH_WARNING"),
        (Decimal("99.99999999"), "ALLOW_WITH_WARNING"),
        (Decimal("100"), "ROUTE_CHEAPER"),
        (Decimal("119.99999999"), "ROUTE_CHEAPER"),
        (Decimal("120"), "DENY_BUDGET_LIMIT"),
        (Decimal("120.00000001"), "DENY_BUDGET_LIMIT"),
    ],
)
def test_preflight_and_reservation_agree_on_every_boundary(session, estimate, decision) -> None:
    record = budget(session)
    assert FinOpsService(session, "tenant-a").preflight(record, estimate).decision == decision
    reservations = BudgetReservationService(session, "tenant-a")
    if decision == "DENY_BUDGET_LIMIT":
        with pytest.raises(ReservationError):
            reservations.reserve(record.id, estimate, "boundary")
    else:
        assert reservations.reserve(record.id, estimate, "boundary").status == "reserved"


def test_missing_hard_threshold_fails_closed_at_budget_in_both_paths(session) -> None:
    record = budget(session, soft_limit_threshold=Decimal("0.9"))
    # On INSERT the ORM replaces None with the column default; NULL arrives via UPDATE.
    record.hard_limit_threshold = None
    session.flush()
    assert (
        FinOpsService(session, "tenant-a").preflight(record, Decimal("100")).decision
        == "DENY_BUDGET_LIMIT"
    )
    with pytest.raises(ReservationError):
        BudgetReservationService(session, "tenant-a").reserve(record.id, Decimal("100"), "k")
    assert (
        BudgetReservationService(session, "tenant-a").reserve(record.id, Decimal("99"), "j").status
        == "reserved"
    )


def test_expired_reservation_frees_capacity_and_is_persisted(session) -> None:
    record = budget(session, budget_amount=Decimal("10"), hard_limit_threshold=Decimal("1"))
    service = BudgetReservationService(session, "tenant-a")
    now = datetime(2026, 1, 10, 12, tzinfo=UTC)
    stale = service.reserve(
        record.id, Decimal("6"), "stale", expires_at=now + timedelta(hours=1), now=now
    )
    with pytest.raises(ReservationError):
        service.reserve(record.id, Decimal("6"), "blocked", now=now)
    later = now + timedelta(hours=2)
    assert service.reserve(record.id, Decimal("6"), "fresh", now=later).status == "reserved"
    assert service.expire_stale(later) == 1
    assert stale.status == "expired"
    assert service.expire_stale(later) == 0
    with pytest.raises(ReservationError):
        service.commit(stale, now=later)
    with pytest.raises(ReservationError):
        service.release(stale)


def test_invalid_transitions_fail_closed_and_repeats_are_idempotent(session) -> None:
    record = budget(session)
    service = BudgetReservationService(session, "tenant-a")
    committed = service.reserve(record.id, Decimal("5"), "committed")
    released = service.reserve(record.id, Decimal("5"), "released")
    service.commit(committed, Decimal("4"))
    assert service.commit(committed, Decimal("99")).final_amount == Decimal("4")
    service.release(released)
    assert service.release(released).status == "released"
    with pytest.raises(ReservationError):
        service.commit(released)
    with pytest.raises(ReservationError):
        service.release(committed)
    assert (committed.status, released.status) == ("committed", "released")


@pytest.mark.parametrize(
    "amount", [Decimal("-0.00000001"), Decimal("NaN"), Decimal("Infinity"), 1.5]
)
def test_invalid_amounts_are_rejected_in_every_mode(session, amount) -> None:
    record = budget(session, enforcement_mode="optimize")
    service = BudgetReservationService(session, "tenant-a")
    with pytest.raises(ReservationError):
        service.reserve(record.id, amount, "invalid")
    reservation = service.reserve(record.id, Decimal("1"), "valid")
    with pytest.raises(ReservationError):
        service.commit(reservation, amount)
    with pytest.raises(ValueError):
        FinOpsService(session, "tenant-a").preflight(record, amount)


def test_zero_amount_reservation_is_deterministic(session) -> None:
    record = budget(session, budget_amount=Decimal("10"), hard_limit_threshold=Decimal("1"))
    service = BudgetReservationService(session, "tenant-a")
    zero = service.reserve(record.id, Decimal("0"), "zero")
    assert service.reserve(record.id, Decimal("9.99999999"), "rest").status == "reserved"
    assert service.commit(zero).final_amount == Decimal("0")


def test_forecast_never_drops_below_spend_after_period_end(session) -> None:
    user = User(tenant_id="tenant-a", issuer="https://issuer.invalid/", subject="u")
    provider = ProviderConfiguration(tenant_id="tenant-a", name="p", kind="deterministic")
    session.add_all([user, provider])
    session.flush()
    model = ModelConfiguration(
        tenant_id="tenant-a",
        provider_id=provider.id,
        name="m",
        input_price=Decimal("0"),
        output_price=Decimal("0"),
    )
    session.add(model)
    session.flush()
    router = LLMModelRouter(
        [ModelSpec("p", "m", "m", input_cost_per_token=0.1, output_cost_per_token=0.2)]
    )
    signals = ComplexitySignals(estimated_input_tokens=1, expected_output_tokens=1)
    decision = router.route("tenant-a", signals)
    record = UsageService(session, "tenant-a", user.id, model.id).record(
        decision,
        input_tokens=1,
        output_tokens=1,
        actual_cost=None,
        latency_ms=1.0,
        correlation_id=uuid4(),
    )
    assert record.cost == Decimal("0.30000000")
    record.created_at = datetime(2026, 1, 5, tzinfo=UTC)
    session.flush()
    finops = FinOpsService(session, "tenant-a")
    assert finops.forecast(START, END, today=date(2026, 3, 15)) == Decimal("0.30000000")
    assert finops.forecast(START, END, today=date(2026, 1, 31)) == Decimal("0.30000000")
    assert FinOpsService(session, "tenant-b").forecast(START, END, today=date(2026, 1, 10)) == 0


@pytest.mark.parametrize(
    ("estimate", "budget_amount", "decision"),
    [
        (Decimal("0.9"), Decimal("1"), "ALLOW_WITH_WARNING"),
        (Decimal("8.5"), Decimal("10"), "ALLOW_WITH_WARNING"),
        (Decimal("11.99"), Decimal("10"), "ROUTE_CHEAPER"),
        (Decimal("12"), Decimal("10"), "DENY_BUDGET_LIMIT"),
        (Decimal("1"), Decimal("10"), "ALLOW"),
    ],
)
def test_pure_decision_matches_preflight_thresholds(estimate, budget_amount, decision) -> None:
    policy = TenantBudget(
        tenant_id="tenant-a",
        period_start=START,
        period_end=END,
        budget_amount=budget_amount,
        currency="EUR",
        warning_threshold=Decimal("0.8"),
        soft_limit_threshold=Decimal("1"),
        hard_limit_threshold=Decimal("1.2"),
        enforcement_mode="hard_limit",
    )
    assert budget_decision(policy, Decimal("0"), estimate).decision == decision
