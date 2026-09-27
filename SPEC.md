# RAGOps Enterprise Copilot Specification

## Purpose

RAGOps Enterprise Copilot is a synthetic, local-first enterprise RAG and
multi-agent portfolio project. It demonstrates document ingestion, tenant-aware
hybrid retrieval, deterministic answer generation with citations, governance,
auditability, evaluation, monitoring, Docker hardening, and a controlled Codex
automation loop.

## Scope

The system serves five fictional enterprise areas:

- Sales
- Customer service
- Operations
- Internal knowledge management
- Compliance

It processes synthetic documents and CRM records only. The default mode uses the
`DeterministicTestProvider` and requires no paid LLM provider or external API
key.

## Core Workflow

```text
Request
  -> Authentication and Tenant Guard
  -> Intent Router
  -> Retrieval Planner
  -> Knowledge Retrieval Agent
  -> CRM Data Agent
  -> Compliance and Security Agent
  -> Response Composer
  -> Evidence and Citation Validator
  -> Evaluation and Audit
```

## Required API Surface

- `GET /health`
- `GET /ready`
- `GET /metrics`
- `POST /v1/documents/ingest`
- `GET /v1/documents`
- `GET /v1/documents/{document_id}`
- `DELETE /v1/documents/{document_id}`
- `POST /v1/query`
- `POST /v1/evaluations/run`
- `GET /v1/evaluations`
- `GET /v1/audit-events`
- `GET /v1/costs/summary`

## Security Acceptance Criteria

- Role-based access control is enforced.
- Cross-tenant retrieval is blocked.
- Prompt-injection patterns in documents are detected as untrusted content.
- PII is masked before telemetry and unsafe output.
- Upload path traversal is blocked.
- Only allowed file extensions are accepted.
- Correlation IDs are generated for every request.
- Audit events avoid secrets and full confidential document content.

## Evaluation Acceptance Criteria

- Synthetic gold dataset contains at least 25 questions.
- Citation coverage is measured from real query results.
- Retrieval hit rate, recall@k, precision@k, tenant leakage, cost, latency, and
  security pass rate are computed by executable code.
- Missing evidence causes controlled abstention.

## Controlled Loop Acceptance Criteria

- The loop reads `AGENTS.md`, `SPEC.md`, `TASKS.yaml`, and the previous state.
- It chooses exactly one bounded task per iteration.
- It invokes Codex only with:

```bash
codex exec --sandbox workspace-write --ask-for-approval never "<task>"
```

- It runs allowlisted quality gates outside the model response.
- It writes atomic state files under `.loop/`.
- It stops on repeated failures, repeated no-progress, repository corruption, or
  critical security findings.

## Assumptions

Detailed assumptions are tracked in `docs/ASSUMPTIONS.md`.

## Enterprise productization amendment (0.2.0.dev0)

The v1.0 target expands the synthetic reference application into a self-hosted
knowledge product. See docs/PRODUCTION_ARCHITECTURE.md and product tasks ENT-00
through ENT-12. Until those gates pass, the current runtime remains a synthetic
demo and production startup is refused. Client identity remains unverified in
demo mode. All admins are now tenant-scoped. External provider classes currently
raise NotImplementedError during generation. The new schema and migration source
are not proof of working persistence until SQLite and PostgreSQL checks pass.

The v1.0 release requires every gate in make product-verify, real service restart
and worker recovery tests, verified identity tests, and a separate adversarial
review. NOT_EXECUTED never qualifies as a passed release gate.
