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

Admins can inspect and manage the in-process approved catalog with `GET/POST/PATCH /v1/admin/providers`, `GET/POST/PATCH /v1/admin/models`, tenant policy `GET /v1/admin/model-policies` and `PUT /v1/admin/model-policies/{tenant_id}`. Admins are tenant-bound: policy listing and updates cover only the admin's own tenant. The provider/model catalog itself is shared process configuration, so any tenant admin can change it for all tenants; non-admin roles receive 403. `POST /v1/admin/model-router/simulate` returns a sanitized deterministic routing decision and never invokes a provider.
# FinOps endpoints

Protected admin routes include `/v1/admin/finops/summary`, `/usage`, `/forecast`, `/alerts`, `/budgets`, budget creation and `/v1/admin/finops/policy/simulate`. Tenant identity is authoritative; simulation has no provider side effect.
