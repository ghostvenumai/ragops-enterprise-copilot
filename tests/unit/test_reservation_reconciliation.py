"""Reservation reconciliation: durable provider boundary, receipts and safe recovery (ENT-11.6).

Every crash window of the productive query path is reproduced by running the real workflow
steps up to the boundary and nothing after it. Reconciliation must never call a provider,
never release a reservation whose provider may have billed, never book unknown usage and
never cross a tenant.
"""

from __future__ import annotations

from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import inspect as sa_inspect
from sqlalchemy import select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from tests.security.finops_helpers import INPUT_PRICE, OUTPUT_PRICE, migrated_sqlite
from tests.security.reconciliation_helpers import (
    COMPLETION_TOKENS,
    PROMPT_TOKENS,
    QUESTION,
    Scenario,
    context,
    decision,
    usage,
)

from ragops.finops import reconciliation as reconciliation_module
from ragops.finops.query_accounting import AccountingError, UsageUnavailable
from ragops.finops.reconciliation import (
    CaseNotFound,
    Classification,
    ReconciliationService,
    ReconciliationUnavailable,
)
from ragops.finops.reservation import BudgetReservationService
from ragops.persistence.database import transaction
from ragops.persistence.models import (
    BudgetReservation,
    QueryExecution,
    QueryIdempotencyRecord,
    TenantBudget,
    UsageRecord,
)
from ragops.workflows.query_idempotency import (
    RECONCILED_DETAIL,
    TRANSITIONS,
    Decision,
    IdempotencyState,
    request_fingerprint,
)

EXPECTED_COST = (PROMPT_TOKENS * INPUT_PRICE + COMPLETION_TOKENS * OUTPUT_PRICE).quantize(
    Decimal("0.00000001")
)


@pytest.fixture
def scenario(tmp_path) -> Scenario:
    engine, _ = migrated_sqlite(tmp_path / "reconcile.db")
    return Scenario(engine)


@pytest.fixture(autouse=True)
def no_provider(monkeypatch):
    """Reconciliation has no provider dependency; any adapter call fails the test."""
    from ragops.llm import providers

    def forbidden(*args, **kwargs):
        raise AssertionError("reconciliation must never call a provider")

    monkeypatch.setattr(providers.DeterministicTestProvider, "generate", forbidden)


def ledger(scenario: Scenario, tenant: str = "tenant-a") -> list[QueryExecution]:
    with Session(scenario.engine) as session:
        rows = list(
            session.scalars(
                select(QueryExecution)
                .where(QueryExecution.tenant_id == tenant)
                .order_by(QueryExecution.attempt)
            )
        )
        for row in rows:
            session.expunge(row)
        return rows


# --------------------------------------------------------------------------- ledger contract


def test_the_ledger_has_scoped_unique_constraints_and_no_content_columns(scenario) -> None:
    columns = {c["name"] for c in sa_inspect(scenario.engine).get_columns("query_executions")}
    for forbidden in ("question", "prompt", "answer", "context", "response", "key", "token"):
        assert not any(forbidden == name or name.startswith(forbidden + "_") for name in columns)
    uniques = {
        tuple(item["column_names"])
        for item in sa_inspect(scenario.engine).get_unique_constraints("query_executions")
    }
    assert ("tenant_id", "reservation_id") in uniques
    assert ("tenant_id", "correlation_id", "attempt") in uniques


def test_prepare_writes_reservation_and_ledger_atomically(scenario) -> None:
    started = scenario.start()
    (row,) = ledger(scenario)
    assert row.state == "prepared" and row.reservation_id == started.ticket.reservation_id
    assert row.correlation_id == started.ticket.correlation_id and row.attempt == 1
    assert row.input_tokens is None and row.cost is None  # nothing is known yet
    assert scenario.reservation(started).status == "reserved"


