"""PostgreSQL: reconciliation is the financial authority under concurrency (ENT-11.6).

Skipped unless RAGOPS_TEST_DATABASE_URL points at an isolated ragops_test_* database. Each
test creates and drops its own database and touches nothing else.
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest

pytest.importorskip("alembic", reason="Install declared persistence extra; product gate blocks")

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.orm import Session
from tests.integration.test_query_finops_postgres import BASE, database  # noqa: F401
from tests.security.reconciliation_helpers import QUESTION, Scenario

from ragops.finops.reconciliation import Classification, ReconciliationService
from ragops.persistence.models import BudgetReservation, QueryExecution, UsageRecord
from ragops.workflows.query_idempotency import Decision, request_fingerprint

pytestmark = pytest.mark.integration
RECONCILERS = 10


def race(engine, tenant: str, target) -> list:
    """Ten reconcilers on separate connections start together on the same case."""
    barrier = threading.Barrier(RECONCILERS)

    def one():
        service = ReconciliationService(lambda: engine, tenant, grace_seconds=0)
        barrier.wait(timeout=10)
        return service.reconcile(target)

    with ThreadPoolExecutor(max_workers=RECONCILERS) as pool:
        return [future.result() for future in [pool.submit(one) for _ in range(RECONCILERS)]]


def test_ten_concurrent_reconcilers_book_a_receipt_once(database) -> None:  # noqa: F811
    scenario = Scenario(database)
    started = scenario.receipt_then_crash()
    outcomes = race(database, "tenant-a", started.ticket.reservation_id)
    assert sum(outcome.changed for outcome in outcomes) == 1
    assert {o.classification for o in outcomes if not o.changed} <= {Classification.RESOLVED}
    assert scenario.count(UsageRecord) == 1
    assert scenario.count(BudgetReservation, status="committed") == 1
    snap = scenario.snapshot()
    assert snap["ledger"] == [(1, "accounted", "receipt_available")]
    assert snap["idempotency"] == ["reconciled"]


def test_ten_concurrent_reconcilers_release_a_pre_provider_case_once(database) -> None:  # noqa: F811
    scenario = Scenario(database)
    started = scenario.crash_before_boundary()
    outcomes = race(database, "tenant-a", started.ticket.reservation_id)
    assert sum(outcome.changed for outcome in outcomes) == 1
    assert scenario.snapshot()["reservations"] == [("released", "None")]
    assert scenario.snapshot()["idempotency"] == ["retryable"]
    claim = scenario.store.claim(started.scope, request_fingerprint(QUESTION, 3))
    assert claim.decision is Decision.EXECUTE


def test_ten_concurrent_reconcilers_never_release_an_ambiguous_case(database) -> None:  # noqa: F811
    scenario = Scenario(database)
    started = scenario.provider_timeout()
    before = scenario.snapshot()
    outcomes = race(database, "tenant-a", started.ticket.reservation_id)
    assert not any(outcome.changed for outcome in outcomes)
    assert {outcome.classification for outcome in outcomes} == {Classification.UNKNOWN_BILLING}
    assert scenario.snapshot() == before


def test_a_reconciler_racing_the_boundary_marker_wins_or_loses_cleanly(database) -> None:  # noqa: F811
    from ragops.finops.query_accounting import AccountingError

    for _ in range(5):
        scenario = Scenario(database)
        tenant = f"tenant-{uuid4().hex[:8]}"
        started = scenario.crash_before_boundary(tenant, key=None)
        barrier = threading.Barrier(2)

        def mark(scenario=scenario, started=started, barrier=barrier):
            barrier.wait(timeout=10)
            try:
                scenario.accounting.begin_provider(started.ticket)
                return "crossed"
            except AccountingError:
                return "refused"

        def reconcile(tenant=tenant, started=started, barrier=barrier):
            barrier.wait(timeout=10)
            service = ReconciliationService(lambda: database, tenant, grace_seconds=0)
            return service.reconcile(started.ticket.reservation_id).action

        with ThreadPoolExecutor(max_workers=2) as pool:
            marked = pool.submit(mark)
            reconciled = pool.submit(reconcile)
            outcome = (marked.result(), reconciled.result())
        status = scenario.reservation(started).status
        # Either the provider may run and the hold stays, or it may not and the budget returns.
        assert outcome in {("crossed", "none"), ("refused", "released")}
        assert status == ("reserved" if outcome[0] == "crossed" else "released")


def test_postgres_rejects_a_second_ledger_row_for_one_reservation(database) -> None:  # noqa: F811
    from sqlalchemy.exc import IntegrityError

    scenario = Scenario(database)
    scenario.start(key=None)
    with Session(database) as session:
        (row,) = session.scalars(select(QueryExecution))
        clone = {
            column.key: getattr(row, column.key)
            for column in QueryExecution.__table__.columns
            if column.key not in ("id", "attempt")
        }
    with pytest.raises(IntegrityError), Session(database) as session, session.begin():
        session.add(QueryExecution(**clone, attempt=2))


# --------------------------------------------------------------------------- migration


def _config(connection) -> Config:
    cfg = Config("alembic.ini")
    cfg.attributes["connection"] = connection
    return cfg


def test_upgrade_from_ent115_keeps_every_legacy_hold(monkeypatch) -> None:
    from sqlalchemy.engine import make_url

    if not BASE.startswith("postgresql+psycopg://"):
        pytest.skip("RAGOPS_TEST_DATABASE_URL is not an isolated PostgreSQL test database")
    base = make_url(BASE)
    name = f"ragops_test_ent116m_{uuid4().hex[:10]}"
    admin = create_engine(base, isolation_level="AUTOCOMMIT")
    with admin.connect() as connection:
        connection.execute(text(f'CREATE DATABASE "{name}"'))
    engine = create_engine(base.set(database=name))
    try:
        with engine.begin() as connection:
            command.upgrade(_config(connection), "0007_query_idempotency")
        legacy = uuid4()
        with engine.begin() as connection:
            connection.execute(
                text("INSERT INTO tenants (id, name, active) VALUES ('t', 't', true)")
            )
            budget = uuid4()
            connection.execute(
                text(
                    "INSERT INTO tenant_budgets (tenant_id, id, created_at, period_start, "
                    "period_end, budget_amount, currency, warning_threshold, soft_limit_threshold, "
                    "hard_limit_threshold, enforcement_mode) VALUES ('t', :b, now(), "
                    "current_date - 1, current_date + 30, 100, 'EUR', 0.8, 1, 1, 'hard_limit')"
                ),
                {"b": budget},
            )
            connection.execute(
                text(
                    "INSERT INTO budget_reservations (tenant_id, id, created_at, budget_id, "
                    "idempotency_key, estimated_amount, currency, status) VALUES "
                    "('t', :r, now(), :b, :k, 0.5, 'EUR', 'reserved')"
                ),
                {"r": legacy, "b": budget, "k": f"query:{uuid4()}"},
            )
            connection.execute(
                text(
                    "INSERT INTO query_idempotency_records (tenant_id, id, created_at, principal, "
                    "endpoint, key_hash, request_fingerprint, correlation_id, state, attempt, "
                    "updated_at, lease_expires_at, expires_at) VALUES ('t', :i, now(), 'p', "
                    "'POST /v1/query', 'h', 'f', :c, 'ambiguous_held', 1, now(), now(), now())"
                ),
                {"i": uuid4(), "c": uuid4()},
            )
        with engine.begin() as connection:
            command.upgrade(_config(connection), "head")
            assert "query_executions" in inspect(connection).get_table_names()
            assert connection.execute(text("SELECT count(*) FROM query_executions")).scalar() == 0
            assert (
                connection.execute(
                    text("SELECT status FROM budget_reservations WHERE id = :r"), {"r": legacy}
                ).scalar()
                == "reserved"
            )
            assert (
                connection.execute(text("SELECT state FROM query_idempotency_records")).scalar()
                == "ambiguous_held"
            )
        (case,) = ReconciliationService(lambda: engine, "t", grace_seconds=0).cases()
        assert case.legacy and case.classification is Classification.UNKNOWN_BILLING
        assert (
            not ReconciliationService(lambda: engine, "t", grace_seconds=0)
            .reconcile(legacy)
            .changed
        )
        with engine.begin() as connection:
            command.downgrade(_config(connection), "0007_query_idempotency")
            assert "query_executions" not in inspect(connection).get_table_names()
            assert (
                connection.execute(
                    text("SELECT status FROM budget_reservations WHERE id = :r"), {"r": legacy}
                ).scalar()
                == "reserved"
            )
            command.upgrade(_config(connection), "head")
    finally:
        engine.dispose()
        with admin.connect() as connection:
            connection.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()


def test_the_reconciled_state_is_allowed_only_after_the_migration(database) -> None:  # noqa: F811
    with database.begin() as connection:
        definition = connection.execute(
            text(
                "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                "WHERE conname = 'ck_query_idempotency_records_state'"
            )
        ).scalar()
    assert "reconciled" in definition and "ambiguous_held" in definition
