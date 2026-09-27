from __future__ import annotations

from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session

from ragops.knowledge.service import KnowledgeManagementService, LocalDocumentBlobStore
from ragops.persistence.models import Tenant
from ragops.workers.queue import IngestionMessage, InMemoryIngestionQueue
from ragops.workers.worker import IngestionWorker


@pytest.fixture
def session(tmp_path: Path):
    engine = create_engine(f"sqlite:///{tmp_path / 'async.db'}")

    @event.listens_for(engine, "connect")
    def fk(connection: object, _: object) -> None:
        connection.execute("PRAGMA foreign_keys=ON")

    with engine.begin() as connection:
        cfg = Config("alembic.ini")
        cfg.attributes["connection"] = connection
        command.upgrade(cfg, "head")
    with Session(engine) as value:
        value.add(Tenant(id="tenant-alpha", name="Alpha"))
        value.commit()
        yield value
    engine.dispose()


def test_worker_is_idempotent_and_retries_bounded(session: Session, tmp_path: Path) -> None:
    km = KnowledgeManagementService(
        session, "tenant-alpha", "user-1", LocalDocumentBlobStore(tmp_path / "blobs")
    )
    workspace = km.create_workspace("Async")
    collection = km.create_collection(workspace.id, "Docs")
    uploaded = km.upload(
        workspace.id, collection.id, "Guide", "guide", "guide.txt", "text/plain", b"content"
    )
    worker = IngestionWorker(session, max_attempts=2)
    job = worker.create_job("tenant-alpha", uploaded.document.id, uploaded.version.id)
    failed = worker.process(job.id, "tenant-alpha", fail_stage="embedding")
    assert failed.status == "retrying" and failed.error_message_redacted == "ingestion failed"
    failed = worker.process(job.id, "tenant-alpha", fail_stage="embedding")
    assert failed.status == "failed"
    done = worker.process(job.id, "tenant-alpha")
    assert done.status == "failed"


def test_duplicate_delivery_and_cross_tenant_job_are_safe(session: Session, tmp_path: Path) -> None:
    km = KnowledgeManagementService(
        session, "tenant-alpha", "user-1", LocalDocumentBlobStore(tmp_path / "blobs")
    )
    workspace = km.create_workspace("Async")
    collection = km.create_collection(workspace.id, "Docs")
    uploaded = km.upload(
        workspace.id, collection.id, "Guide", "guide", "guide.txt", "text/plain", b"content"
    )
    worker = IngestionWorker(session)
    job = worker.create_job("tenant-alpha", uploaded.document.id, uploaded.version.id)
    queue = InMemoryIngestionQueue()
    message = IngestionMessage(job.id, "tenant-alpha", job.correlation_id)
    queue.enqueue(message)
    queue.enqueue(message)
    assert queue.status()["queued"] == 1
    with pytest.raises(ValueError, match="job not found"):
        worker.process(job.id, "tenant-beta")
    worker.process(job.id, "tenant-alpha")
    assert worker.process(job.id, "tenant-alpha").status == "completed"