def test_a_failing_ledger_insert_leaves_no_reservation(scenario, monkeypatch) -> None:
    scenario.tenant("tenant-a")
    with scenario.engine.begin() as connection:
        connection.execute(text("DROP TABLE query_executions"))
    with pytest.raises(AccountingError):
        scenario.accounting.prepare(
            context("tenant-a"),
            decision(),
            input_tokens=PROMPT_TOKENS,
            output_tokens=256,
            correlation_id=uuid4(),
        )
    assert scenario.count(BudgetReservation) == 0


def test_the_boundary_marker_is_conditional_and_fails_closed(scenario) -> None:
    started = scenario.start()
    scenario.accounting.begin_provider(started.ticket)
    assert ledger(scenario)[0].state == "provider_may_run"
    with pytest.raises(AccountingError):  # never twice: the provider would run twice
        scenario.accounting.begin_provider(started.ticket)


def test_the_boundary_marker_cannot_cross_after_a_release(scenario) -> None:
    started = scenario.start()
    scenario.service().reconcile(started.ticket.reservation_id)
    with pytest.raises(AccountingError):
        scenario.accounting.begin_provider(started.ticket)


def test_unreported_usage_is_never_a_receipt(scenario) -> None:
    started = scenario.crash_after_boundary()
    with pytest.raises(UsageUnavailable):
        scenario.accounting.record_receipt(
            started.ticket, "deterministic", "deterministic-ragops-v1", usage(False), 0, 1.0
        )
    (row,) = ledger(scenario)
    assert row.state == "provider_may_run" and row.ambiguity_reason == "usage_not_reported"
    assert row.input_tokens is None and row.output_tokens is None and row.cost is None
    case = scenario.service().inspect(started.ticket.reservation_id)
    assert case.classification is Classification.UNKNOWN_BILLING


def test_the_receipt_holds_the_execution_time_price_snapshot(scenario) -> None:
    started = scenario.receipt_then_crash()
    (row,) = ledger(scenario)
    assert row.state == "receipt_recorded"
    assert (row.input_tokens, row.output_tokens) == (PROMPT_TOKENS, COMPLETION_TOKENS)
    assert (row.input_price, row.output_price) == (INPUT_PRICE, OUTPUT_PRICE)
    assert row.cost == EXPECTED_COST and row.provider_id == "deterministic"
    assert row.model_configuration_id is not None and row.fallback_count == 0
    assert started.ticket.reservation_id == row.reservation_id


def test_the_normal_path_settles_usage_reservation_and_ledger_together(scenario) -> None:
    started = scenario.crash_after_boundary()
    booked = scenario.accounting.finalize(
        started.ticket, "deterministic", "deterministic-ragops-v1", usage(), 0, 9.5
    )
    assert booked.cost == EXPECTED_COST
    assert scenario.snapshot()["reservations"] == [("committed", str(EXPECTED_COST))]
    assert ledger(scenario)[0].state == "accounted"
    assert scenario.count(UsageRecord) == 1


# --------------------------------------------------------------------------- crash windows


def test_crash_before_the_boundary_releases_and_makes_the_key_retryable(scenario) -> None:
    started = scenario.crash_before_boundary()
    service = scenario.service()
    case = service.inspect(started.ticket.reservation_id)
    assert case.classification is Classification.PRE_PROVIDER
    outcome = service.reconcile(started.ticket.reservation_id)
    assert outcome.action == "released" and outcome.changed
    snap = scenario.snapshot()
    assert snap["reservations"] == [("released", "None")] and snap["usage"] == []
    assert snap["ledger"] == [(1, "released", "pre_provider")]
    assert snap["idempotency"] == ["retryable"]
    # The same key executes again as the same logical query, with a fresh reservation.
    claim = scenario.store.claim(started.scope, request_fingerprint(QUESTION, 3))
    assert claim.decision is Decision.EXECUTE
    assert claim.correlation_id == started.ticket.correlation_id
    scenario.store.enter_billing(started.scope, claim)
    retry = scenario.accounting.prepare(
        context("tenant-a"),
        decision(),
        input_tokens=PROMPT_TOKENS,
        output_tokens=256,
        correlation_id=claim.correlation_id,
    )
    assert retry.reservation_id != started.ticket.reservation_id and retry.attempt == 2
    scenario.accounting.begin_provider(retry)
    scenario.accounting.finalize(retry, "deterministic", "deterministic-ragops-v1", usage(), 0, 1)
    assert scenario.snapshot()["ledger"] == [(1, "released", "pre_provider"), (2, "accounted", "")]
    assert scenario.count(UsageRecord) == 1


