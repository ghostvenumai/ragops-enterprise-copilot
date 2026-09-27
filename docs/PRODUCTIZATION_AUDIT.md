# Productization audit — 2026-09-13

Baseline: d2696e2, version 0.1.0. Read-only inspection before production edits.
Inspected README.md, README_DE.md, SPEC.md, TASKS.yaml, AGENTS.md,
pyproject.toml, constraints.txt, Dockerfile, docker-compose.yml, Makefile,
.github/workflows/ci.yml, src/ragops/, apps/, tests/, evidence/, loop/ and automation/.
Historical evidence is not a fresh production verification.

| Component | Decision | Observed behavior and required work |
|---|---|---|
| ingestion/pipeline.py, loaders, normalization | KEEP / HARDEN | Reuse extraction and chunking; front matter currently controls tenant/access. Production must override document-supplied authority. |
| storage/json_store.py | DEPRECATE for production | CSV fixtures and process-local chunk/CRM caches, no transactional persistence. Keep explicit demo compatibility. |
| retrieval/hybrid.py | KEEP / HARDEN | Hybrid scoring/reranking work; admin bypasses tenant filter. Hashed vectors are deterministic lexical features, not semantic embeddings. Add Qdrant authorization filters. |
| auth/rbac.py, api/schemas.py | HARDEN / ADD | Client chooses tenant/user/role. Admin is cross-tenant. No token validation. Add verified identity, strict tenant boundary including admins. |
| api/app.py | HARDEN | Document lists/details, audit and evaluation endpoints unauthenticated. Ingestion/evaluation synchronous. Readiness reads fixture chunks only. |
| workflows/state_machine.py | KEEP / HARDEN | Typed fixed stages, citation/abstention, injection detection; fixed reference date and tenant-beta names are synthetic assumptions. Citation check only verifies brackets, not source references. |
| llm/providers.py | KEEP / HARDEN | Deterministic provider works. OpenAI and Azure generation raise NotImplementedError: configured adapters are stubs, not working integrations. Unknown names silently select deterministic. Add approved routing and usage accounting. |
| security/ | KEEP / HARDEN | Path/size/extension checks, injection patterns, PII masking. MIME validation, hostile filenames and production authorization require targeted tests. |
| governance/audit.py | HARDEN | Append JSONL, local storage and basic redaction; no durable tenant-scoped administrative audit. |
| monitoring/metrics.py | KEEP / HARDEN | In-process aggregates; not restart durable or multiprocess aggregated. No dependency/queue/rate-limit instrumentation. |
| evaluation/runner.py | KEEP / HARDEN | Real deterministic fixture measurements; overwrites report. Add immutable operator runs, distinguish lexical proxy scores from faithfulness measurement. |
| apps/dashboard | KEEP / HARDEN | Functional German Streamlit UI, visible demo tenant/role selectors. Needs login and production-backed management/history. |
| Dockerfile, compose | KEEP / ADD | Non-root app, dropped capabilities, read-only mounts; DB services unused by app. Add workers/Redis/proxy, production secrets and migrations. |
| tests/ | KEEP / ADD | Existing demo/API/security/evaluation/automation tests; missing JWT, DB constraints/migrations/restart, queue crash recovery, budgets and restore tests. |
| evidence/ | KEEP | Historical 140 tests and 96.02% coverage; synthetic 28-case evaluation. Not proof of OIDC, persistence or production readiness. |
| loop/, automation/ | KEEP / HARDEN | Fixed command lists, atomic state and bounded retries. Add production task roadmap, tier records and fail-closed product gates. Preserve narration/TTS cache rules. |
| CI, dependencies | HARDEN | Existing lint/type/security/audit/build; no real service integrations, migration gates or tagged release. New dependency pins need scan. |
| migrations, backup/restore, retention | ADD | No implementations found. Recovery and coordinated DB/vector snapshot behavior undefined. |

## Release-blocking findings

- HIGH: public deployment permits identity forgery and unauthenticated metadata/audit access.
- HIGH: admin retrieval and RBAC permit cross-tenant content access.
- HIGH: no durable production persistence, migration, worker recovery or backup restore.
- HIGH: readiness can report ready without any enterprise dependency.
- README correctly labels identity selection as demo, but its authentication diagram
  must not be interpreted as verified authentication. Container presence is not adapter evidence.
- Prompt-injection pattern matching is defense in depth, not a guarantee; model-output
  citation validation needs stronger source checking.

Additional inspection: scripts/review.py writes static successful findings without
performing the checks it claims. Classify REPLACE with evidence-based checks;
this report is not a separate adversarial review.

Baseline local run: 137 collected; 134 passed, 3 failed because socket creation is
denied by sandbox (video free-port tests). Full API run stalled in TestClient and
was interrupted. SQLAlchemy/Alembic/Psycopg/JWT/Redis/RQ are absent; PyPI DNS
lookup failed. Docker socket access was denied. No escalation attempted.

## Roadmap and release gates

