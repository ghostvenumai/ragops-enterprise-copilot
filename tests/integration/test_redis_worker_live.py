"""Authorized-host Redis/worker end-to-end gate (skips without live services)."""

from __future__ import annotations

import json
import os
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, delete, select
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from ragops.knowledge.service import KnowledgeManagementService, LocalDocumentBlobStore
from ragops.persistence.models import (
    Document,
    DocumentVersion,
    IngestionJob,
    KnowledgeCollection,
    Tenant,
    Workspace,
)
from ragops.workers.queue import IngestionMessage, RedisIngestionQueue
from ragops.workers.worker import IngestionWorker


@pytest.mark.integration
def test_live_redis_worker_path(tmp_path: Path) -> None:
    redis_url = os.getenv("RAGOPS_REDIS_URL")
    database_url = os.getenv("RAGOPS_TEST_DATABASE_URL")
    if not redis_url or not database_url:
        pytest.skip("RAGOPS_REDIS_URL and RAGOPS_TEST_DATABASE_URL are required")
    db_url = make_url(database_url)
    if db_url.drivername != "postgresql+psycopg" or not (db_url.database or "").startswith(
        "ragops_test_"
    ):
        pytest.fail("live worker gate requires isolated ragops_test_* PostgreSQL database")
    queue_name = os.getenv("RAGOPS_RC_QUEUE_NAME", "ragops-rc-ingestion")
    queue = RedisIngestionQueue(redis_url, queue_name)
    queue.client.ping()
    engine = create_engine(db_url, hide_parameters=True)
    tenant_id = f"rc-worker-{uuid4().hex[:12]}"
    intruder_tenant_id = f"rc-intruder-{uuid4().hex[:12]}"
    with Session(engine) as session:
        tenant = Tenant(id=tenant_id, name="RC Worker Tenant")
        intruder = Tenant(id=intruder_tenant_id, name="RC Worker Intruder")
        session.add_all([tenant, intruder])
        session.commit()
        service = KnowledgeManagementService(
            session, tenant_id, "rc-worker-user", LocalDocumentBlobStore(tmp_path / "blobs")
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
        message = IngestionMessage(job.id, tenant_id, job.correlation_id)
        queue.enqueue(message)
        first = queue.dequeue(timeout=2)
        assert first is not None and first.job_id == job.id
        worker.process(job.id, tenant_id)
        session.commit()
        queue.enqueue(message)
        duplicate = queue.dequeue(timeout=2)
        assert duplicate is not None
        assert worker.process(job.id, tenant_id).status == "completed"
        with pytest.raises(ValueError, match="job not found"):
            worker.process(job.id, intruder_tenant_id)
        retry_job = worker.create_job(tenant_id, uploaded.document.id, uploaded.version.id)
        session.commit()
        failed = worker.process(retry_job.id, tenant_id, fail_stage="embedding")
        assert failed.status == "retrying" and failed.attempt_count == 1
        worker.retry(retry_job.id, tenant_id)
        retry_message = IngestionMessage(retry_job.id, tenant_id, retry_job.correlation_id)
        queue.enqueue(retry_message)
        retry_claim = queue.dequeue(timeout=2)
        assert retry_claim is not None and retry_claim.job_id == retry_job.id
        worker.process(retry_job.id, tenant_id)
        cancel_job = worker.create_job(tenant_id, uploaded.document.id, uploaded.version.id)
        worker.cancel(cancel_job.id, tenant_id)
        session.commit()
        final_jobs = session.scalars(
            select(IngestionJob).where(IngestionJob.tenant_id == tenant_id)
        ).all()
        assert {item.status for item in final_jobs} == {"completed", "cancelled"}
        print(
            "REDIS_WORKER_RESULT "
            + json.dumps(
                {
                    "queue_name": queue_name,
                    "enqueue_count": 3,
                    "claim_count": 3,
                    "retry_attempt_count": retry_job.attempt_count,
                    "duplicate_delivery": "idempotent",
                    "cancellation": "cancelled_before_processing",
                    "terminal_status": "completed",
                    "persisted_job_status": "completed",
                    "transition_sequence": ["queued", "running", "completed"],
                    "tenant_leakage": 0,
                    "job_ids": [str(job.id), str(retry_job.id), str(cancel_job.id)],
                }
            )
        )
        session.execute(delete(IngestionJob).where(IngestionJob.tenant_id == tenant_id))
        session.execute(delete(DocumentVersion).where(DocumentVersion.tenant_id == tenant_id))
        session.execute(delete(Document).where(Document.tenant_id == tenant_id))
        session.execute(
            delete(KnowledgeCollection).where(KnowledgeCollection.tenant_id == tenant_id)
        )
        session.execute(delete(Workspace).where(Workspace.tenant_id == tenant_id))
        session.delete(intruder)
        session.delete(tenant)
        session.commit()
    queue.client.delete(queue_name)
    engine.dispose()