def test_a_prepared_attempt_is_reused_by_the_same_logical_query(scenario) -> None:
    first = scenario.crash_before_boundary(key=None)
    again = scenario.accounting.prepare(
        context("tenant-a"),
        decision(),
        input_tokens=PROMPT_TOKENS,
        output_tokens=256,
        correlation_id=first.ticket.correlation_id,
    )
    assert again.reservation_id == first.ticket.reservation_id
    assert again.execution_id == first.ticket.execution_id
    assert scenario.count(BudgetReservation) == 1 and len(ledger(scenario)) == 1
    scenario.accounting.begin_provider(again)
    with pytest.raises(AccountingError):  # one ticket crosses the boundary, never two
        scenario.accounting.begin_provider(first.ticket)


def test_a_too_small_prepared_attempt_is_superseded_without_double_reservation(scenario) -> None:
    first = scenario.crash_before_boundary(key=None)
    retry = scenario.accounting.prepare(
        context("tenant-a"),
        decision(),
        input_tokens=PROMPT_TOKENS,
        output_tokens=4096,  # a larger estimate than the earlier reservation covers
        correlation_id=first.ticket.correlation_id,
    )
    assert retry.attempt == 2 and retry.reservation_id != first.ticket.reservation_id
    assert scenario.count(BudgetReservation, status="reserved") == 1
    assert scenario.reservation(first).status == "released"
    assert [row.state for row in ledger(scenario)] == ["released", "prepared"]
    with pytest.raises(AccountingError):  # the superseded attempt can no longer cross
        scenario.accounting.begin_provider(first.ticket)
    scenario.accounting.begin_provider(retry)


def test_prepare_refuses_a_correlation_whose_provider_may_have_run(scenario) -> None:
    first = scenario.crash_after_boundary(key=None)
    with pytest.raises(AccountingError):
        scenario.accounting.prepare(
            context("tenant-a"),
            decision(),
            input_tokens=PROMPT_TOKENS,
            output_tokens=256,
            correlation_id=first.ticket.correlation_id,
        )
    assert scenario.count(BudgetReservation) == 1


def test_crash_after_the_boundary_stays_held(scenario) -> None:
    started = scenario.crash_after_boundary()
    before = scenario.snapshot()
    outcome = scenario.service().reconcile(started.ticket.reservation_id)
    assert outcome.classification is Classification.UNKNOWN_BILLING
    assert outcome.action == "none" and not outcome.changed and outcome.review_required
    assert scenario.snapshot() == before
    assert before["reservations"] == [("reserved", "None")]


def test_a_provider_timeout_stays_held_however_old(scenario) -> None:
    started = scenario.provider_timeout()
    service = scenario.service(clock=lambda: started_time_far_future())
    outcome = service.reconcile(started.ticket.reservation_id)
    assert outcome.classification is Classification.UNKNOWN_BILLING and not outcome.changed
    case = service.inspect(started.ticket.reservation_id)
    assert case.ambiguity_reason == "provider_timeout" and case.review_required
    snap = scenario.snapshot()
    assert snap["reservations"] == [("reserved", "None")] and snap["usage"] == []
    assert snap["idempotency"] == ["ambiguous_held"]


def started_time_far_future():
    from datetime import UTC, datetime

    return datetime(2099, 1, 1, tzinfo=UTC)