0.2.0: audit, typed fail-closed configuration, schema/migrations and repository foundation.
0.3.0: verified identity and tenant-bound persistent API.
0.4.0: knowledge versions, queue worker, persistent vectors, operations.
0.5.0: approved model routing and FinOps.
0.9.0: production UI/deployment/CI integrated and adversarial review complete.
1.0.0: only after all briefing acceptance criteria pass with real service evidence.

Work follows product tasks in TASKS.yaml. Never automatically restore or delete production
records. SQLite contract tests may supplement but cannot replace PostgreSQL integration.

Design references: [SQLAlchemy mapped columns](https://docs.sqlalchemy.org/en/20/orm/declarative_tables.html),
[Alembic consistency checks](https://alembic.sqlalchemy.org/en/latest/autogenerate.html),
[PyJWT validation](https://pyjwt.readthedocs.io/en/stable/usage.html),
[RQ worker documentation](https://python-rq.org/docs/).

## Foundation follow-up

Implemented fixes and prepared schema are recorded in UPGRADE_GUIDE.md. Phase 0
is complete; ENT-01 remains in progress because dependency installation and
PostgreSQL verification are blocked. Later phases remain pending. All 19 entities
and a frozen revision exist as unverified source, not enabled production features.
The unconditional review report has been replaced with bounded executable checks.

## Phase 2 follow-up

The identity boundary is now implemented in `src/ragops/auth/identity.py` and
`dependencies.py`. OIDC validates asymmetric JWT signatures, issuer, audience,
expiry, subject, tenant and roles; the API uses verified context and ignores
client identity fields in OIDC mode. Development identity is explicitly bounded
to local/development/test/demo. Phase-2 evidence records 17 authentication tests,
19 authorization/persistence tests, configuration checks and type/lint results
as passed. Live OIDC discovery/key rotation, external PostgreSQL and production
deployment remain NOT_EXECUTED. Therefore the Phase-2 implementation gate passes
for the locally testable scope, while the overall production release remains blocked.

The broader product verification now measures 63.23% coverage across core, loop,
automation and video; it fails the unchanged 80% gate. Historical coverage used
src/ragops and loop only. Current local tests: 156 passed and three denied-socket
failures. API TestClient stalls at the thread/event-loop portal; diagnostic stack
is captured in evidence/product-v1/api-tests.log. Its exact cause is unresolved.

## Phase 3 follow-up

Enterprise Knowledge Management now provides tenant-owned workspace, collection,
document and version metadata through Alembic migration `0002_knowledge_management`.
The service enforces lifecycle transitions, secure generated blob keys, MIME,
extension, size and hash validation, duplicate rejection and historical version
preservation. Local Phase 3 evidence records 2 KM tests, 6 tenant/persistence
tests, 18 security tests, migration, lint and type checks as passed. Production
PostgreSQL, Docker and full OIDC ASGI integration remain NOT_EXECUTED due to the
sandbox. The next roadmap phase is asynchronous ingestion workers.

## Phase 4 follow-up

Asynchronous ingestion now has explicit persistent job state, stage/progress,
bounded retry handling, deterministic and Redis queue adapters, and an idempotent
worker boundary around the existing ingestion pipeline/vector interface. Uploads
create a job and return `202`; tenant-scoped job status and cancellation APIs are
available. Local Phase 4 evidence covers worker, lifecycle, isolation, security,
migration, lint and type checks. Live Redis, Docker worker and production
PostgreSQL gates remain NOT_EXECUTED because those services are unavailable in
the sandbox. The next roadmap phase is strict persistent Qdrant retrieval.

## Phase 5 follow-up

The `VectorIndex` boundary now has deterministic and Qdrant implementations,
stable UUID point IDs derived from SHA-256, strict embedding dimensions and payload metadata for
tenant, workspace, collection, document, version, lifecycle and access policy.
Authorized filters are constructed centrally and applied inside vector queries;
the deterministic contract applies the same checks before scoring. Worker
indexing uses the abstraction and repeated/partial upserts converge by ID.
Local vector, lifecycle, tenant/security, worker, KM, RAG, migration, MyPy and
Ruff checks pass. Live Qdrant, Docker and production PostgreSQL remain
NOT_EXECUTED because of sandbox service/socket restrictions. The next roadmap
phase is model provider and cost governance.
# ENT-06 model governance

The local Phase 6 scope is implemented with deterministic routing and explicit policy checks. Production remains blocked until external provider, PostgreSQL, Redis, Qdrant and Docker gates can be executed in an authorized environment.

## Phase 10H Qdrant live gate

The live gate now bootstraps only an isolated `rc-qdrant` service in a unique
Compose project, discovers its exact dynamic loopback REST port, and runs the
production `QdrantVectorIndex` against a unique synthetic collection. It checks
deterministic UUID point IDs, partial-write recovery, idempotent replay,
tenant/workspace/collection/access/lifecycle/validity filtering, and
tenant-scoped deletion. Qdrant validity ranges are enforced in the query
filter, and the configured schema creates datetime payload indexes. The local
Docker/Qdrant live scenario remains BLOCKED/NOT_EXECUTED in this sandbox; its
status must be established on the authorized host. The dev version remains
`0.2.0.dev0` and production release remains blocked.
