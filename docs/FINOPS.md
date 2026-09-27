# AI FinOps

FinOps reads immutable `UsageRecord` events. `TenantBudget`, `QuotaPolicy` and deduplicated `BudgetAlert` provide tenant-scoped budget control. Forecasts are estimates using current spend multiplied by period days divided by elapsed days.

Money is a finite, non-negative `Decimal`; floats, `NaN` and `Infinity` are rejected
before accounting. `UsageService` quantizes cost to the `Numeric(18, 8)` column with
`ROUND_HALF_UP` (PostgreSQL numeric rounding), and `(tenant_id, correlation_id)` is
unique, so a repeated usage event is rejected instead of billed twice. Forecasts are
capped at the period length, so after the period ends the forecast equals actual spend.

Reservations move `reserved -> committed | released | expired`. Repeating the same
terminal transition is idempotent; committing a released or expired reservation, or
releasing a committed or expired one, is rejected. A reservation may carry
`expires_at`; once expired it no longer consumes capacity and `expire_stale()` persists
the `expired` state. Commit closes the reservation only: usage records remain the
source of truth for spend, so usage is booked through `UsageService`.

Current scope: `FinOpsService`, `BudgetReservationService` and `UsageService` are not
yet called by `/v1/query`, so budgets are not enforced and usage is not recorded on the
query path, and the `ROUTE_CHEAPER` decision is not consumed by the router. The admin
simulation `POST /v1/admin/finops/policy/simulate` uses the same Decimal decision
function as preflight. The `finops` RC gate (`scripts/finops_gate.py`) proves the
services against the isolated PostgreSQL database and records these limitations.