def test_a_receipt_is_recovered_once_without_the_provider(scenario) -> None:
    started = scenario.receipt_then_crash()
    before = scenario.snapshot()
    assert before["usage"] == [] and before["reservations"] == [("reserved", "None")]
    service = scenario.service()
    assert service.inspect(started.ticket.reservation_id).classification is (
        Classification.RECEIPT_AVAILABLE
    )
    outcome = service.reconcile(started.ticket.reservation_id)
    assert outcome.action == "committed" and outcome.changed
    snap = scenario.snapshot()
    correlation = str(started.ticket.correlation_id)
    assert snap["usage"] == [(correlation, PROMPT_TOKENS, COMPLETION_TOKENS, str(EXPECTED_COST))]
    assert snap["reservations"] == [("committed", str(EXPECTED_COST))]
    assert snap["ledger"] == [(1, "accounted", "receipt_available")]
    assert snap["idempotency"] == ["reconciled"]
    with Session(scenario.engine) as session:
        (record,) = session.scalars(select(UsageRecord))
        assert record.provider_id == "deterministic" and record.routing_class == "SIMPLE"
        assert record.routing_reason == "short_question,few_sources"
        assert record.latency_ms == 9.5 and record.fallback_count == 0


def test_the_recovered_key_never_executes_again(scenario) -> None:
    started = scenario.receipt_then_crash()
    scenario.service().reconcile(started.ticket.reservation_id)
    claim = scenario.store.claim(started.scope, request_fingerprint(QUESTION, 3))
    assert claim.decision is Decision.REPLAY
    assert claim.status_code == 409 and claim.body == {"detail": RECONCILED_DETAIL}


def test_existing_usage_is_authoritative(scenario) -> None:
    started = scenario.receipt_then_crash()
    with transaction(scenario.engine) as session:  # another path booked the usage already
        execution = session.scalar(select(QueryExecution))
        session.add(
            UsageRecord(
                tenant_id="tenant-a",
                user_id=execution.user_id,
                model_id=execution.model_configuration_id,
                correlation_id=execution.correlation_id,
                input_tokens=PROMPT_TOKENS,
                output_tokens=COMPLETION_TOKENS,
                cost=EXPECTED_COST,
                latency_ms=9.5,
                endpoint="rag",
                workflow="query",
            )
        )
    outcome = scenario.service().reconcile(started.ticket.reservation_id)
    assert outcome.classification is Classification.ALREADY_ACCOUNTED
    assert outcome.action == "committed"
    assert scenario.count(UsageRecord) == 1
    assert scenario.snapshot()["reservations"] == [("committed", str(EXPECTED_COST))]


def test_reconciling_twice_has_no_further_side_effects(scenario) -> None:
    started = scenario.receipt_then_crash()
    service = scenario.service()
    first = service.reconcile(started.ticket.reservation_id)
    after_first = scenario.snapshot()
    second = service.reconcile(started.ticket.reservation_id)
    assert first.changed and not second.changed
    assert second.classification is Classification.RESOLVED and second.action == "none"
    assert scenario.snapshot() == after_first


def test_a_datastore_failure_leaves_the_financial_state_unchanged(scenario, monkeypatch) -> None:
    started = scenario.receipt_then_crash()
    before = scenario.snapshot()

    def failing(*args, **kwargs):
        raise reconciliation_module.SQLAlchemyError("injected outage")

    monkeypatch.setattr(reconciliation_module.UsageService, "record", failing)
    with pytest.raises(ReconciliationUnavailable):
        scenario.service().reconcile(started.ticket.reservation_id)
    assert scenario.snapshot() == before


def test_an_unreachable_datastore_fails_closed(scenario) -> None:
    def broken():
        raise reconciliation_module.SQLAlchemyError("no database")

    service = ReconciliationService(broken, "tenant-a", grace_seconds=0)
    with pytest.raises(ReconciliationUnavailable):
        service.cases()
    with pytest.raises(ReconciliationUnavailable):
        service.reconcile(uuid4())


# --------------------------------------------------------------------------- tenant authority


