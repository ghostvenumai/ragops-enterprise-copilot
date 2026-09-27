# Database foundation and knowledge management (0.2.0.dev0)

Status: implemented schema and repository source; execution NOT_EXECUTED locally
because production PostgreSQL is unavailable in the current sandbox. The demo
still uses JsonRepository; KM repositories are available for local integration.

Alembic revision `0002_knowledge_management` adds lifecycle metadata, current
version pointers, content hashes, storage references and tenant/status indexes.
Production schema changes must use Alembic, never `create_all`.

Install declared dependencies in the project virtual environment:

```bash
.venv/bin/python -m pip install -e '.[dev,persistence]' -c constraints.txt
```

The 19 entities are Tenant, User, RoleAssignment, Workspace,
KnowledgeCollection, Document, DocumentVersion, IngestionJob, Conversation,
Message, Citation, AuditEvent, ProviderConfiguration, ModelConfiguration,
UsageRecord, Budget, EvaluationRun, SecurityEvent and SystemSetting.

Tenant-owned primary keys combine tenant_id and a UUID. Every parent relationship
includes tenant_id in its foreign key, preventing a child from referencing a
parent in a different tenant. The tenant root uses an opaque string identifier;
future OIDC identity mapping must resolve its verified claim into that identifier.
TenantRepository requires a tenant, scopes reads before materialization, rejects
cross-tenant writes, limits list size, and exposes no generic update/delete.
The application must still enforce RBAC before calling it. PostgreSQL row-level
security and database roles for immutable history have not been implemented.

Amounts use fixed-precision NUMERIC, with EUR intended as the foundation's
accounting unit. Model prices are intended per million input/output tokens.
These tables do not yet implement billing or budget enforcement. Credential
references name externally injected secrets; never persist provider keys.
JSON columns contain structured settings/results, not file-backed persistence.
Document bytes are intended for bounded uploads (currently max 2 MB); large
objects, partitioning and external object storage require later design review.

The transaction helper commits on success and rolls back on failure. Sessions
are not shared between requests. Database errors hide bound parameters. No
schema is created on engine construction or on API startup.

Tests under tests/product cover empty migrations, model/schema consistency,
reconnect persistence, rollback, SQL injection strings and cross-tenant foreign
keys. SQLite contract tests supplement the real PostgreSQL CI job; they do not
prove PostgreSQL restart durability. Full service restart verification is pending.
