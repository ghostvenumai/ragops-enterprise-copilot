# API

The FastAPI app is created by **ragops.api.app:create_app** and exposed through
**apps/api/main.py**. The interactive OpenAPI documentation is available at
<http://localhost:8000/docs> in the demo profile.

## Endpoints

- GET /health
- GET /ready
- GET /metrics
- POST /v1/documents/ingest
- GET /v1/documents
- GET /v1/documents/{document_id}
- DELETE /v1/documents/{document_id}
- POST /v1/query
- POST /v1/evaluations/run
- GET /v1/evaluations
- GET /v1/audit-events
- GET /v1/costs/summary
- GET /v1/workspaces
- POST /v1/workspaces
- GET /v1/collections
- POST /v1/documents/upload
- GET /v1/documents/{document_id}/versions
- POST /v1/documents/{document_id}/reindex
- GET /v1/ingestion/jobs
- GET /v1/ingestion/jobs/{job_id}
- POST /v1/ingestion/jobs/{job_id}/cancel

The Phase 3 persistence service provides the tenant-scoped management contract
for workspace, collection, document upload, version, metadata and reindex
operations. Its database-backed router is enabled only when a configured
PostgreSQL session is available; the local demo remains deliberately isolated
from that runtime.

## Query Contract

~~~bash
curl -sS http://localhost:8000/v1/query \
  -H 'content-type: application/json' \
  -d '{
    "question": "Welche Enterprise-Kunden haben offene kritische Supportfaelle und bald endende Vertraege?",
    "tenant_id": "tenant-alpha",
    "user_id": "portfolio-user",
    "role": "sales",
    "top_k": 5
  }'
~~~

A successful response contains answer, abstained, evidence_score, citations,
per-request metrics, and correlation_id. A factual response without citations
is converted into a refusal by the server-side validator.

Each citation has `source_id`, `title`, `tenant_id` and `score`. In the vector query
mode (`RAGOPS_QUERY_MODE=vector`) it also carries `document_id`,
`document_version_id`, `chunk_id` and `page_number`; in the demo mode these fields are
`null`. The vector mode answers `503` with `retrieval unavailable`, `retrieval
integrity violation`, `retrieval misconfigured` or `generation unavailable` and never
falls back to the demo corpus; see docs/RETRIEVAL_ARCHITECTURE.md.

## Idempotent Queries

In the vector query mode a client may send `Idempotency-Key` with `POST /v1/query`. The
key is an opaque token of 1 to 128 characters from `A-Z a-z 0-9 . _ ~ : + / = -`; a
malformed, oversized or repeated header returns `400 invalid idempotency key`. The header
is optional: without it every request is a new query with a new `correlation_id`, as
before. Only requests that reuse the same key are deduplicated; different keys are never
merged, and the guarantee is at most one provider execution per key and retention window,
not a global exactly-once guarantee against an external provider.

The scope is derived after authentication: the verified tenant, the authenticated
principal (issuer and subject), the endpoint and a keyed digest of the key. The same key
in another tenant or for another user is a different request, and nobody learns whether
another tenant or user used a key. Client keys are opaque and never persisted or logged:
PostgreSQL stores an HMAC-SHA256 of the key under the server secret
`RAGOPS_IDEMPOTENCY_HMAC_KEY` (or a file named by `RAGOPS_IDEMPOTENCY_HMAC_KEY_FILE`;
at least 32 bytes and 8 distinct characters, never committed, no default), a digest of
the principal, the request fingerprint and the state. A database dump alone therefore does
not allow recovering predictable keys offline without the server secret. The request
fingerprint is plain SHA-256 over the normalized question and `top_k`: it is not secret
and only decides whether a retry is the same request. A weak configured secret stops the
process at startup; without a secret (or without a database) every request that carries
the header answers `503 idempotency unavailable` before any provider call, while requests
without the header are unaffected. A unique constraint over the scope lets exactly one
concurrent request claim it.

