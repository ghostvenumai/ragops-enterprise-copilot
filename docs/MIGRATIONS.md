# Migrations

Status: unverified foundation; no production database was changed.

Alembic revision `0001_enterprise` contains explicit, frozen table operations.
It does not import current application models or call metadata.create_all().
The Alembic environment compares the actual schema against current models.
Destructive downgrades are refused; upgrades and recovery require operator review.

After installing the persistence extra, configure exactly one database URL source:
`RAGOPS_DATABASE_URL` or `RAGOPS_DATABASE_URL_FILE`. File injection is preferred.
The value must use `postgresql+psycopg://` and identify a host and database.
Do not put credentials in shell history or commit secret files.
SQLite URLs are accepted only when `RAGOPS_ENV=test`.

```bash
make migrate
make migration-check
```

`make migrate` explicitly applies upgrades to the operator-configured database.
It must not be run casually against production. `make migration-check` checks
schema drift without creating a revision. Neither command falls back to fixture
storage. Both require the installed persistence extra and a reachable database.
The application never automatically runs either command.

Local contracts:

```bash
.venv/bin/python -m pytest tests/product/test_persistence.py
```

Real PostgreSQL gate:

```bash
.venv/bin/python -m pytest tests/product/test_postgres.py
```

Set `RAGOPS_TEST_DATABASE_URL` to an EMPTY disposable database whose name begins
with `ragops_test_`. The integration test refuses non-test names or existing
tables. It leaves the test schema in place and does not delete data. CI provisions
an ephemeral PostgreSQL service with synthetic credentials for this purpose.

This revision has not been executed in the current sandbox. Dependency install,
Alembic consistency, PostgreSQL migration and restart gates must pass before the
persistence phase is complete. Back up and rehearse future production migrations
in staging before deployment; a backup implementation does not yet exist here.
