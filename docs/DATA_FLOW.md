# Data Flow

## Ingestion

1. Synthetic documents and CRM CSV files are read from data/synthetic.
2. Upload and path checks enforce extension, size, filename, and traversal rules.
3. Text is normalized and chunked.
4. Metadata records tenant, classification, access level, version, validity, and
   content hash.

## Query

1. The browser sends a question and demo identity context to Streamlit.
2. Streamlit creates a bounded JSON request to POST /v1/query.
3. FastAPI validates the schema and starts a correlation-scoped workflow.
4. Retrieval filters by tenant and RBAC before scoring.
5. Prompt-injection chunks are flagged and removed from final evidence.
6. The deterministic provider creates German, source-bound summaries and never echoes raw chunks.
7. The citation validator refuses ungrounded factual output.
8. Streamlit renders answer, citations, evidence score, and bounded telemetry.

## Telemetry and Audit

Audit and metrics record correlation ID, counts, costs, latency, and security
events without full document content. The dashboard reads these values from API
endpoints; it does not mount or parse production data stores directly.

~~~mermaid
sequenceDiagram
  actor User
  participant UI as Streamlit
  participant API as FastAPI
  participant WF as RAG Workflow
  participant Store as Tenant-scoped Store
  participant Audit as Metrics and Audit

  User->>UI: Question
  UI->>API: Typed query request
  API->>WF: QueryRequest
  WF->>Store: Filtered hybrid retrieval
  Store-->>WF: Ranked evidence
  WF->>Audit: Redacted event and metrics
  WF-->>API: Answer or refusal with citations
  API-->>UI: QueryApiResponse
  UI-->>User: Answer, sources, evidence
~~~
