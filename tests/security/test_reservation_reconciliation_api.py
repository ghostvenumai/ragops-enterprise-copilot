"""Reconciliation through the real /v1/query path: boundary, receipt, recovery (ENT-11.6).

The API runs unchanged; failures are injected at the accounting and provider boundaries. The
reconciliation service is the operator path; it must never reach the provider.
"""

from __future__ import annotations

import json
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session
from tests.security.finops_helpers import reservations, usage_rows
from tests.security.test_query_finops import QUESTION, TEXT
from tests.security.test_query_idempotency_api import KEY, REPLAYED, IdempotentHarness

from ragops.finops import query_accounting as accounting_module
from ragops.finops.reconciliation import Classification, ReconciliationService
from ragops.modeling import usage as usage_module
from ragops.modeling.router import ProviderErrorCategory
from ragops.persistence.models import QueryExecution
from ragops.workflows.query_idempotency import RECONCILED_DETAIL

HMAC_KEY = "api-test-hmac-secret-0123456789-abcdefghijkl"
RECONCILIATION = {"detail": "idempotent request requires reconciliation"}


@pytest.fixture
def harness(keys, tmp_path, monkeypatch) -> IdempotentHarness:
    return IdempotentHarness(keys, tmp_path, monkeypatch, hmac_key=HMAC_KEY)


def executions(harness: IdempotentHarness, tenant: str = "tenant-a") -> list[QueryExecution]:
    with Session(harness.engine) as session:
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


def service(harness: IdempotentHarness, tenant: str = "tenant-a") -> ReconciliationService:
    return ReconciliationService(lambda: harness.engine, tenant, grace_seconds=0)


def guard_provider(harness: IdempotentHarness, monkeypatch) -> None:
    def forbidden(*args, **kwargs):
        raise AssertionError("reconciliation must never call a provider")

    monkeypatch.setattr(harness.provider, "generate", forbidden)


def test_a_successful_query_is_accounted_in_the_ledger(harness) -> None:
    harness.provision("tenant-a")
    response = harness.send(key=None)
    assert response.status_code == 200
    (row,) = executions(harness)
    assert row.state == "accounted" and str(row.correlation_id) == response.json()["correlation_id"]
    assert harness.events == ["reserve", "provider", "usage", "commit"]
    assert service(harness).cases() == []


def test_an_accounting_failure_is_recovered_from_the_receipt(harness, monkeypatch) -> None:
    harness.provision("tenant-a")

    def broken(*args, **kwargs):
        raise RuntimeError("database write failed")

    monkeypatch.setattr(usage_module.UsageService, "record", broken)
    first = harness.send()
    assert first.status_code == 503 and first.json() == {"detail": "accounting unavailable"}
    monkeypatch.undo()
    assert harness.send().json() == RECONCILIATION
    (row,) = executions(harness)
    assert row.state == "receipt_recorded" and harness.provider.calls == 1
    assert usage_rows(harness.engine) == []
    guard_provider(harness, monkeypatch)
    outcome = service(harness).reconcile(row.reservation_id)
    assert outcome.classification is Classification.RECEIPT_AVAILABLE
    (booked,) = usage_rows(harness.engine)
    (committed,) = reservations(harness.engine)
    assert committed.status == "committed" and committed.final_amount == booked.cost
    assert booked.cost == row.cost and booked.input_tokens == row.input_tokens
    # The client learns that the query is settled; nothing is executed or billed again.
    after = harness.send()
    assert after.status_code == 409 and after.json() == {"detail": RECONCILED_DETAIL}
    assert after.headers[REPLAYED] == "true"
    assert len(usage_rows(harness.engine)) == 1
    assert "Orbit" not in after.text and TEXT["tenant-a"] not in after.text


def test_a_lost_boundary_marker_never_calls_the_provider(harness, monkeypatch) -> None:
    harness.provision("tenant-a")

    def lost(self, ticket):
        raise accounting_module.AccountingError("provider boundary could not be recorded")

    monkeypatch.setattr(accounting_module.QueryAccounting, "begin_provider", lost)
    first = harness.send()
    assert first.status_code == 503 and first.json() == {"detail": "accounting unavailable"}
    assert harness.provider.calls == 0 and usage_rows(harness.engine) == []
    (released,) = reservations(harness.engine)
    assert released.status == "released"
    (record,) = harness.records()
    assert record.state == "retryable"
    monkeypatch.undo()
    retry = harness.send()
    assert retry.status_code == 200 and retry.json()["correlation_id"] == str(record.correlation_id)
    assert harness.provider.calls == 1 and len(usage_rows(harness.engine)) == 1
    assert [row.state for row in executions(harness)] == ["released", "accounted"]