| Same-key situation | Response | Provider | Reservation and usage |
| --- | --- | --- | --- |
| First request | normal response | as usual | as usual |
| Earlier request succeeded (also an abstention) | the stored response, header `Idempotency-Replayed: true`, same `correlation_id` | no | no new ones |
| Different question or `top_k` | `409 idempotency key reused with a different request` | no | no new ones |
| Earlier request still running | `409 idempotent request in progress`, `Retry-After: 1` | no | no new ones |
| Earlier request failed before the provider (budget limit, no eligible model, retrieval or accounting setup) | executed again with the same `correlation_id` | as usual | as usual |
| Provider rejection that was certainly not billed | the stored `503 generation unavailable`, `Idempotency-Replayed: true` | no | no new ones |
| Provider timeout or provider error, or accounting failed after the provider answered | `409 idempotent request requires reconciliation` | no | the held reservation stays; no new ones |
| Idempotency store unavailable or not configured | `503 idempotency unavailable` | no | none |

The `correlation_id` identifies the logical query: a replay returns the original one, and
the usage record and the reservation (`query:<correlation_id>`) belong to it. Records are
kept for `RAGOPS_QUERY_IDEMPOTENCY_TTL_SECONDS` (default 86400, one day, allowed 300 to
604800). A succeeded record stores the response envelope (answer, citation metadata,
metrics) for that period, so idempotency introduces bounded, tenant-scoped response
retention; expired finished records are deleted when the tenant claims a new key. After
expiry the same key starts a new logical query. Ambiguous records (provider may have
billed, outcome not booked) keep no response and never expire on their own: they need
operator reconciliation, like the held reservation they belong to. Audit events
`query_idempotency_claimed`, `_replayed`, `_conflict`, `_in_progress`,
`_reconciliation_required`, `_retryable`, `_failed`, `_ambiguous_held`, `_expired`,
`_succeeded` and `_unavailable` carry the record id and state, never the key, the
question or the answer.

## Evaluation Contract

POST /v1/evaluations/run executes the synthetic gold set. GET /v1/evaluations
returns status=available plus the measured report fields. The report's internal
pass status is preserved in the generated evidence file.

## Audit Contract

GET /v1/audit-events returns the caller's tenant's latest 100 redacted events as
structured JSON objects; events of other tenants are never returned. Fields include timestamp, event type, tenant ID, user ID, outcome,
correlation ID, and bounded details. Full questions and document bodies are not
returned.
# Model governance administration

Admins can inspect and manage the in-process approved catalog with `GET/POST/PATCH /v1/admin/providers`, `GET/POST/PATCH /v1/admin/models`, tenant policy `GET /v1/admin/model-policies` and `PUT /v1/admin/model-policies/{tenant_id}`. Admins are tenant-bound: policy listing and updates cover only the admin's own tenant. `PUT /v1/admin/model-policies/{tenant_id}` accepts `allowed_providers`, `allowed_models`, `high_risk_models` (lists of names), `default_tier`, `max_routing_tier` (`SIMPLE`, `STANDARD`, `COMPLEX`, `HIGH_RISK`), `cost_ceiling_eur` (non-negative number or null) and `fallback_enabled` (boolean); unknown fields or invalid values return 422. The provider/model catalog itself is shared process configuration, so any tenant admin can change it for all tenants; non-admin roles receive 403. Providers and models created through the API start disabled and are not routable until `PATCH /v1/admin/models/{provider_id}/{model_id}` sets `enabled` to a boolean `true`; that change keeps the entry's cost and capability metadata. The simulation returns 409 `NO_ELIGIBLE_MODEL` when no approved model satisfies the tenant policy. `POST /v1/admin/model-router/simulate` returns a sanitized deterministic routing decision and never invokes a provider.
# FinOps endpoints

Protected admin routes include `/v1/admin/finops/summary`, `/usage`, `/forecast`, `/alerts`, `/budgets`, budget creation and `/v1/admin/finops/policy/simulate`. Tenant identity is authoritative; simulation has no provider side effect. The simulation accepts `estimated_cost` and `budget_amount` as numbers or numeric strings, evaluates them with the preflight Decimal decision on the default hard-limit thresholds (80% / 100% / 120%) and returns `decision`, `state`, `reason_codes` and `estimated_post_request_spend` as an exact decimal string; negative, non-finite or non-numeric amounts return 422.

# Rate limits

All authenticated routes are rate limited per verified tenant, user and endpoint class
and return `RateLimit-Limit`, `RateLimit-Remaining` and `RateLimit-Reset`. An exceeded
limit returns 429 with `Retry-After`; an unavailable limiter returns 503 with
`Retry-After: 1`. `/health`, `/ready` and `/metrics` are exempt. See
[RATE_LIMITING.md](RATE_LIMITING.md).
