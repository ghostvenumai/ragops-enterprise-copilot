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

Query path (ENT-11.3): in `RAGOPS_QUERY_MODE=vector`, `/v1/query` routes every
answer through `LLMModelRouter` and `QueryAccounting`
(`src/ragops/finops/query_accounting.py`). After the context is built it routes for the
verified tenant, resolves the caller's `User` (issuer and subject of the token), the
`ModelConfiguration` of every model the decision may execute and the budget covering
today, and reserves the most expensive reachable outcome (estimated input tokens plus
`RAGOPS_LLM_MAX_OUTPUT_TOKENS`, at the tenant's configured prices) under the idempotency
key `query:<correlation_id>` in its own committed transaction. Only then does the router
execute the provider chain. Usage and the reservation commit are booked in one
transaction; the answer is returned only after that commit.

| Situation | Response | Provider | Usage | Reservation |
| --- | --- | --- | --- | --- |
| No context | 200, abstained | no | none | none |
| No eligible model | 409 `no eligible model` | no | none | none |
| User, model record, pricing or budget missing; no database | 503 `accounting not configured` | no | none | none |
| Hard limit reached | 429 `budget_limit_exceeded` | no | none | none |
| Rate limit or model unavailable | fallback along the policy chain | next model | once, on the model that answered | committed |
| Authentication, invalid request, context limit, policy denied | 503 `generation unavailable` | no fallback | none | released |
| Timeout, provider error | 503 `generation unavailable` | no fallback | none | stays reserved |
| Provider reported no usage | 503 `accounting unavailable` | answered, answer dropped | none | stays reserved |
| Usage or reservation write fails | 503 `accounting unavailable` | answered, answer dropped | none | stays reserved |

Rules and limits:

- A paid model is accountable only with a price; a price of zero is accepted only for
  providers of kind `deterministic`. Fallback models without an accounting record are
  removed from the chain, so nothing runs that could not be booked.
- Fallback follows only failures the provider certainly did not bill. After a timeout or a
  provider error the cost is unknown, so the request fails and the reservation keeps the
  budget; one usage record per query could not represent a second billed attempt.
- Unknown token counts are never booked as zero (`LLMUsage.reported`).
- Reservations carry no expiry: a reservation left `reserved` keeps consuming budget until
  an operator reconciles it against the provider's billing.
- `(tenant_id, correlation_id)` stays unique, and finalization is idempotent for one
  query. The correlation ID is created per HTTP request, so a client that retries a request
  that failed after the provider answered causes a second provider call and a second
  reservation; client retries are not deduplicated.
- The `ROUTE_CHEAPER` decision is still not consumed, and the demo mode does not account.
