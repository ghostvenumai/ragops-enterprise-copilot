from __future__ import annotations

from datetime import date
from decimal import Decimal

from ragops.finops.service import BudgetState, FinOpsService


def test_budget_thresholds_are_deterministic() -> None:
    class FakeSession:
        def scalar(self, _query: object) -> Decimal:
            return Decimal("80")

    budget = type(
        "Budget",
        (),
        {
            "budget_amount": Decimal("100"),
            "currency": "EUR",
            "period_start": date(2026, 1, 1),
            "period_end": date(2026, 2, 1),
            "warning_threshold": Decimal(".8"),
            "soft_limit_threshold": Decimal("1"),
            "hard_limit_threshold": Decimal("1.2"),
            "enforcement_mode": "hard_limit",
        },
    )()
    decision = FinOpsService(FakeSession(), "tenant-a").preflight(budget, Decimal("1"))
    assert decision.state == BudgetState.WARNING
    denied = FinOpsService(FakeSession(), "tenant-a").preflight(budget, Decimal("41"))
    assert denied.decision == "DENY_BUDGET_LIMIT"
