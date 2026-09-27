from __future__ import annotations

from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session

from ragops.knowledge.service import (
    DuplicateDocumentError,
    KnowledgeAuthorizationError,
    KnowledgeManagementService,
    LocalDocumentBlobStore,
)
from ragops.persistence.models import Tenant


@pytest.fixture
def session(tmp_path: Path):
    engine = create_engine(f"sqlite:///{tmp_path / 'km.db'}")

    @event.listens_for(engine, "connect")
    def fk(connection: object, _: object) -> None:
        connection.execute("PRAGMA foreign_keys=ON")

    with engine.begin() as connection:
        cfg = Config("alembic.ini")
        cfg.attributes["connection"] = connection
        command.upgrade(cfg, "head")
    with Session(engine) as value:
        value.add_all(
            [Tenant(id="tenant-alpha", name="Alpha"), Tenant(id="tenant-beta", name="Beta")]
        )
        value.commit()
        yield value
    engine.dispose()


def test_version_history_duplicate_and_lifecycle(session: Session, tmp_path: Path) -> None:
    service = KnowledgeManagementService(
        session, "tenant-alpha", "user-1", LocalDocumentBlobStore(tmp_path / "blobs")
    )
    workspace = service.create_workspace("Sales Germany")
    collection = service.create_collection(workspace.id, "Contracts")
    first = service.upload(
        workspace.id, collection.id, "Contract", "contract-1", "contract.txt", "text/plain", b"v1"
    )
    assert first.version.version_number == 1
    with pytest.raises(DuplicateDocumentError):
        service.upload(
            workspace.id, collection.id, "Contract", "other", "copy.txt", "text/plain", b"v1"
        )
    second = service.upload(
        workspace.id, collection.id, "Contract", "contract-1", "contract.txt", "text/plain", b"v2"
    )
    assert second.version.version_number == 2
    assert service.versions(first.document.id)[1].superseded_by == second.version.id
    assert service.versions(first.document.id)[1].ingestion_status == "superseded"
    with pytest.raises(ValueError):
        service.change_status(first.document.id, "indexed")


def test_every_lookup_is_tenant_scoped_and_missing_is_opaque(
    session: Session, tmp_path: Path
) -> None:
    alpha = KnowledgeManagementService(
        session, "tenant-alpha", "user-1", LocalDocumentBlobStore(tmp_path / "a")
    )
    beta = KnowledgeManagementService(
        session, "tenant-beta", "user-2", LocalDocumentBlobStore(tmp_path / "b")
    )
    workspace = alpha.create_workspace("Private")
    collection = alpha.create_collection(workspace.id, "Only Alpha")
    uploaded = alpha.upload(
        workspace.id, collection.id, "Secret", "secret", "secret.txt", "text/plain", b"alpha"
    )
    assert beta.list_workspaces() == []
    assert beta.list_documents() == []
    with pytest.raises(KnowledgeAuthorizationError, match="resource not found"):
        beta.get_document(uploaded.document.id)
    with pytest.raises(KnowledgeAuthorizationError, match="resource not found"):
        beta.create_collection(workspace.id, "Cross tenant")