def test_another_tenant_cannot_see_or_reconcile_a_case(scenario) -> None:
    started = scenario.receipt_then_crash("tenant-a")
    scenario.tenant("tenant-b")
    before = scenario.snapshot("tenant-a")
    other = scenario.service("tenant-b")
    assert other.cases() == []
    for probe in (started.ticket.reservation_id, started.ticket.correlation_id):
        with pytest.raises(CaseNotFound):
            other.inspect(probe)
        with pytest.raises(CaseNotFound):
            other.reconcile(probe)
    assert other.reconcile_auto() == []
    assert scenario.snapshot("tenant-a") == before


def test_a_correlation_id_resolves_to_its_own_tenant_case_only(scenario) -> None:
    started = scenario.receipt_then_crash("tenant-a")
    case = scenario.service("tenant-a").inspect(started.ticket.correlation_id)
    assert case.reservation_id == started.ticket.reservation_id


def test_the_service_requires_a_tenant() -> None:
    with pytest.raises(ValueError):
        ReconciliationService(lambda: None, "", grace_seconds=0)


# --------------------------------------------------------------------------- tampering


def tamper(scenario: Scenario, **values) -> None:
    with transaction(scenario.engine) as session:
        session.execute(update(QueryExecution).values(**values))


@pytest.mark.parametrize(
    "values",
    [
        {"cost": EXPECTED_COST / 2},  # cost does not follow from tokens and prices
        {"input_price": Decimal("0")},  # price snapshot changed after the fact
        {"output_tokens": 0},  # usage rewritten to look free
        {"correlation_id": uuid4()},  # receipt detached from its reservation
    ],
    ids=["cost", "price", "tokens", "correlation"],
)
def test_a_forged_receipt_is_invalid_and_changes_nothing(scenario, values) -> None:
    started = scenario.receipt_then_crash()
    tamper(scenario, **values)
    before = scenario.snapshot()
    outcome = scenario.service().reconcile(started.ticket.reservation_id)
    assert outcome.classification is Classification.INVALID and not outcome.changed
    assert scenario.snapshot() == before
    assert scenario.count(UsageRecord) == 0


def test_mismatched_existing_usage_is_invalid(scenario) -> None:
    started = scenario.receipt_then_crash()
    with transaction(scenario.engine) as session:
        execution = session.scalar(select(QueryExecution))
        session.add(
            UsageRecord(
                tenant_id="tenant-a",
                user_id=execution.user_id,
                model_id=execution.model_configuration_id,
                correlation_id=execution.correlation_id,
                input_tokens=1,
                output_tokens=1,
                cost=Decimal("0.00000003"),
                latency_ms=1,
                endpoint="rag",
                workflow="query",
            )
        )
    before = scenario.snapshot()
    outcome = scenario.service().reconcile(started.ticket.reservation_id)
    assert outcome.classification is Classification.INVALID and not outcome.changed
    assert scenario.snapshot() == before


def test_contradicting_idempotency_state_is_invalid(scenario) -> None:
    started = scenario.crash_before_boundary()
    with transaction(scenario.engine) as session:
        session.execute(update(QueryIdempotencyRecord).values(state="succeeded"))
    outcome = scenario.service().reconcile(started.ticket.reservation_id)
    assert outcome.classification is Classification.INVALID and not outcome.changed
    assert scenario.reservation(started).status == "reserved"


def test_a_committed_reservation_without_usage_is_invalid(scenario) -> None:
    started = scenario.crash_before_boundary(key=None)
    with transaction(scenario.engine) as session:
        session.execute(update(BudgetReservation).values(status="committed"))
    case = scenario.service().inspect(started.ticket.reservation_id)
    assert case.classification is Classification.INVALID


def test_receipt_columns_are_complete_or_rejected_by_the_database(scenario) -> None:
    scenario.receipt_then_crash()
    with pytest.raises(IntegrityError), transaction(scenario.engine) as session:
        session.execute(update(QueryExecution).values(cost=None))


def test_ledger_states_are_constrained_by_the_database(scenario) -> None:
    scenario.start()
    with pytest.raises(IntegrityError), transaction(scenario.engine) as session:
        session.execute(update(QueryExecution).values(state="assumed_not_billed"))


