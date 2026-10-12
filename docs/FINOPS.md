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
| Provider-boundary marker cannot be written | 503 `accounting unavailable` | no | none | released (or provably pre-provider for reconciliation) |
| Timeout, provider error | 503 `generation unavailable` | no fallback | none | stays reserved; review required |
| Provider reported no usage | 503 `accounting unavailable` | answered, answer dropped | none | stays reserved; review required |
| Usage or reservation write fails | 503 `accounting unavailable` | answered, answer dropped | none | stays reserved; recoverable from the durable receipt |

Client retries (ENT-11.5): a retry with the same `Idempotency-Key` never creates a second
reservation, provider call or usage record for a logical query that succeeded, is still
running or may have been billed (timeout, provider error, accounting failure after the
provider answered); the last case answers `409 idempotent request requires reconciliation`
and leaves the held reservation for an operator. Retries after failures before the
provider (for example the hard limit) execute again under the same `correlation_id`, so
the reservation key `query:<correlation_id>` stays unique. Retries without the header, or
with a new key, are new queries. Keys are stored only as HMAC-SHA256 digests under the
server secret `RAGOPS_IDEMPOTENCY_HMAC_KEY`; without that secret keyed requests answer
`503 idempotency unavailable` before any reservation. See docs/API.md.

Rules and limits:

- A paid model is accountable only with a price; a price of zero is accepted only for
  providers of kind `deterministic`. Fallback models without an accounting record are
  removed from the chain, so nothing runs that could not be booked.
- Fallback follows only failures the provider certainly did not bill. After a timeout or a
  provider error the cost is unknown, so the request fails and the reservation keeps the
  budget; one usage record per query could not represent a second billed attempt.
- Unknown token counts are never booked as zero (`LLMUsage.reported`).
- Reservations carry no expiry: a reservation left `reserved` keeps consuming budget until
  reconciliation resolves it from durable evidence (below). Age alone never releases it.
- `(tenant_id, correlation_id)` stays unique, and finalization is idempotent for one
  query. The correlation ID is created per HTTP request, so a client that retries a request
  that failed after the provider answered causes a second provider call and a second
  reservation; client retries are not deduplicated.
- The `ROUTE_CHEAPER` decision is still not consumed, and the demo mode does not account.

## Reservation Reconciliation (ENT-11.6)

When billing is uncertain, the hold is preserved. A reservation stays `reserved` whenever
the provider may have billed and the usage was not booked: a timeout or ambiguous provider
error, a provider that reported no usage, a usage or reservation write that failed after the
provider answered, or a process that stopped around the provider/accounting boundary.
Reconciliation makes these cases discoverable, auditable and, where durable evidence is
sufficient, recoverable — without ever calling a provider again.

### Durable execution ledger

PostgreSQL stays the authority. Every accounted query attempt has one row in
`query_executions` (migration `0008_query_reconciliation`), unique per reservation and per
`(correlation_id, attempt)`. It holds identifiers, states, token counts and amounts only:
no question, context, answer, `Idempotency-Key` or credential.

| Ledger state | Written | Proves |
| --- | --- | --- |
| `prepared` | in the reservation's own transaction | the provider has not been invoked by this workflow |
| `provider_may_run` | committed **before** the provider is invoked | the provider may have run (a crash right after the marker is a conservative false positive) |
| `receipt_recorded` | committed after the provider returned with reported usage | provider, model, model configuration, token counts, the execution-time price snapshot, the cost, latency and fallback count |
| `accounted` | together with the usage record and the reservation commit | the query is booked |
| `released` | together with the reservation release | the provider certainly did not bill this attempt |

If the `provider_may_run` marker cannot be committed, the provider is not invoked; the
workflow releases the reservation and the idempotency record becomes `retryable`. A missing
marker is therefore proof that this workflow never invoked the provider. The receipt keeps
the prices that applied to the execution; reconciliation never re-prices a query from the
current model catalog. Unreported usage never produces a receipt and is never booked as
zero; the ledger records only why the outcome is unknown (`ambiguity_reason`, for example
`provider_timeout`, `provider_error`, `usage_not_reported`).

