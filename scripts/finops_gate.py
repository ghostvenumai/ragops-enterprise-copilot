"""FinOps RC gate on the real budget, reservation, usage, forecast and alert services.

Runs against the isolated ragops_test_* PostgreSQL database (Alembic head) with unique
synthetic tenants and only the production services; direct SQL is limited to
independent read-back in fresh sessions. Usage is booked through UsageService with a
decision from the real router, so no provider is called; outbound HTTP is counted and
blocked. The gate records that FinOps is not yet enforced in the /v1/query path.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from functools import partial
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sqlalchemy import create_engine, func, select, text  # noqa: E402
from sqlalchemy.engine import Engine  # noqa: E402
from sqlalchemy.exc import IntegrityError  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from ragops.finops.reservation import BudgetReservationService, ReservationError  # noqa: E402
from ragops.finops.service import FinOpsService  # noqa: E402
from ragops.modeling.router import ComplexitySignals, LLMModelRouter, ModelSpec  # noqa: E402
from ragops.modeling.usage import UsageService  # noqa: E402
from ragops.persistence.database import transaction  # noqa: E402
from ragops.persistence.models import (  # noqa: E402
    BudgetAlert,
    BudgetReservation,
    ModelConfiguration,
    ProviderConfiguration,
    Tenant,
    TenantBudget,
    UsageRecord,
    User,
)

EVIDENCE = ROOT / "evidence" / "product-v1" / "rc-live" / "finops.json"
RC_TENANT_PREFIX = "rc-fo"
D = Decimal
HARD_LIMIT = D("120")  # budget 100 EUR x hard_limit_threshold 1.2


@dataclass
class Tenancy:
    tenant_id: str
    budget_id: UUID
    user_id: UUID
    model_id: UUID


@dataclass
class FinOpsResult:
    checks: dict[str, bool] = field(default_factory=dict)
    finops_tenant_leakage: int = 0
    reservation_count: int = 0
    committed_amount: Decimal = D("0")
    active_reserved_amount: Decimal = D("0")
    errors: list[str] = field(default_factory=list)

    def check(self, name: str, ok: bool) -> None:
        self.checks[name] = self.checks.get(name, True) and ok

    def rejects(
        self, name: str, action: Callable[[], object], *expected: type[BaseException]
    ) -> None:
        try:
            action()
        except expected:
            self.check(name, True)
            return
        self.check(name, False)

    def leak(self, ok: bool) -> None:
        self.check("tenant_isolation", ok)
        if not ok:
            self.finops_tenant_leakage += 1


def rc_tenants(run_id: str) -> tuple[str, str, str]:
    prefix = f"{RC_TENANT_PREFIX}-{run_id}"
    return f"{prefix}-a", f"{prefix}-b", f"{prefix}-c"


def period(today: date) -> tuple[date, date]:
    start = today.replace(day=1)
    return start, (start + timedelta(days=32)).replace(day=1)


def seed(engine: Engine, tenants: tuple[str, str, str], today: date) -> dict[str, Tenancy]:
    start, end = period(today)
    modes = {
        tenants[0]: ("hard_limit", D("100"), D("1.2")),
        tenants[1]: ("optimize", D("100"), D("1.2")),
        tenants[2]: ("hard_limit", D("10"), D("1")),
    }
    seeded: dict[str, Tenancy] = {}
    with transaction(engine) as session:
        session.add_all(Tenant(id=tenant, name=f"RC FinOps {tenant}") for tenant in tenants)
        session.flush()
        for tenant, (mode, amount, hard) in modes.items():
            user = User(
                tenant_id=tenant, issuer="https://rc-finops.invalid/", subject=f"rc-{tenant}"
            )
            provider = ProviderConfiguration(
                tenant_id=tenant, name="rc-provider", kind="deterministic"
            )
            session.add_all([user, provider])
            session.flush()
            model = ModelConfiguration(
                tenant_id=tenant,
                provider_id=provider.id,
                name="rc-model",
                input_price=D("0.00001"),
                output_price=D("0.00002"),
            )
            budget = TenantBudget(
                tenant_id=tenant,
                period_start=start,
                period_end=end,
                budget_amount=amount,
                hard_limit_threshold=hard,
                enforcement_mode=mode,
            )
            session.add_all([model, budget])
            session.flush()
            seeded[tenant] = Tenancy(tenant, budget.id, user.id, model.id)
    return seeded


def _spend(engine: Engine, tenant: str, today: date) -> Decimal:
    with Session(engine) as session:
        return FinOpsService(session, tenant).spend(*period(today))


def _active_reserved(engine: Engine, tenant: str, budget_id: UUID, now: datetime) -> Decimal:
    """Independent read-back of reserved capacity that has not expired."""
    with Session(engine) as session:
        rows = session.scalars(
            select(BudgetReservation).where(
                BudgetReservation.tenant_id == tenant,
                BudgetReservation.budget_id == budget_id,
                BudgetReservation.status == "reserved",
            )
        ).all()
    return sum(
        (
            row.estimated_amount
            for row in rows
            if row.expires_at is None or _aware(row.expires_at) > now
        ),
        D("0"),
    )


def _load(session: Session, tenant: str, reservation_id: UUID) -> BudgetReservation:
    reservation = session.get(BudgetReservation, (tenant, reservation_id))
    if reservation is None:
        raise LookupError("reservation missing")
    return reservation


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _invariant(
    engine: Engine, a: Tenancy, today: date, now: datetime, result: FinOpsResult
) -> None:
    committed, reserved = (
        _spend(engine, a.tenant_id, today),
        _active_reserved(engine, a.tenant_id, a.budget_id, now),
    )
    result.check(
        "invariant_within_hard_limit",
        committed >= 0 and reserved >= 0 and committed + reserved <= HARD_LIMIT,
    )


def check_decisions(engine: Engine, a: Tenancy, b: Tenancy, result: FinOpsResult) -> None:
    matrix = (
        (D("79.99999999"), "ALLOW", "allow_verified"),
        (D("80"), "ALLOW_WITH_WARNING", "warning_verified"),
        (D("100"), "ROUTE_CHEAPER", "route_cheaper_verified"),
        (D("119.99999999"), "ROUTE_CHEAPER", "route_cheaper_verified"),
        (D("120"), "DENY_BUDGET_LIMIT", "exact_limit_verified"),
        (D("120.00000001"), "DENY_BUDGET_LIMIT", "deny_verified"),
    )
    with Session(engine) as session:
        budget_a = session.get(TenantBudget, (a.tenant_id, a.budget_id))
        budget_b = session.get(TenantBudget, (b.tenant_id, b.budget_id))
        assert budget_a is not None and budget_b is not None
        service = FinOpsService(session, a.tenant_id)
        for estimate, expected, name in matrix:
            result.check(name, service.preflight(budget_a, estimate).decision == expected)
        # Optimize mode routes cheaper but never denies, even far above the hard ratio.
        optimize = FinOpsService(session, b.tenant_id).preflight(budget_b, D("500"))
        result.check("route_cheaper_verified", optimize.decision == "ROUTE_CHEAPER")
        result.rejects(
            "invalid_amount_rejected", lambda: service.preflight(budget_a, D("-1")), ValueError
        )
        result.rejects(
            "invalid_amount_rejected", lambda: service.preflight(budget_a, D("NaN")), ValueError
        )


def check_reservations(
    engine: Engine, a: Tenancy, b: Tenancy, today: date, result: FinOpsResult
) -> None:
    now = datetime.now(UTC)
    with transaction(engine) as session:
        service = BudgetReservationService(session, a.tenant_id)
        first = service.reserve(a.budget_id, D("50"), "rc-shared-key")
        repeat = service.reserve(a.budget_id, D("50"), "rc-shared-key")
        other = BudgetReservationService(session, b.tenant_id).reserve(
            b.budget_id, D("50"), "rc-shared-key"
        )
        second = service.reserve(a.budget_id, D("69.99999999"), "rc-second")
        result.check("reservation_idempotency", repeat.id == first.id and other.id != first.id)
        # 50 + 69.99999999 + 0.00000001 reaches the hard limit exactly: denied, like preflight.
        result.rejects(
            "exact_limit_verified",
            lambda: service.reserve(a.budget_id, D("0.00000001"), "rc-exact"),
            ReservationError,
        )
        first_id, second_id = first.id, second.id
    with Session(engine) as session:
        keyed = session.scalar(
            select(func.count())
            .select_from(BudgetReservation)
            .where(
                BudgetReservation.tenant_id == a.tenant_id,
                BudgetReservation.idempotency_key == "rc-shared-key",
            )
        )
        persisted = _load(session, a.tenant_id, first_id)
        result.check(
            "reservation_persisted",
            keyed == 1
            and persisted is not None
            and persisted.status == "reserved"
            and persisted.estimated_amount == D("50"),
        )
    result.check(
        "active_reserved_before_commit",
        _active_reserved(engine, a.tenant_id, a.budget_id, now) == D("119.99999999"),
    )
    _invariant(engine, a, today, now, result)

    # Commit books nothing itself; usage records are the source of truth (ADR 0014).
    with transaction(engine) as session:
        service = BudgetReservationService(session, a.tenant_id)
        first = _load(session, a.tenant_id, first_id)
        service.commit(first, D("45"))
        _book(session, a, actual_cost=45.0)
        again = service.commit(first, D("99"))
        result.check(
            "commit_verified", again.status == "committed" and again.final_amount == D("45")
        )
    result.check("commit_verified", _spend(engine, a.tenant_id, today) == D("45"))
    result.check(
        "commit_verified",
        _active_reserved(engine, a.tenant_id, a.budget_id, now) == D("69.99999999"),
    )
    _invariant(engine, a, today, now, result)

    with transaction(engine) as session:
        service = BudgetReservationService(session, a.tenant_id)
        second = _load(session, a.tenant_id, second_id)
        first = _load(session, a.tenant_id, first_id)
        service.release(second)
        result.check("release_verified", service.release(second).status == "released")
        result.rejects(
            "invalid_transition_rejected", lambda: service.commit(second), ReservationError
        )
        result.rejects(
            "invalid_transition_rejected", lambda: service.release(first), ReservationError
        )
        foreign = BudgetReservationService(session, b.tenant_id)
        cross_tenant: list[Callable[[], object]] = [
            partial(foreign.commit, first),
            partial(foreign.release, first),
            partial(foreign.reserve, a.budget_id, D("1"), "rc-cross"),
        ]
        for action in cross_tenant:
            try:
                action()
                result.leak(False)
            except ReservationError:
                result.leak(True)
        # Released capacity returns: 45 committed + 74.99999999 stays below 120.
        returned = service.reserve(a.budget_id, D("74.99999999"), "rc-returned")
        service.release(returned)
        result.check("release_verified", returned.status == "released")
    result.check("release_verified", _active_reserved(engine, a.tenant_id, a.budget_id, now) == 0)
    _invariant(engine, a, today, now, result)

    expires = now + timedelta(hours=1)
    later = now + timedelta(hours=2)
    with transaction(engine) as session:
        service = BudgetReservationService(session, a.tenant_id)
        stale = service.reserve(a.budget_id, D("10"), "rc-expiring", expires_at=expires, now=now)
        # Before expiry it still consumes capacity: 45 + 10 + 65 reaches 120.
        result.rejects(
            "expiry_verified",
            lambda: service.reserve(a.budget_id, D("65"), "rc-blocked", now=now),
            ReservationError,
        )
        after = service.reserve(a.budget_id, D("65"), "rc-after-expiry", now=later)
        result.check("expiry_verified", after.status == "reserved")
        service.release(after)
        result.check(
            "expiry_verified", service.expire_stale(later) == 1 and service.expire_stale(later) == 0
        )
        result.rejects(
            "invalid_transition_rejected",
            lambda: service.commit(stale, now=later),
            ReservationError,
        )
        result.rejects(
            "invalid_transition_rejected", lambda: service.release(stale), ReservationError
        )
        stale_id = stale.id
    with Session(engine) as session:
        stored = session.get(BudgetReservation, (a.tenant_id, stale_id))
        result.check("expiry_verified", stored is not None and stored.status == "expired")

    with transaction(engine) as session:
        service = BudgetReservationService(session, a.tenant_id)
        for amount in (D("-0.00000001"), D("NaN"), D("Infinity")):
            result.rejects(
                "invalid_amount_rejected",
                partial(service.reserve, a.budget_id, amount, "rc-bad"),
                ReservationError,
            )
        result.rejects(
            "invalid_amount_rejected",
            lambda: service.reserve(uuid4(), D("1"), "rc-unknown"),
            ReservationError,
        )
        zero = service.reserve(a.budget_id, D("0"), "rc-zero")
        result.check(
            "zero_amount_deterministic",
            zero.status == "reserved" and service.commit(zero).final_amount == D("0"),
        )
        start, end = period(today)
        foreign_currency = TenantBudget(
            tenant_id=a.tenant_id,
            period_start=end,
            period_end=(end + timedelta(days=32)).replace(day=1),
            budget_amount=D("100"),
            currency="USD",
            enforcement_mode="hard_limit",
        )
        session.add(foreign_currency)
        session.flush()
        result.rejects(
            "invalid_amount_rejected",
            lambda: service.reserve(foreign_currency.id, D("1"), "rc-usd"),
            ReservationError,
        )
        result.rejects(
            "invalid_amount_rejected",
            lambda: FinOpsService(session, a.tenant_id).preflight(foreign_currency, D("1")),
            ValueError,
        )
    with Session(engine) as session:
        result.reservation_count = int(
            session.scalar(
                select(func.count())
                .select_from(BudgetReservation)
                .where(BudgetReservation.tenant_id == a.tenant_id)
            )
            or 0
        )
    _invariant(engine, a, today, later, result)


def _book(
    session: Session,
    tenancy: Tenancy,
    *,
    actual_cost: float | None,
    correlation: UUID | None = None,
    input_tokens: int = 800,
    output_tokens: int = 400,
) -> UsageRecord:
    catalog = [
        ModelSpec(
            "rc-provider",
            "rc-model",
            "RC",
            input_cost_per_token=0.00001,
            output_cost_per_token=0.00002,
        )
    ]
    decision = LLMModelRouter(catalog).route(
        tenancy.tenant_id,
        ComplexitySignals(
            estimated_input_tokens=input_tokens, expected_output_tokens=output_tokens
        ),
    )
    return UsageService(session, tenancy.tenant_id, tenancy.user_id, tenancy.model_id).record(
        decision,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        actual_cost=actual_cost,
        latency_ms=1.0,
        correlation_id=correlation or uuid4(),
    )


def check_usage(engine: Engine, a: Tenancy, b: Tenancy, today: date, result: FinOpsResult) -> None:
    correlation = uuid4()
    with transaction(engine) as session:
        record_id = _book(session, a, actual_cost=None, correlation=correlation).id
        _book(session, a, actual_cost=0.1 + 0.2)  # binary-float noise must quantize exactly
        _book(session, b, actual_cost=1.0)
    with Session(engine) as session:
        stored = session.get(UsageRecord, (a.tenant_id, record_id))
        result.check(
            "usage_accounting_verified",
            stored is not None
            and stored.cost == D("0.01600000")
            and (stored.input_tokens, stored.output_tokens, stored.total_tokens) == (800, 400, 1200)
            and (stored.provider_id, stored.model_id_text) == ("rc-provider", "rc-model"),
        )
    before = _spend(engine, a.tenant_id, today)
    try:
        with transaction(engine) as session:
            _book(session, a, actual_cost=None, correlation=correlation)
        result.check("duplicate_billing_prevented", False)
    except IntegrityError:
        result.check("duplicate_billing_prevented", _spend(engine, a.tenant_id, today) == before)
    unknown_model = Tenancy(a.tenant_id, a.budget_id, a.user_id, uuid4())
    try:
        with transaction(engine) as session:
            _book(session, unknown_model, actual_cost=1.0)
        result.check("unknown_cost_metadata_rejected", False)
    except IntegrityError:
        result.check("unknown_cost_metadata_rejected", True)
    for bad in (
        {"actual_cost": -1.0},
        {"actual_cost": float("nan")},
        {"actual_cost": None, "input_tokens": -1},
    ):
        try:
            with transaction(engine) as session:
                _book(session, a, **bad)
            result.check("usage_accounting_verified", False)
        except ValueError:
            result.check("usage_accounting_verified", True)
    result.committed_amount = _spend(engine, a.tenant_id, today)
    result.check("usage_accounting_verified", result.committed_amount == D("45.31600000"))
    result.leak(_spend(engine, b.tenant_id, today) == D("1.00000000"))


def check_forecast_alerts(
    engine: Engine, a: Tenancy, b: Tenancy, today: date, result: FinOpsResult
) -> None:
    start, end = period(today)
    total, elapsed = (end - start).days, (today - start).days + 1
    with transaction(engine) as session:
        a_service, b_service = (
            FinOpsService(session, a.tenant_id),
            FinOpsService(session, b.tenant_id),
        )
        expected = (D("45.31600000") * total / elapsed).quantize(D("0.00000001"))
        result.check("forecast_verified", a_service.forecast(start, end, today) == expected)
        result.check(
            "forecast_verified",
            a_service.forecast(start, end, end + timedelta(days=5)) == D("45.31600000"),
        )
        result.leak(
            b_service.forecast(start, end, today)
            == (D("1") * total / elapsed).quantize(D("0.00000001"))
        )
        warning = a_service.ensure_alert(start, "warning", D("0.8"), "80% budget warning")
        repeat = a_service.ensure_alert(start, "warning", D("0.8"), "80% budget warning")
        hard = a_service.ensure_alert(start, "hard_limit", D("1.2"), "hard limit risk")
        result.check(
            "alerts_verified",
            warning.id == repeat.id and hard.id != warning.id and hard.kind != warning.kind,
        )
    with Session(engine) as session:
        a_alerts = session.scalar(
            select(func.count())
            .select_from(BudgetAlert)
            .where(BudgetAlert.tenant_id == a.tenant_id)
        )
        b_alerts = session.scalar(
            select(func.count())
            .select_from(BudgetAlert)
            .where(BudgetAlert.tenant_id == b.tenant_id)
        )
    result.check("alerts_verified", a_alerts == 2)
    result.leak(b_alerts == 0)


def check_concurrency(engine: Engine, c: Tenancy, result: FinOpsResult) -> None:
    """One focused re-assertion of the proven PostgreSQL race: 6 + 6 against a limit of 10."""
    barrier = threading.Barrier(2)
    outcomes: list[str] = []

    def attempt(index: int) -> None:
        with Session(engine) as session:
            barrier.wait()
            try:
                BudgetReservationService(session, c.tenant_id).reserve(
                    c.budget_id, D("6"), f"rc-race-{index}"
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
    reserved = _active_reserved(engine, c.tenant_id, c.budget_id, datetime.now(UTC))
    result.check(
        "concurrency_invariant", sorted(outcomes) == ["accepted", "denied"] and reserved <= D("10")
    )


def run_scenarios(
    engine: Engine, tenants: tuple[str, str, str], *, concurrency: bool
) -> FinOpsResult:
    result = FinOpsResult()
    today = date.today()
    seeded = seed(engine, tenants, today)
    a, b, c = (seeded[tenant] for tenant in tenants)
    steps: list[tuple[str, Callable[[], None]]] = [
        ("decisions", lambda: check_decisions(engine, a, b, result)),
        ("reservations", lambda: check_reservations(engine, a, b, today, result)),
        ("usage", lambda: check_usage(engine, a, b, today, result)),
        ("forecast_alerts", lambda: check_forecast_alerts(engine, a, b, today, result)),
    ]
    if concurrency:
        steps.append(("concurrency", lambda: check_concurrency(engine, c, result)))
    for name, step in steps:
        try:
            step()
        except Exception as exc:  # noqa: BLE001 - an aborted block must fail the gate
            result.errors.append(f"{name}: {type(exc).__name__}")
            result.check(f"{name}_completed", False)
    result.active_reserved_amount = _active_reserved(
        engine, a.tenant_id, a.budget_id, datetime.now(UTC)
    )
    return result


REQUIRED = (
    "allow_verified",
    "warning_verified",
    "route_cheaper_verified",
    "deny_verified",
    "exact_limit_verified",
    "reservation_persisted",
    "reservation_idempotency",
    "commit_verified",
    "release_verified",
    "expiry_verified",
    "invalid_transition_rejected",
    "invalid_amount_rejected",
    "zero_amount_deterministic",
    "usage_accounting_verified",
    "duplicate_billing_prevented",
    "unknown_cost_metadata_rejected",
    "forecast_verified",
    "alerts_verified",
    "tenant_isolation",
    "invariant_within_hard_limit",
)


def classify(
    result: FinOpsResult, paid_provider_calls: int, *, require_concurrency: bool
) -> tuple[str, str]:
    required = REQUIRED + (("concurrency_invariant",) if require_concurrency else ())
    missing = [name for name in required if name not in result.checks]
    failed = sorted(name for name, ok in result.checks.items() if not ok)
    if paid_provider_calls:
        return "FAIL", "outbound provider traffic was attempted"
    if result.finops_tenant_leakage:
        return "FAIL", "tenant accounting crossed tenant boundaries"
    if failed or missing:
        return "FAIL", f"FinOps invariants failed: {', '.join(failed + missing)}"
    return "PASS", "all implemented FinOps invariants hold on the real services"


def result_fields(result: FinOpsResult) -> dict[str, Any]:
    ok = result.checks.get
    return {
        "decimal_accounting": bool(ok("usage_accounting_verified") and ok("exact_limit_verified")),
        "reservation_count": result.reservation_count,
        "reservation_idempotency": ok("reservation_idempotency"),
        "commit_verified": ok("commit_verified"),
        "release_verified": ok("release_verified"),
        "expiry_verified": ok("expiry_verified"),
        "invalid_transition_rejected": ok("invalid_transition_rejected"),
        "hard_limit": str(HARD_LIMIT),
        "committed_amount": str(result.committed_amount),
        "active_reserved_amount": str(result.active_reserved_amount),
        "invariant_within_hard_limit": ok("invariant_within_hard_limit"),
        "allow_verified": ok("allow_verified"),
        "warning_verified": ok("warning_verified"),
        "route_cheaper_verified": ok("route_cheaper_verified"),
        "deny_verified": ok("deny_verified"),
        "exact_limit_verified": ok("exact_limit_verified"),
        "exact_limit_contract": "reaching the hard threshold is denied (>=) in both paths",
        "usage_accounting_verified": ok("usage_accounting_verified"),
        "duplicate_billing_prevented": ok("duplicate_billing_prevented"),
        "forecast_verified": ok("forecast_verified"),
        "alerts_verified": ok("alerts_verified"),
        "finops_tenant_leakage": result.finops_tenant_leakage,
        "concurrency_contract_reused": "BudgetReservationService.reserve, SELECT FOR UPDATE",
        "concurrency_invariant": ok("concurrency_invariant"),
        "finops_route_cheaper_decision": ok("route_cheaper_verified"),
        "route_cheaper_enforced_in_query_path": False,
        "finops_enforced_in_query_path": False,
        "usage_recorded_by_query_path": False,
        "preflight_includes_active_reservations": False,
        "checks": dict(sorted(result.checks.items())),
        "errors": result.errors,
    }


def _commit() -> str:
    try:
        completed = subprocess.run(  # noqa: S603
            ["git", "rev-parse", "HEAD"],  # noqa: S607
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return "unknown"
    return completed.stdout.strip() or "unknown"


def _finish(evidence: dict[str, Any], status: str, reason: str, exit_code: int) -> int:
    evidence.update(
        {
            "status": status,
            "reason": reason,
            "integration_test_exit_code": exit_code,
            "timestamp": datetime.now(UTC).isoformat(),
        }
    )
    EVIDENCE.parent.mkdir(parents=True, exist_ok=True)
    EVIDENCE.write_text(json.dumps(evidence, indent=2) + "\n", encoding="utf-8")
    return exit_code


def main() -> int:
    from scripts.model_router_gate import outbound_guard
    from scripts.postgres_concurrency_gate import (
        migration_revision,
        run_alembic,
        validate_isolated_database,
    )
    from scripts.tenant_isolation_gate import cleanup_database, unrelated_row_counts

    tenants = rc_tenants(uuid4().hex[:10])
    database_url = os.getenv("RAGOPS_TEST_DATABASE_URL", "")
    evidence: dict[str, Any] = {
        "gate": "finops",
        "status": "BLOCKED",
        "tested_commit": _commit(),
        "timestamp": datetime.now(UTC).isoformat(),
        "postgres_isolated_database": False,
        "postgres_reachable": False,
        "migration_status": "NOT_RUN",
        "tenant_count": len(tenants),
        "tenant_ids": list(tenants),
        "paid_provider_calls": 0,
        "cleanup_status": "NOT_RUN",
    }
    valid, reason, _ = validate_isolated_database(database_url)
    if not valid:
        return _finish(evidence, "BLOCKED", f"isolated PostgreSQL required: {reason}", 2)
    evidence["postgres_isolated_database"] = True
    engine = create_engine(database_url, hide_parameters=True, connect_args={"connect_timeout": 3})
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        evidence["postgres_reachable"] = True
    except Exception:  # noqa: BLE001
        engine.dispose()
        return _finish(evidence, "BLOCKED", "isolated PostgreSQL database is unreachable", 2)
    migrated = run_alembic(database_url, "upgrade", "head")
    current = run_alembic(database_url, "current")
    evidence["migration_status"] = (
        "PASS" if migrated.returncode == 0 and migration_revision(current.stdout) else "FAIL"
    )
    if evidence["migration_status"] != "PASS":
        engine.dispose()
        return _finish(evidence, "FAIL", "isolated PostgreSQL could not be migrated to head", 1)
    before = unrelated_row_counts(engine, tenants)
    result: FinOpsResult | None = None
    status, reason, exit_code = "FAIL", "FinOps scenario did not complete", 1
    with outbound_guard() as attempts:
        try:
            result = run_scenarios(engine, tenants, concurrency=True)
        except Exception as exc:  # noqa: BLE001 - evidence must not contain raw service detail
            reason = f"FinOps scenario raised {type(exc).__name__}"
        finally:
            cleanup_ok = cleanup_database(engine, tenants)
            unrelated_intact = unrelated_row_counts(engine, tenants) == before
            engine.dispose()
    evidence["paid_provider_calls"] = len(attempts)
    evidence["cleanup_status"] = "PASS" if cleanup_ok and unrelated_intact else "FAIL"
    evidence["unrelated_data_intact"] = unrelated_intact
    if result is not None:
        evidence.update(result_fields(result))
        status, reason = classify(result, len(attempts), require_concurrency=True)
    if evidence["cleanup_status"] != "PASS":
        status, reason = "FAIL", "synthetic tenant cleanup failed or touched unrelated data"
    exit_code = 0 if status == "PASS" else 1
    return _finish(evidence, status, reason, exit_code)


if __name__ == "__main__":
    raise SystemExit(main())
