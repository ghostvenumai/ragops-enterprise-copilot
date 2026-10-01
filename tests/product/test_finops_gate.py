"""FinOps gate contracts on a migrated SQLite schema; injected money defects must fail."""

from __future__ import annotations

import json
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest

pytest.importorskip("alembic", reason="Install declared persistence extra; product gate blocks")

from alembic import command
from alembic.config import Config
from scripts import finops_gate as gate
from scripts import rc_live_gate
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.engine import Engine

from ragops.finops import reservation as reservation_module
from ragops.finops.reservation import BudgetReservationService
from ragops.finops.service import FinOpsService
from ragops.modeling.usage import UsageService
from ragops.persistence.models import UsageRecord


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    database = create_engine(f"sqlite:///{tmp_path / 'finops-gate.db'}")

    @event.listens_for(database, "connect")
    def enable_foreign_keys(connection: object, record: object) -> None:
        connection.execute("PRAGMA foreign_keys=ON")  # type: ignore[attr-defined]

    with database.begin() as connection:
        cfg = Config("alembic.ini")
        cfg.attributes["connection"] = connection
        command.upgrade(cfg, "head")
    yield database
    database.dispose()


def run(engine: Engine) -> gate.FinOpsResult:
    return gate.run_scenarios(engine, gate.rc_tenants(uuid4().hex[:8]), concurrency=False)


def test_real_services_pass_every_required_invariant(engine) -> None:
    result = run(engine)
    assert result.errors == []
    assert set(gate.REQUIRED) <= set(result.checks)
    assert all(result.checks.values()), result.checks
    assert gate.classify(result, 0, require_concurrency=False)[0] == "PASS"
    fields = gate.result_fields(result)
    assert fields["finops_tenant_leakage"] == 0
    assert fields["committed_amount"] == "45.31600000"
    assert fields["active_reserved_amount"] == "0"
    assert fields["route_cheaper_enforced_in_query_path"] is False
    assert fields["finops_enforced_in_query_path"] == "vector_mode_only"
    assert fields["usage_recorded_by_query_path"] == "vector_mode_only"


def test_exact_boundary_regression_is_reported(engine, monkeypatch) -> None:
    monkeypatch.setattr(
        reservation_module, "hard_limit_ratio", lambda budget: budget.hard_limit_threshold + 1
    )
    result = run(engine)
    assert result.checks["exact_limit_verified"] is False
    assert gate.classify(result, 0, require_concurrency=False)[0] == "FAIL"


def test_double_billing_is_reported(engine, monkeypatch) -> None:
    original = UsageService.record

    def fresh_correlation(self, decision, **kwargs):
        return original(self, decision, **{**kwargs, "correlation_id": uuid4()})

    monkeypatch.setattr(UsageService, "record", fresh_correlation)
    result = run(engine)
    assert result.checks["duplicate_billing_prevented"] is False
    assert gate.classify(result, 0, require_concurrency=False)[0] == "FAIL"


def test_cross_tenant_spend_is_reported_as_leakage(engine, monkeypatch) -> None:
    def all_tenants_spend(self, start, end):
        value = self.session.scalar(select(func.coalesce(func.sum(UsageRecord.cost), 0)))
        return Decimal(str(value or 0))

    monkeypatch.setattr(FinOpsService, "spend", all_tenants_spend)
    result = run(engine)
    assert result.finops_tenant_leakage > 0
    assert gate.classify(result, 0, require_concurrency=False) == (
        "FAIL",
        "tenant accounting crossed tenant boundaries",
    )


def test_silent_invalid_transition_is_reported(engine, monkeypatch) -> None:
    def permissive_commit(self, reservation, actual_amount=None, *, now=None):
        reservation.status = "committed"
        return reservation

    monkeypatch.setattr(BudgetReservationService, "commit", permissive_commit)
    result = run(engine)
    assert result.checks["invalid_transition_rejected"] is False


def test_ignored_expiry_is_reported(engine, monkeypatch) -> None:
    monkeypatch.setattr(BudgetReservationService, "expire_stale", lambda self, now=None: 0)
    result = run(engine)
    assert result.checks["expiry_verified"] is False


def test_live_classification_requires_concurrency_and_zero_provider_calls(engine) -> None:
    result = run(engine)
    assert gate.classify(result, 0, require_concurrency=True)[0] == "FAIL"
    result.checks["concurrency_invariant"] = True
    assert gate.classify(result, 0, require_concurrency=True)[0] == "PASS"
    assert gate.classify(result, 1, require_concurrency=True) == (
        "FAIL",
        "outbound provider traffic was attempted",
    )


def test_evidence_has_money_as_strings_and_no_secrets(engine) -> None:
    serialized = json.dumps(gate.result_fields(run(engine)))
    for forbidden in ("Bearer", "eyJ", "BEGIN", "postgresql", "sqlite:///", "sk-"):
        assert forbidden not in serialized
    assert '"hard_limit": "120"' in serialized


@pytest.mark.parametrize(
    "url", [None, "postgresql+psycopg://rc:pw@127.0.0.1/ragops", "sqlite:///ragops_test_x.db"]
)
def test_missing_or_shared_database_is_blocked(monkeypatch, tmp_path, url) -> None:
    monkeypatch.setattr(gate, "EVIDENCE", tmp_path / "finops.json")
    if url is None:
        monkeypatch.delenv("RAGOPS_TEST_DATABASE_URL", raising=False)
    else:
        monkeypatch.setenv("RAGOPS_TEST_DATABASE_URL", url)
    assert gate.main() == 2
    evidence = json.loads(gate.EVIDENCE.read_text())
    assert (evidence["status"], evidence["paid_provider_calls"]) == ("BLOCKED", 0)


def test_unreachable_database_is_blocked_without_leaking_url(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(gate, "EVIDENCE", tmp_path / "finops.json")
    monkeypatch.setenv(
        "RAGOPS_TEST_DATABASE_URL",
        "postgresql+psycopg://rcuser:TopSecretPw1@127.0.0.1:9/ragops_test_gate",
    )
    assert gate.main() == 2
    raw = gate.EVIDENCE.read_text()
    assert json.loads(raw)["status"] == "BLOCKED"
    assert "TopSecretPw1" not in raw and "postgresql+psycopg://" not in raw


def test_rc_runner_merges_finops_evidence_and_keeps_prior_pass() -> None:
    assert "finops" in rc_live_gate.DETAILED_EVIDENCE_GATES
    prior = {name: "PASS" for name in rc_live_gate.GATES[:9]}
    merged = rc_live_gate.merge_gate_statuses(prior, {"finops"}, {"finops": {"status": "PASS"}})
    assert all(merged[name] == "PASS" for name in prior)
    assert (merged["finops"], merged["rate_limiting"]) == ("PASS", "BLOCKED")
