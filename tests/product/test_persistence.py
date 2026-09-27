"""Migration/repository contracts. Missing extra is never a passed product gate."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from uuid import uuid4

import pytest

pytest.importorskip("sqlalchemy", reason="Install declared persistence extra; product gate blocks")
pytest.importorskip("alembic", reason="Install declared persistence extra; product gate blocks")

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, event, inspect
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ragops.persistence.database import database_url, transaction
from ragops.persistence.models import Base, KnowledgeCollection, Tenant, Workspace
from ragops.persistence.repository import TenantRepository


@pytest.fixture
def database(tmp_path: Path) -> Iterator[Engine]:
    engine = create_engine(f"sqlite:///{tmp_path / 'contract.db'}")

    @event.listens_for(engine, "connect")
    def enable_foreign_keys(connection: object, record: object) -> None:
        connection.execute("PRAGMA foreign_keys=ON")

    with engine.begin() as connection:
        cfg = Config("alembic.ini")
        cfg.attributes["connection"] = connection
        command.upgrade(cfg, "head")
    yield engine
    engine.dispose()


def test_migration_from_empty_database_matches_models(database: Engine) -> None:
    with database.connect() as connection:
        context = MigrationContext.configure(connection, opts={"compare_type": True})
        assert compare_metadata(context, Base.metadata) == []
        assert set(Base.metadata.tables) <= set(inspect(connection).get_table_names())
        assert len(Base.metadata.tables) == 24


def test_reopened_database_keeps_tenant_records(database: Engine) -> None:
    with transaction(database) as session:
        session.add_all(
            [
                Tenant(id="tenant-alpha", name="Synthetic Alpha"),
                Tenant(id="tenant-beta", name="Synthetic Beta"),
            ]
        )
        session.flush()
        repo = TenantRepository(session, "tenant-alpha")
        workspace = repo.add(Workspace(tenant_id="tenant-alpha", name="Synthetic Workspace"))
        key = workspace.id
    database.dispose()
    with transaction(database) as session:
        own = TenantRepository(session, "tenant-alpha")
        other = TenantRepository(session, "tenant-beta")
        assert own.get(Workspace, key).name == "Synthetic Workspace"
        assert other.get(Workspace, key) is None
        assert other.list(Workspace) == []
        with pytest.raises(ValueError, match="Cross-tenant"):
            other.add(Workspace(tenant_id="tenant-alpha", name="Forbidden"))
        with pytest.raises(ValueError, match="limit"):
            own.list(Workspace, limit=1000)
        assert TenantRepository(session, "' OR 1=1 --").list(Workspace) == []


def test_database_rejects_cross_tenant_relationship(database: Engine) -> None:
    key = uuid4()
    with transaction(database) as session:
        session.add_all(
            [
                Tenant(id="tenant-alpha", name="Synthetic Alpha"),
                Tenant(id="tenant-beta", name="Synthetic Beta"),
            ]
        )
        session.flush()
        session.add(Workspace(id=key, tenant_id="tenant-alpha", name="Only Alpha"))
    with pytest.raises(IntegrityError), transaction(database) as session:
        session.add(
            KnowledgeCollection(
                tenant_id="tenant-beta", workspace_id=key, name="Forbidden cross-tenant parent"
            )
        )


def test_failed_transaction_rolls_back(database: Engine) -> None:
    with pytest.raises(RuntimeError), transaction(database) as session:
        session.add(Tenant(id="tenant-alpha", name="Synthetic"))
        session.flush()
        raise RuntimeError("Synthetic failure")
    with Session(database) as session:
        assert session.get(Tenant, "tenant-alpha") is None


def test_database_config_does_not_fallback_or_reveal_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("RAGOPS_DATABASE_URL_FILE", raising=False)
    monkeypatch.delenv("RAGOPS_DATABASE_URL", raising=False)
    with pytest.raises(ValueError, match="required"):
        database_url()
    monkeypatch.setenv("RAGOPS_ENV", "production")
    monkeypatch.setenv("RAGOPS_DATABASE_URL", "sqlite:///:memory:")
    with pytest.raises(ValueError, match="PostgreSQL"):
        database_url()
    monkeypatch.setenv("RAGOPS_ENV", "test")
    assert database_url().drivername == "sqlite"
    monkeypatch.setenv("RAGOPS_DATABASE_URL", "not-a-url")
    with pytest.raises(ValueError, match="Invalid database URL"):
        database_url()
