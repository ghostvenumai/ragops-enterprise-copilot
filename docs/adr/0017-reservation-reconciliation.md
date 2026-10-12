# ADR 0017: Durable provider boundary and reservation reconciliation

Status: accepted (ENT-11.6).

The productive query path keeps a budget reservation `reserved` whenever the provider may
have billed and the usage was not booked. Until ENT-11.6 nothing durable distinguished a
reservation whose provider never ran from one whose provider may have billed, so every such
hold needed manual handling.

Decision: one tenant-scoped PostgreSQL ledger, `query_executions`, with one row per
accounted query attempt (`prepared` → `provider_may_run` → `receipt_recorded` →
`accounted`, or `released`). The row is created in the reservation's transaction; the
`provider_may_run` marker is committed before the provider is invoked and the provider is
not invoked if that commit fails; the reported usage with the execution-time price snapshot
is committed as a receipt before booking; booking writes the usage record, the reservation
commit and the ledger in one transaction through a shared `settle_receipt` path. Redis,
logs and process memory are never financial authority.

`ReconciliationService` classifies cases deterministically (`already_accounted`,
`receipt_available`, `pre_provider`, `unknown_billing`, `invalid`, `in_flight`,
`resolved`) under row locks and acts only on the first three. It never calls a provider,
never releases a reservation whose provider may have run, never books unknown usage and
never reads or writes outside the verified tenant. A billed query recovered from its receipt
moves its idempotency record to the new terminal state `reconciled` (instead of redefining
`failed`, which keeps meaning "certainly not billed"). A provably pre-provider case returns
the idempotency record to `retryable` in the same transaction.

The operator interface is a CLI (`scripts/reconcile_reservations.py`), not an HTTP
endpoint, to keep the public attack surface unchanged; it has no force, trust or
"assume not billed" option.

Alternatives rejected: releasing holds after an age threshold (age is not evidence of
non-billing); replaying the provider to learn the outcome (a second billed call);
recalculating cost from current prices (prices may have changed); a separate receipt table
(one ledger row per attempt covers the boundary, the receipt and the resolution).

Consequences: two extra small commits per accounted query (marker and receipt). Holds whose
provider may have billed without a receipt still need provider-native billing evidence,
which is a later extension. ENT-11.5 holds without a ledger row are reported as legacy and
never released automatically. The `reservation_reconciliation` RC gate and the security
control LIVE-12 prove the semantics on PostgreSQL (release target 18/18, security 29/29).
