"""Real PostgreSQL gate: only an explicitly named empty synthetic test database."""

from __future__ import annotations

import os

import pytest

pytest.importorskip("sqlalchemy", reason="Persistence extra missing; product gate blocks")
pytest.importorskip("alembic", reason="Persistence extra missing; product gate blocks")
pytest.importorskip("psycopg", reason="Persistence extra missing; product gate blocks")

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, inspect
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ragops.persistence.models import Base, KnowledgeCollection, Tenant, Workspace
from ragops.persistence.repository import TenantRepository


@pytest.mark.integration
def test_empty_postgres_migration_constraints_and_reconnect() -> None:
    raw = os.getenv("RAGOPS_TEST_DATABASE_URL")
    if not raw:
        pytest.skip("RAGOPS_TEST_DATABASE_URL absent; real PostgreSQL gate NOT_EXECUTED")
    url = make_url(raw)
    if (
        url.drivername != "postgresql+psycopg"
        or not url.database
        or not url.database.startswith("ragops_test_")
    ):
        pytest.fail("Only an explicit ragops_test_* PostgreSQL database is allowed")
    engine = create_engine(url, hide_parameters=True, connect_args={"connect_timeout": 5})
    try:
        with engine.begin() as connection:
            assert inspect(connection).get_table_names() == [], "Test database must be empty"
            cfg = Config("alembic.ini")
            cfg.attributes["connection"] = connection
            command.upgrade(cfg, "head")
            assert compare_metadata(MigrationContext.configure(connection), Base.metadata) == []
        with Session(engine) as session, session.begin():
            session.add_all(
                [
                    Tenant(id="tenant-alpha", name="Synthetic Alpha"),
                    Tenant(id="tenant-beta", name="Synthetic Beta"),
                ]
            )
            session.flush()
            workspace = TenantRepository(session, "tenant-alpha").add(
                Workspace(tenant_id="tenant-alpha", name="Synthetic persistent workspace")
            )
            key = workspace.id
        engine.dispose()
        with Session(engine) as session:
            assert TenantRepository(session, "tenant-alpha").get(Workspace, key) is not None
            assert TenantRepository(session, "tenant-beta").get(Workspace, key) is None
        with pytest.raises(IntegrityError), Session(engine) as session, session.begin():
            session.add(
                KnowledgeCollection(
                    tenant_id="tenant-beta", workspace_id=key, name="Synthetic forbidden parent"
                )
            )
            session.flush()
        # No database deletion, schema drop or production restore is performed by this test.
    finally:
        engine.dispose()
