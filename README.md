# RAGOps Enterprise Copilot

RAGOps Enterprise Copilot is a local-first portfolio project for enterprise RAG,
typed multi-agent workflows, tenant isolation, AI governance, evaluation, and a
controlled autonomous Codex loop.

The default mode uses synthetic data and a deterministic local provider. It does
not require paid LLM access or API keys.

## Quick Start

~~~bash
make setup
make verify
make build
make up
~~~

Open:

- Copilot workspace: <http://localhost:8501>
- FastAPI documentation: <http://localhost:8000/docs>
- Prometheus-compatible metrics: <http://localhost:8000/metrics>

**make demo** executes the deterministic command-line scenario without requiring
Docker.

## Copilot Workspace

The German-language Streamlit application starts in a real chat workspace rather than a
static metrics page. It provides:

- governed chat with citations, evidence scores, latency, and token telemetry;
- synthetic tenant and role context selection;
- prompt suggestions for CRM, pricing, and compliance scenarios;
- tenant-filtered knowledge-base inventory and index refresh;
- 20 synthetic knowledge documents with controlled no-evidence refusal;
- live runtime and measured retrieval-quality views;
- security-control status and structured audit-event history.

The tenant and role selectors are explicit demo controls. A production
deployment must derive both values from verified identity-provider claims.

Detailed operation and design are documented in
[docs/DASHBOARD.md](docs/DASHBOARD.md). Knowledge inventory and extension rules are documented in [docs/KNOWLEDGE_BASE.md](docs/KNOWLEDGE_BASE.md).

## Important Paths

- **src/ragops/**: core package
- **apps/api/**: FastAPI entrypoint
- **apps/dashboard/**: Streamlit copilot workspace
- **data/synthetic/**: synthetic CRM and document data
- **data/evaluation/**: synthetic gold dataset
- **loop/**: controlled Codex loop
- **evidence/**: generated quality and evaluation evidence
- **docs/**: architecture, security, governance, and demo documentation

## Quality

Run the complete, fail-closed gate set with:

~~~bash
make verify
~~~

The generated evidence is stored under **evidence/**. The current verified
baseline is 46 passing tests, 96.39 percent core coverage, 100 percent retrieval
hit rate, recall@5, and citation coverage plus 77.38 percent precision@5 on the synthetic gold set, and zero tenant
leakage.

## Safety

All bundled data is synthetic. Do not add real customer data, secrets, private
documents, or production logs.
