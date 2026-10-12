"""The live redis_worker test cleans up a processed tenant without weakening foreign keys.

Regression (ENT-11.8): every processed version now has a document intelligence run. The live
test deleted ingestion jobs, versions and documents by hand and failed on the RESTRICT key
from ``document_intelligence_runs``. This runs the same worker path offline (SQLite with
foreign keys enforced, Alembic head) and checks both the old failure and the fixed cleanup.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

pytest.importorskip("alembic", reason="Install declared persistence extra; product gate blocks")

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, delete, event, func, select
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from tests.integration.test_redis_worker_live import delete_tenant_records

from ragops.knowledge.service import KnowledgeManagementService, LocalDocumentBlobStore
from ragops.persistence.models import (
    Base,
    DocumentIntelligenceRun,
    DocumentVersion,
    IngestionJob,
    Tenant,
)
from ragops.workers.worker import IngestionWorker

TENANT, OTHER = "rc-worker-cleanup", "rc-worker-other"


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    database = create_engine(f"sqlite:///{tmp_path / 'worker.db'}")

    @event.listens_for(database, "connect")
    def enable_foreign_keys(connection: object, record: object) -> None:
        connection.execute("PRAGMA foreign_keys=ON")  # type: ignore[attr-defined]

    with database.begin() as connection:
        cfg = Config("alembic.ini")
        cfg.attributes["connection"] = connection
        command.upgrade(cfg, "head")
    yield database
    database.dispose()


def process_text_document(session: Session, tmp_path: Path, tenant_id: str) -> None:
    session.add(Tenant(id=tenant_id, name=tenant_id))
    session.commit()
    service = KnowledgeManagementService(
        session, tenant_id, "rc-worker-user", LocalDocumentBlobStore(tmp_path / tenant_id)
    )
    workspace = service.create_workspace("RC Worker Workspace")
    collection = service.create_collection(workspace.id, "RC Worker Collection")
    uploaded = service.upload(
        workspace.id,
        collection.id,
        "RC Worker Document",
        "rc-worker-document",
        "rc-worker.txt",
        "text/plain",
        b"synthetic worker gate content",
    )
    worker = IngestionWorker(session, max_attempts=3, worker_id="rc-worker")
    job = worker.create_job(tenant_id, uploaded.document.id, uploaded.version.id)
    session.commit()
    assert worker.process(job.id, tenant_id).status == "completed"
    session.commit()


def tenant_rows(session: Session, tenant_id: str) -> dict[str, int]:
    counts = {}
    for table in Base.metadata.sorted_tables:
        if "tenant_id" in table.c:
            counts[table.name] = session.scalar(
                select(func.count()).select_from(table).where(table.c.tenant_id == tenant_id)
            )
    return {name: count for name, count in counts.items() if count}


def test_processed_version_keeps_restrict_key_against_hand_ordered_cleanup(
    engine: Engine, tmp_path: Path
) -> None:
    with Session(engine) as session:
        process_text_document(session, tmp_path, TENANT)
        assert session.scalar(
            select(func.count()).where(DocumentIntelligenceRun.tenant_id == TENANT)
        )
        # The pre-ENT-11.8 cleanup order: the intelligence run still references the version.
        session.execute(delete(IngestionJob).where(IngestionJob.tenant_id == TENANT))
        with pytest.raises(IntegrityError):
            session.execute(delete(DocumentVersion).where(DocumentVersion.tenant_id == TENANT))
        session.rollback()


def test_live_cleanup_removes_every_tenant_record_and_nothing_else(
    engine: Engine, tmp_path: Path
) -> None:
    with Session(engine) as session:
        process_text_document(session, tmp_path, TENANT)
        process_text_document(session, tmp_path, OTHER)
        before_other = tenant_rows(session, OTHER)
        assert "document_intelligence_runs" in before_other
        session.expunge_all()
        delete_tenant_records(session, TENANT)
        session.commit()
        assert tenant_rows(session, TENANT) == {}
        assert session.get(Tenant, TENANT) is None
        assert tenant_rows(session, OTHER) == before_other