# --------------------------------------------------------------------------- legacy ENT-11.5


def legacy_case(scenario: Scenario, *, with_usage: bool = False) -> BudgetReservation:
    """A reservation held by ENT-11.5 code: no ledger row exists for it."""
    scenario.tenant("tenant-a")
    correlation = uuid4()
    with transaction(scenario.engine) as session:
        budget = session.scalar(select(TenantBudget).where(TenantBudget.tenant_id == "tenant-a"))
        reservation = BudgetReservationService(session, "tenant-a").reserve(
            budget.id, Decimal("0.01"), f"query:{correlation}"
        )
        if with_usage:
            from ragops.persistence.models import ModelConfiguration, User

            session.add(
                UsageRecord(
                    tenant_id="tenant-a",
                    user_id=session.scalar(select(User.id)),
                    model_id=session.scalar(select(ModelConfiguration.id)),
                    correlation_id=correlation,
                    input_tokens=PROMPT_TOKENS,
                    output_tokens=COMPLETION_TOKENS,
                    cost=EXPECTED_COST,
                    latency_ms=3,
                    endpoint="rag",
                    workflow="query",
                )
            )
        session.flush()
        session.expunge(reservation)
        return reservation


def test_a_legacy_hold_without_ledger_evidence_is_never_released(scenario) -> None:
    reservation = legacy_case(scenario)
    service = scenario.service(clock=started_time_far_future)
    (case,) = service.cases()
    assert case.classification is Classification.UNKNOWN_BILLING and case.legacy
    assert service.reconcile(reservation.id).changed is False
    assert service.reconcile_auto() == []
    assert scenario.snapshot()["reservations"] == [("reserved", "None")]


def test_a_legacy_hold_with_booked_usage_is_committed_not_released(scenario) -> None:
    reservation = legacy_case(scenario, with_usage=True)
    outcome = scenario.service().reconcile(reservation.id)
    assert outcome.classification is Classification.ALREADY_ACCOUNTED
    assert scenario.snapshot()["reservations"] == [("committed", str(EXPECTED_COST))]
    assert scenario.count(UsageRecord) == 1


def test_non_query_reservations_are_not_reconciliation_cases(scenario) -> None:
    scenario.tenant("tenant-a")
    with transaction(scenario.engine) as session:
        budget = session.scalar(select(TenantBudget))
        reservation = BudgetReservationService(session, "tenant-a").reserve(
            budget.id, Decimal("1"), "batch-export-7"
        )
        session.flush()
        key = reservation.id
    service = scenario.service()
    assert service.cases() == []
    with pytest.raises(CaseNotFound):
        service.reconcile(key)


# --------------------------------------------------------------------------- batch + liveness


def test_auto_reconciliation_acts_only_on_safe_cases_and_is_bounded(scenario) -> None:
    held = scenario.crash_after_boundary("tenant-a", key="k-held")
    pre = scenario.crash_before_boundary("tenant-a", key="k-pre")
    receipt = scenario.receipt_then_crash("tenant-a", key="k-receipt")
    service = scenario.service()
    assert len(service.cases()) == 3
    outcomes = service.reconcile_auto(limit=1)
    assert len(outcomes) == 1
    outcomes += service.reconcile_auto(limit=10)
    acted = {outcome.reservation_id: outcome.action for outcome in outcomes}
    assert acted == {
        pre.ticket.reservation_id: "released",
        receipt.ticket.reservation_id: "committed",
    }
    (remaining,) = service.cases()
    assert remaining.reservation_id == held.ticket.reservation_id
    assert remaining.classification is Classification.UNKNOWN_BILLING


def test_a_fresh_case_is_in_flight_and_left_alone(scenario) -> None:
    started = scenario.crash_before_boundary()
    service = ReconciliationService(lambda: scenario.engine, "tenant-a", grace_seconds=3600)
    outcome = service.reconcile(started.ticket.reservation_id)
    assert outcome.classification is Classification.IN_FLIGHT and not outcome.changed
    assert scenario.reservation(started).status == "reserved"


