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
- GET /v1/documents/{document_id}/versions/{version_id}/pages
- GET /v1/documents/{document_id}/versions/{version_id}/chunks/{chunk_index}/provenance
- POST /v1/documents/{document_id}/reindex
- GET /v1/ingestion/jobs
- GET /v1/ingestion/jobs/{job_id}
- POST /v1/ingestion/jobs/{job_id}/cancel

The Phase 3 persistence service provides the tenant-scoped management contract
for workspace, collection, document upload, version, metadata and reindex
operations. Its database-backed router is enabled only when a configured
PostgreSQL session is available; the local demo remains deliberately isolated
from that runtime.

## Uploads and Scanned Documents (ENT-11.7)

`POST /v1/documents/upload` accepts PDF, JPEG (`.jpg`, `.jpeg`), PNG, DOCX, Markdown, TXT
and CSV. Extension, declared MIME type and file signature must agree, and the content is
inspected structurally before it is stored. PDF, JPEG and PNG may be up to 12 MB (the upload
route accepts request bodies up to `RAGOPS_MAX_UPLOAD_REQUEST_BYTES`, 12.6 MB by default;
every other route keeps `RAGOPS_MAX_REQUEST_BYTES`); the other formats keep 2 MB. Images
must be single-frame, at most 40 megapixels and 20 000 px per side (checked from the header
before decoding) and decode completely. Rejections are `400` with `file content does not
match its type`, `file is encrypted or contains active content`, `image exceeds safety
limits`, `file exceeds size limit` or `unsupported file type or MIME type`; an oversized
body is `413`.

The worker decides per page whether OCR is needed (see ADR 0018). `GET /v1/ingestion/jobs`
reports `review_required`, `pages_total` and `pages_review` per job; a version with pages
that could not be read reliably is indexed only with its acceptable pages and has
`ingestion_status = review_required`. Failure codes include `ocr_engine_unavailable`,
`ocr_language_unavailable` and `ocr_review_required`.

`GET /v1/documents/{document_id}/versions/{version_id}/pages` returns the page report:
per page `page_number`, `classification` (`native_text`, `scanned_image`,
`mixed_content`, `empty_page`, `unreadable`, `ocr_review_required`), `extraction_method`,
`quality_state`, `mean_confidence` (Tesseract's word score, not a probability of
correctness), counts, flags, transformations, page size and unit, `coordinate_space`,
engine, engine version, configuration version, language and `ocr_milliseconds`.
`GET .../chunks/{chunk_index}/provenance` returns a chunk's `chunk_id`, `page_number`,
`source_name`, `extraction_method` and its OCR line `regions` as `[x0, y0, x1, y1]` in
`page-normalized-v1` (origin top-left of the displayed page, values in `[0, 1]`). Both
carry no document text and answer `404` for another tenant, another document's version or a
document above the caller's access level.

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
| Reconciliation booked the billed query from its durable receipt (ENT-11.6) | the stored `409 idempotent request was settled without a deliverable response`, `Idempotency-Replayed: true` | no | no new ones |
| Reconciliation proved the provider never ran and released the reservation | executed again with the same `correlation_id` | as usual | a fresh reservation |
| Idempotency store unavailable or not configured | `503 idempotency unavailable` | no | none |

The `correlation_id` identifies the logical query: a replay returns the original one, and
the usage record and the reservation (`query:<correlation_id>`) belong to it. Records are
kept for `RAGOPS_QUERY_IDEMPOTENCY_TTL_SECONDS` (default 86400, one day, allowed 300 to
604800). A succeeded record stores the response envelope (answer, citation metadata,
metrics) for that period, so idempotency introduces bounded, tenant-scoped response
retention; expired finished records are deleted when the tenant claims a new key. After
expiry the same key starts a new logical query. Ambiguous records (provider may have
billed, outcome not booked) keep no response and never expire on their own: they need
operator reconciliation, like the held reservation they belong to. Reconciliation
(`scripts/reconcile_reservations.py`, docs/FINOPS.md) turns them into `reconciled` (billed,
booked, not re-executable; expires like a finished record) only from durable evidence. Audit events
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

# Document intelligence (ENT-11.8)

Structured, deterministic answers from PostgreSQL about the caller's documents (ADR 0019).
Every route needs a verified identity (401 otherwise) and is rate limited in the
`intelligence` endpoint class. Reads cover only the caller's tenant, documents at or below the
caller's access level and each document's current version; anything else is 404 and absent
from every list. Writes need the `admin` or `compliance` role (403 otherwise). Request bodies
reject unknown fields (422), so a client cannot supply pages, spans, source references or raw
values. No model is called; `/v1/query` is unchanged.

| Route | Purpose |
|---|---|
| `GET /v1/intelligence/documents?classification=&person_id=&review_required=` | Documents with primary class (`doc-taxonomy-v1`), classification status, debtor status (`identified`, `ambiguous`, `unmatched`, `unknown`) and review flags; `person_id` lists documents where that person is an evidenced or confirmed debtor |
| `GET /v1/intelligence/documents/{document_id}` | Classification candidates with rule evidence and strength, facts, person links (`debtor`/`addressee`, state, basis) and claim memberships of the current version |
| `GET /v1/intelligence/facts/{fact_id}` | One fact with its source: version, page, line, character span, raw stated value, chunk index, extraction method and, for OCR, the `page-normalized-v1` line box; `effective` shows a reviewer's correction beside the immutable source value |
| `GET /v1/intelligence/creditors` | Normalized creditors (creditor, original creditor, current claimant) with document and fact references |
| `GET /v1/intelligence/claims?person_id=` | Claim groups with member documents and amount roles (`historical`, `potentially_current`, `component`, `payment`, `unclassified`), the potentially current amount with its date and basis, `settled`, and `unresolved` items; `consolidated_total` (per currency) is present only when nothing is unresolved, otherwise `null` with `resolved_subtotal` |
| `GET /v1/intelligence/deadlines?person_id=` | Due dates and payment deadlines with sources |
| `GET /v1/intelligence/findings` | Everything that needs a human: review-required facts, ambiguous classifications and debtors, flagged documents, failed analyses, unresolved groups |
| `GET /v1/intelligence/persons` | Person profiles (label, status, alias count; alias values are never listed) |
| `POST /v1/intelligence/persons` | `{label}`; 409 for an existing label |
| `POST /v1/intelligence/persons/{id}/aliases` | `{kind: name\|identifier, value}`; 409 if the normalized alias already belongs to a person of the tenant; existing documents are re-attributed |
| `POST /v1/intelligence/reviews` | `{entity_type, entity_id, action, expected_version, reason_category, value?, person_id?, role?}`; actions per entity: fact `confirm`/`reject`/`correct`/`mark_for_review`, classification `confirm`/`reject`/`mark_for_review`, person link `confirm`/`reject`, claim group `confirm`/`dismiss`, claim member `confirm`/`reject`, document `assign_person`/`mark_for_review`/`clear_review_flag`; a stale `expected_version` is 409; a correction is typed (Decimal amount ≥ 0 with two places and an ISO currency, ISO date or text) and stored beside the source fact; every decision is an append-only event with actor, previous and new state |
| `POST /v1/intelligence/documents/{id}/reprocess` | 202; re-runs extraction and analysis of the current version through the worker (idempotent) |