### Classification

`ragops.finops.reconciliation.ReconciliationService` classifies a case from the
reservation, its ledger row, the usage record and the idempotency record of the same
correlation, all inside one verified tenant:

| Classification | Durable evidence | Action |
| --- | --- | --- |
| `already_accounted` | a usage record exists | commit the reservation with that usage cost; no second usage record |
| `receipt_available` | `receipt_recorded`, no usage record, cost follows from its tokens and price snapshot | book exactly one usage record and commit the reservation, as the workflow would have |
| `pre_provider` | `prepared`: the boundary was never crossed | release the reservation; an `executing` idempotency record becomes `retryable` in the same transaction |
| `unknown_billing` | `provider_may_run` without receipt, or an ENT-11.5 hold without ledger evidence | none: the hold stays, review required |
| `invalid` | contradictory or forged state: cost not explained by the receipt, ledger detached from its reservation, usage contradicting the receipt, closed reservation with an open ledger, idempotency state contradicting the ledger | none: fail closed, review required |
| `in_flight` | younger than the grace period (default 300 s) | none: a live request may still settle it |
| `resolved` | already terminal | none (repeated reconciliation is idempotent) |

Every action re-reads and re-classifies the case under row locks (reservation, then ledger
— the same order as the workflow) in one transaction. Ten concurrent reconcilers of one case
produce one financial resolution, at most one usage record and one reservation transition;
a datastore failure rolls back and leaves the state unchanged. The grace period protects
liveness only: correctness comes from the conditional, locked writes. Age is never evidence
that a provider did not bill, and a provider is never replayed to find out.

### Idempotency interaction

A reconciled billed query never executes again under its key: the idempotency record
becomes `reconciled` and the key replays `409 idempotent request was settled without a
deliverable response` until the record expires (the answer was never delivered and is not
regenerated). `failed` keeps its meaning (certainly not billed). After a `pre_provider`
release the key is `retryable` and executes again as the same logical query with a fresh
reservation (`query:<correlation_id>:<attempt>`). ENT-11.5 ambiguous records are not
rewritten by the migration and stay `ambiguous_held` until reconciliation finds evidence.

### Legacy ENT-11.5 holds

Reservations held before `0008_query_reconciliation` have no ledger row. They are listed as
`unknown_billing` (`legacy = true`) and are never released automatically; a legacy hold whose
usage record already exists is committed with that cost.

### Operator workflow

```bash
export RAGOPS_DATABASE_URL=...   # from the operator's secret store, never as an argument
.venv/bin/python scripts/reconcile_reservations.py --tenant <tenant> list
.venv/bin/python scripts/reconcile_reservations.py --tenant <tenant> inspect <id>
.venv/bin/python scripts/reconcile_reservations.py --tenant <tenant> reconcile <id>
.venv/bin/python scripts/reconcile_reservations.py --tenant <tenant> auto --limit 50
```

`<id>` is a reservation, ledger or correlation id of that tenant; another tenant's id is
`case not found`. Output is JSON with identifiers, states, classifications and counts.
`auto` acts only on `already_accounted`, `receipt_available` and `pre_provider` cases. There
is no switch to force a release or to assume that nothing was billed, and no HTTP endpoint.

### Known limitations

- Cases classified `unknown_billing` stay held until provider-native billing evidence
  exists; provider invoice or billing-portal reconciliation is not implemented and would be
  a later extension. Such a hold keeps consuming budget.
- The workflow cannot distinguish a crash right after the `provider_may_run` commit from a
  provider that billed: both stay held (a conservative false positive, never a false
  negative).
- The answer of a recovered query is not stored and cannot be returned.
- The `reservation_reconciliation` RC gate and LIVE-12 prove these semantics with the
  deterministic provider on a temporary PostgreSQL database (docs/RELEASE_CANDIDATE.md).
