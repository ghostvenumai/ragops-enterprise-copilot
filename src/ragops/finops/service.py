from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ragops.persistence.models import BudgetAlert, TenantBudget, UsageRecord


class BudgetState:
    NORMAL = "NORMAL"
    WARNING = "WARNING"
    SOFT_LIMIT = "SOFT_LIMIT"
    HARD_LIMIT = "HARD_LIMIT"


@dataclass(frozen=True)
class BudgetDecision:
    decision: str
    state: str
    reason_codes: tuple[str, ...]
    spend: Decimal
    projected_spend: Decimal


def valid_amount(value: object) -> bool:
    """Money is a finite, non-negative Decimal; floats and NaN/Infinity never enter accounting."""
    return isinstance(value, Decimal) and value.is_finite() and value >= 0


def hard_limit_ratio(budget: TenantBudget) -> Decimal:
    """A hard-limit budget without an explicit threshold fails closed at the budget amount."""
    threshold = budget.hard_limit_threshold
    return Decimal("1") if threshold is None else threshold


def budget_decision(
    budget: TenantBudget, current: Decimal, estimated_cost: Decimal, *, exempt: bool = False
) -> BudgetDecision:
    """Pure threshold decision shared by preflight and the admin simulation."""
    if (
        not valid_amount(estimated_cost)
        or not valid_amount(current)
        or budget.budget_amount <= 0
        or budget.currency != "EUR"
    ):
        raise ValueError("invalid monetary policy")
    projected = current + estimated_cost
    ratio = projected / budget.budget_amount
    if (
        ratio >= hard_limit_ratio(budget)
        and budget.enforcement_mode == "hard_limit"
        and not exempt
    ):
        return BudgetDecision(
            "DENY_BUDGET_LIMIT", BudgetState.HARD_LIMIT, ("HARD_LIMIT",), current, projected
        )
    if ratio >= budget.soft_limit_threshold and budget.enforcement_mode in {
        "optimize",
        "hard_limit",
    }:
        return BudgetDecision(
            "ROUTE_CHEAPER", BudgetState.SOFT_LIMIT, ("SOFT_LIMIT",), current, projected
        )
    if ratio >= budget.warning_threshold:
        return BudgetDecision(
            "ALLOW_WITH_WARNING",
            BudgetState.WARNING,
            ("WARNING_THRESHOLD",),
            current,
            projected,
        )
    return BudgetDecision("ALLOW", BudgetState.NORMAL, (), current, projected)


class FinOpsService:
    def __init__(self, session: Session, tenant_id: str) -> None:
        if not tenant_id:
            raise ValueError("tenant is required")
        self.session = session
        self.tenant_id = tenant_id

    def spend(self, start: date, end: date) -> Decimal:
        value = self.session.scalar(
            select(func.coalesce(func.sum(UsageRecord.cost), 0)).where(
                UsageRecord.tenant_id == self.tenant_id,
                UsageRecord.created_at >= start,
                UsageRecord.created_at < end,
            )
        )
        return Decimal(str(value or 0))

    def preflight(
        self, budget: TenantBudget, estimated_cost: Decimal, *, exempt: bool = False
    ) -> BudgetDecision:
        current = self.spend(budget.period_start, budget.period_end)
        return budget_decision(budget, current, estimated_cost, exempt=exempt)

    def forecast(self, start: date, end: date, today: date | None = None) -> Decimal:
        today = today or date.today()
        total_days = max((end - start).days, 1)
        # After the period ends the forecast equals actual spend instead of shrinking below it.
        elapsed = min(max((today - start).days + 1, 1), total_days)
        return (
            self.spend(start, min(today + timedelta(days=1), end))
            * Decimal(total_days)
            / Decimal(elapsed)
        ).quantize(Decimal("0.00000001"))

    def ensure_alert(
        self, period_start: date, kind: str, threshold: Decimal, message: str
    ) -> BudgetAlert:
        alert = self.session.scalar(
            select(BudgetAlert).where(
                BudgetAlert.tenant_id == self.tenant_id,
                BudgetAlert.period_start == period_start,
                BudgetAlert.kind == kind,
            )
        )
        if alert is None:
            alert = BudgetAlert(
                tenant_id=self.tenant_id,
                period_start=period_start,
                kind=kind,
                threshold=threshold,
                message=message,
            )
            self.session.add(alert)
            self.session.flush()
        return alert
