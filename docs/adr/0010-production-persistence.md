# ADR 0010: PostgreSQL persistence with SQLAlchemy and Alembic

Status: accepted design; implementation foundation awaiting execution gates.
Date: 2026-09-13.

The existing JsonRepository reads synthetic documents/CSV and caches process
state. A production product needs transactions, restart persistence and schema
upgrades while retaining deterministic demo behavior.

Use PostgreSQL with SQLAlchemy 2.x and explicit Alembic revisions. The current
foundation declares all 19 product entities. Composite tenant/entity keys and
composite parent foreign keys provide a tenant boundary on relationships.
Service code uses a tenant-scoped repository and transaction boundary. Credentials
come from an environment value or injected secret file; models store references
rather than provider secrets. Historical event mutation APIs are intentionally
absent from the generic repository.

Keep JsonRepository for explicit demo/tests. Do not expose production persistence
until identity, authorization and integration tests are available. SQLite is
limited to contract tests; PostgreSQL CI verifies actual dialect behavior. No
create_all startup strategy, automatic destructive downgrade or automatic restore.

Tradeoffs: composite keys require explicit tenant-aware joins; bounded document
bytes reside in PostgreSQL for now. Audit immutability grants, retention, queue
outbox/recovery semantics, RLS, data backfill and restart evidence remain separate
acceptance work. A schema alone is not a deployable persistence service.

References: [SQLAlchemy 2 mappings](https://docs.sqlalchemy.org/en/20/orm/declarative_tables.html)
and [Alembic migration checking](https://alembic.sqlalchemy.org/en/latest/autogenerate.html).