@pytest.mark.parametrize("value", [-1, 86_401])
def test_the_grace_period_is_bounded(value) -> None:
    with pytest.raises(ValueError):
        ReconciliationService(lambda: None, "tenant-a", grace_seconds=value)


def test_case_views_carry_no_content_or_secrets(scenario) -> None:
    started = scenario.receipt_then_crash()
    case = scenario.service().inspect(started.ticket.reservation_id)
    rendered = str(case.as_dict())
    assert QUESTION not in rendered and "retry-key-1" not in rendered
    assert set(case.as_dict()) >= {
        "reservation_id",
        "correlation_id",
        "classification",
        "reason",
        "review_required",
        "reservation_status",
        "ledger_state",
        "usage_recorded",
    }


# --------------------------------------------------------------------------- idempotency


def test_reconciled_is_a_terminal_non_executable_state() -> None:
    state = IdempotencyState.RECONCILED
    assert state.value == "reconciled"
    assert IdempotencyState.AMBIGUOUS_HELD in {
        source for source, targets in TRANSITIONS.items() if state in targets
    }
    assert TRANSITIONS[state] == frozenset({IdempotencyState.CLAIMED})  # only after expiry
    assert IdempotencyState.FAILED not in TRANSITIONS[IdempotencyState.AMBIGUOUS_HELD]
    assert IdempotencyState.RETRYABLE not in TRANSITIONS[IdempotencyState.AMBIGUOUS_HELD]


# --------------------------------------------------------------------------- migration


def test_migration_0008_upgrades_and_downgrades_without_releasing_anything(tmp_path) -> None:
    from alembic import command
    from alembic.config import Config
    from sqlalchemy import create_engine

    engine = create_engine(f"sqlite:///{tmp_path / 'migrate.db'}")

    def run(step, revision):
        with engine.begin() as connection:
            cfg = Config("alembic.ini")
            cfg.attributes["connection"] = connection
            step(cfg, revision)

    run(command.upgrade, "0007_query_idempotency")
    held, settled = uuid4(), uuid4()
    with engine.begin() as connection:
        connection.execute(text("INSERT INTO tenants (id, name, active) VALUES ('t', 't', 1)"))
        for record in (held, settled):
            connection.execute(
                text(
                    "INSERT INTO query_idempotency_records (tenant_id, id, created_at, principal, "
                    "endpoint, key_hash, request_fingerprint, correlation_id, state, attempt, "
                    "updated_at, lease_expires_at, expires_at) VALUES ('t', :i, '2026-10-01', "
                    ":p, 'POST /v1/query', 'h', 'f', :c, 'ambiguous_held', 1, '2026-10-01', "
                    "'2026-10-01', '2026-10-01')"
                ),
                {"i": record.hex, "p": record.hex[:16], "c": uuid4().hex},
            )
    run(command.upgrade, "head")
    with engine.begin() as connection:
        states = connection.execute(text("SELECT state FROM query_idempotency_records")).scalars()
        assert sorted(states) == ["ambiguous_held", "ambiguous_held"]  # never assumed safe
        assert connection.execute(text("SELECT count(*) FROM query_executions")).scalar() == 0
        connection.execute(
            text("UPDATE query_idempotency_records SET state = 'reconciled' WHERE id = :i"),
            {"i": settled.hex},
        )
    run(command.downgrade, "0007_query_idempotency")
    with engine.begin() as connection:
        # A settled record never becomes executable or "not billed" on the older schema.
        states = connection.execute(text("SELECT state FROM query_idempotency_records")).scalars()
        assert sorted(states) == ["ambiguous_held", "ambiguous_held"]
        assert "query_executions" not in sa_inspect(connection).get_table_names()
    with pytest.raises(IntegrityError), engine.begin() as connection:
        connection.execute(text("UPDATE query_idempotency_records SET state = 'reconciled'"))
    run(command.upgrade, "head")