@pytest.mark.parametrize(
    ("category", "reason"),
    [
        (ProviderErrorCategory.TIMEOUT, "provider_timeout"),
        (ProviderErrorCategory.PROVIDER_ERROR, "provider_error"),
    ],
)
def test_an_ambiguous_provider_failure_stays_held_for_review(
    harness, monkeypatch, category, reason
) -> None:
    harness.provision("tenant-a")
    harness.fault(monkeypatch, category)
    assert harness.send().status_code == 503
    (row,) = executions(harness)
    assert row.state == "provider_may_run" and row.ambiguity_reason == reason
    guard_provider(harness, monkeypatch)
    outcome = service(harness).reconcile(row.reservation_id)
    assert outcome.classification is Classification.UNKNOWN_BILLING
    assert not outcome.changed and outcome.review_required
    assert service(harness).reconcile_auto() == []
    (held,) = reservations(harness.engine)
    assert held.status == "reserved" and usage_rows(harness.engine) == []
    assert harness.send().json() == RECONCILIATION


def test_unreported_usage_is_held_and_never_booked_as_zero(keys, tmp_path, monkeypatch) -> None:
    harness = IdempotentHarness(keys, tmp_path, monkeypatch, hmac_key=HMAC_KEY, usage=False)
    harness.provision("tenant-a")
    assert harness.send().json() == {"detail": "accounting unavailable"}
    (row,) = executions(harness)
    assert row.state == "provider_may_run" and row.ambiguity_reason == "usage_not_reported"
    assert row.input_tokens is None and row.cost is None
    outcome = service(harness).reconcile(row.reservation_id)
    assert outcome.classification is Classification.UNKNOWN_BILLING and not outcome.changed
    assert usage_rows(harness.engine) == []
    assert reservations(harness.engine)[0].status == "reserved"


def test_a_provider_rejection_releases_reservation_and_ledger_together(
    harness, monkeypatch
) -> None:
    harness.provision("tenant-a")
    harness.fault(monkeypatch, ProviderErrorCategory.RATE_LIMIT)
    assert harness.send().status_code == 503
    (row,) = executions(harness)
    assert row.state == "released" and row.resolution == "provider_rejected"
    assert reservations(harness.engine)[0].status == "released"
    assert service(harness).cases() == []


def test_the_ledger_and_audit_hold_no_content_or_keys(harness, monkeypatch) -> None:
    harness.provision("tenant-a")

    def broken(*args, **kwargs):
        raise RuntimeError("database write failed")

    monkeypatch.setattr(usage_module.UsageService, "record", broken)
    harness.send()
    monkeypatch.undo()
    service(harness).reconcile(executions(harness)[0].reservation_id)
    with harness.engine.connect() as connection:
        stored = json.dumps(
            [
                dict(row)
                for row in connection.exec_driver_sql("SELECT * FROM query_executions").mappings()
            ],
            default=str,
        )
    case = service(harness).inspect(executions(harness)[0].reservation_id)
    rendered = stored + json.dumps(case.as_dict(), default=str)
    rendered += (harness.evidence / "audit-events.jsonl").read_text()
    for value in (KEY, QUESTION, TEXT["tenant-a"], HMAC_KEY, "Bearer"):
        assert value not in rendered


def test_another_tenant_cannot_reconcile_through_any_identifier(harness, monkeypatch) -> None:
    harness.provision("tenant-a")
    harness.provision("tenant-b")
    harness.fault(monkeypatch, ProviderErrorCategory.TIMEOUT)
    harness.send("tenant-a")
    (row,) = executions(harness)
    other = service(harness, "tenant-b")
    from ragops.finops.reconciliation import CaseNotFound

    for probe in (row.reservation_id, row.correlation_id, row.id):
        with pytest.raises(CaseNotFound):
            other.reconcile(probe)
    assert other.cases() == [] and reservations(harness.engine)[0].status == "reserved"
    assert reservations(harness.engine)[0].estimated_amount > Decimal("0")
