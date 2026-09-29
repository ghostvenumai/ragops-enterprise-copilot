"""Upload persists the job before enqueueing it, so a fast worker never loses a job."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

pytest.importorskip("alembic", reason="Install declared persistence extra; product gate blocks")

from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from tests.security.oidc_helpers import AUDIENCE, ISSUER, bearer

from ragops.api.app import create_app
from ragops.config.settings import Settings
from ragops.knowledge.service import KnowledgeManagementService, LocalDocumentBlobStore
from ragops.persistence.database import transaction
from ragops.persistence.models import IngestionJob, Tenant
from ragops.workers.queue import InMemoryIngestionQueue


@pytest.fixture
def setup(
    keys, tmp_path: Path, monkeypatch
) -> Iterator[tuple[TestClient, str, str, dict[str, str]]]:
    url = f"sqlite:///{tmp_path / 'upload.db'}"
    engine = create_engine(url)
    with engine.begin() as connection:
        cfg = Config("alembic.ini")
        cfg.attributes["connection"] = connection
        command.upgrade(cfg, "head")
    with transaction(engine) as session:
        session.add(Tenant(id="tenant-alpha", name="A"))
        session.flush()
        service = KnowledgeManagementService(
            session,
            "tenant-alpha",
            "admin",
            LocalDocumentBlobStore(tmp_path / "data" / "document-blobs"),
        )
        workspace = service.create_workspace("W")
        collection = service.create_collection(workspace.id, "C")
        ids = (str(workspace.id), str(collection.id))
    monkeypatch.setenv("RAGOPS_DATABASE_URL", url)
    monkeypatch.setenv("RAGOPS_ENV", "test")
    settings = Settings(
        environment="test",
        identity_provider="oidc",
        oidc_issuer=ISSUER,
        oidc_audience=AUDIENCE,
        oidc_public_key=keys[1],
        data_dir=tmp_path / "data",
        evidence_dir=tmp_path / "evidence",
    )
    with TestClient(create_app(settings), raise_server_exceptions=False) as client:
        yield client, *ids, bearer(keys[0], "tenant-alpha", ["admin"])
    engine.dispose()


def _upload(client: TestClient, workspace: str, collection: str, headers: dict[str, str]):
    return client.post(
        "/v1/documents/upload",
        data={
            "workspace_id": workspace,
            "collection_id": collection,
            "title": "T",
            "logical_document_key": "k",
        },
        files={"file": ("doc.txt", b"synthetic content", "text/plain")},
        headers=headers,
    )


def test_job_is_committed_before_a_worker_can_see_the_message(setup, monkeypatch) -> None:
    client, workspace, collection, headers = setup
    visible: list[bool] = []
    original = InMemoryIngestionQueue.enqueue

    def concurrent_worker_view(self, message):
        # A worker on its own connection reads the job the moment the message exists.
        with Session(create_engine(__import__("os").environ["RAGOPS_DATABASE_URL"])) as other:
            visible.append(other.get(IngestionJob, (message.tenant_id, message.job_id)) is not None)
        original(self, message)

    monkeypatch.setattr(InMemoryIngestionQueue, "enqueue", concurrent_worker_view)
    response = _upload(client, workspace, collection, headers)
    assert response.status_code == 202
    assert visible == [True]


def test_queue_failure_leaves_a_visible_retryable_job(setup, monkeypatch) -> None:
    from redis.exceptions import ConnectionError as RedisConnectionError

    client, workspace, collection, headers = setup

    def unavailable(self, message):
        raise RedisConnectionError("queue down")

    monkeypatch.setattr(InMemoryIngestionQueue, "enqueue", unavailable)
    response = _upload(client, workspace, collection, headers)
    assert response.status_code == 503
    assert response.json() == {"detail": "dependency unavailable"}
    with Session(create_engine(__import__("os").environ["RAGOPS_DATABASE_URL"])) as session:
        job = session.scalar(select(IngestionJob))
    assert job is not None and (job.status, job.error_code) == ("failed", "queue_unavailable")
    monkeypatch.undo()
    retried = client.post(f"/v1/ingestion/jobs/{job.id}/retry", headers=headers)
    assert retried.status_code == 202 and retried.json()["status"] == "queued"


def test_job_list_names_the_tenants_own_documents(setup) -> None:
    client, workspace, collection, headers = setup
    response = client.post(
        "/v1/documents/upload",
        data={
            "workspace_id": workspace,
            "collection_id": collection,
            "title": "<b>Quartal</b>",
            "logical_document_key": "quartal",
        },
        files={"file": ("quartal.pdf", b"%PDF-1.4 synthetic", "application/pdf")},
        headers=headers,
    )
    assert response.status_code == 202
    jobs = client.get("/v1/ingestion/jobs", headers=headers).json()
    assert [(job["title"], job["filename"]) for job in jobs] == [("<b>Quartal</b>", "quartal.pdf")]


def test_renamed_file_is_rejected_before_a_job_exists(setup) -> None:
    client, workspace, collection, headers = setup
    response = client.post(
        "/v1/documents/upload",
        data={
            "workspace_id": workspace,
            "collection_id": collection,
            "title": "Defekt",
            "logical_document_key": "defekt",
        },
        files={"file": ("defekt.pdf", b"MZ renamed executable", "application/pdf")},
        headers=headers,
    )
    assert response.status_code == 400
    assert response.json() == {"detail": "file content does not match its type"}
    assert client.get("/v1/ingestion/jobs", headers=headers).json() == []
