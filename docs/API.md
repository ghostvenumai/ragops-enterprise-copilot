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

GET /v1/audit-events returns the latest 100 redacted events as structured JSON
objects. Fields include timestamp, event type, tenant ID, user ID, outcome,
correlation ID, and bounded details. Full questions and document bodies are not
returned.
