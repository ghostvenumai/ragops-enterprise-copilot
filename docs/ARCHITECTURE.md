# Architecture

RAGOps Enterprise Copilot is modular and local-first. FastAPI provides the
typed service boundary and Streamlit provides the operator workspace. Core RAG
and evaluation remain deterministic without external LLM access.

## Modules

- **ragops.ingestion**: file allowlist, path safety, text loading, metadata, chunking.
- **ragops.retrieval**: tenant-aware hybrid BM25 and deterministic vector search with Unicode tokenization, title metadata, stopwords, and lexical evidence gating.
- **ragops.auth**: RBAC and tenant guard.
- **ragops.security**: prompt-injection, PII, upload controls.
- **ragops.workflows**: typed multi-agent state machine.
- **ragops.llm**: provider abstraction with deterministic default.
- **ragops.evaluation**: executable gold-set metrics.
- **ragops.monitoring**: request, latency, token, cost, and citation metrics.
- **ragops.governance**: audit logging with redaction.
- **apps.api**: FastAPI transport and OpenAPI contract.
- **apps.dashboard**: chat-first presentation, knowledge operations,
  observability, and governance views.

## Runtime Topology

~~~mermaid
flowchart LR
  U[Browser] -->|8501| D[Streamlit dashboard]
  D -->|internal HTTP| A[FastAPI]
  A --> W[Typed RAG workflow]
  W --> F[(Synthetic file store)]
  W -. adapter boundary .-> Q[(Qdrant)]
  W -. adapter boundary .-> P[(PostgreSQL)]
  A --> E[(Evidence volume)]
  D -->|read via API| E
~~~

The dashboard never connects directly to a data service. PostgreSQL and Qdrant
remain on the internal datanet; only API and dashboard ports are exposed.

## Query Workflow

~~~mermaid
flowchart TD
  A[Request] --> B[Authentication and Tenant Guard]
  B --> C[Intent Router]
  C --> D[Retrieval Planner]
  D --> E[Knowledge Retrieval Agent]
  E --> F[CRM Data Agent]
  F --> G[Compliance and Security Agent]
  G --> H[Response Composer]
  H --> I[Evidence and Citation Validator]
  I --> J[Evaluation and Audit]
~~~

## Presentation Boundary

The dashboard submits only typed API payloads. It does not import or call RAG
workflow internals. This keeps authorization, filtering, refusal behavior, and
audit logging server-side. The UI is replaceable without duplicating security
policy.

## Runtime Modes

- Local tests: deterministic provider, file-backed synthetic data.
- Demo: FastAPI, Streamlit, deterministic provider, Docker Compose.
- Evaluation: deterministic provider with the 28-case gold dataset.
- Optional provider mode: OpenAI or Azure OpenAI through environment variables.
