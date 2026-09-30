"""Worker runtime: ack after commit, bounded retries, backoff, recovery and compensation."""

from __future__ import annotations

import threading
import time
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import httpx
import pytest

pytest.importorskip("alembic", reason="Install declared persistence extra; product gate blocks")

from alembic import command
from alembic.config import Config
from redis.exceptions import ConnectionError as RedisConnectionError
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from ragops.knowledge.service import KnowledgeManagementService, LocalDocumentBlobStore
from ragops.persistence.database import transaction
from ragops.persistence.models import DocumentVersion, IngestionJob, Tenant
from ragops.workers.queue import IngestionMessage
from ragops.workers.runtime import WorkerRuntime, failure_code
from ragops.workers.worker import IngestionWorker


class FakeQueue:
    def __init__(self) -> None:
        self.pending: list[str] = []
        self.processing: list[str] = []
        self.acked: list[str] = []
        self.released: list[str] = []
        self.messages: dict[str, IngestionMessage] = {}
        self.fail = False
        self.client = SimpleNamespace(close=lambda: None)

    def put(self, message: IngestionMessage) -> None:
        raw = f"{message.job_id}:{len(self.messages)}"
        self.messages[raw] = message
        self.pending.append(raw)

    def reserve(self, worker_id: str, timeout: float = 1.0):
        if self.fail:
            raise RedisConnectionError("down")
        if not self.pending:
            return None
        raw = self.pending.pop(0)
        self.processing.append(raw)
        return self.messages[raw], raw

    def ack(self, raw: str, worker_id: str) -> None:
        if self.fail:
            raise RedisConnectionError("down")
        self.processing.remove(raw)
        self.acked.append(raw)

    def release(self, raw: str, worker_id: str) -> None:
        if self.fail:
            raise RedisConnectionError("down")
        self.processing.remove(raw)
        self.pending.append(raw)
        self.released.append(raw)

    def requeue_inflight(self, worker_id: str) -> int:
        if self.fail:
            raise RedisConnectionError("down")
        moved = len(self.processing)
        self.pending.extend(self.processing)
        self.processing.clear()
        return moved


class Index:
    def __init__(self, error: Exception | None = None) -> None:
        self.error, self.upserts, self.deleted = error, [], []

    def upsert(self, tenant_id: str, version_id: UUID, content_hash: str) -> None:
        self.upserts.append(version_id)
        if self.error:
            raise self.error

    def delete_version_vectors(self, tenant_id: str, version_id: str) -> int:
        self.deleted.append(version_id)
        return 0


@pytest.fixture
def engine(tmp_path: Path):
    database = create_engine(f"sqlite:///{tmp_path / 'worker.db'}")
    with database.begin() as connection:
        cfg = Config("alembic.ini")
        cfg.attributes["connection"] = connection
        command.upgrade(cfg, "head")
    yield database
    database.dispose()


def _job(engine: Engine, tmp_path: Path, max_attempts: int = 3) -> IngestionMessage:
    with transaction(engine) as session:
        if session.get(Tenant, "tenant-a") is None:
            session.add(Tenant(id="tenant-a", name="A"))
            session.flush()
        service = KnowledgeManagementService(
            session, "tenant-a", "u", LocalDocumentBlobStore(tmp_path / "b")
        )
        workspace = service.create_workspace(f"W{uuid4().hex[:6]}")
        collection = service.create_collection(workspace.id, "C")
        upload = service.upload(
            workspace.id, collection.id, "T", "k", "d.txt", "text/plain", uuid4().hex.encode()
        )
        job = IngestionWorker(session, max_attempts=max_attempts).create_job(
            "tenant-a", upload.document.id, upload.version.id
        )
        return IngestionMessage(job.id, "tenant-a", job.correlation_id)


def _status(engine: Engine, message: IngestionMessage) -> tuple[str, int]:
    with Session(engine) as session:
        job = session.get(IngestionJob, ("tenant-a", message.job_id))
        return job.status, job.attempt_count


def _runtime(queue: FakeQueue, engine: Engine, index: Index, **kwargs) -> WorkerRuntime:
    return WorkerRuntime(
        queue, engine, index, worker_id="w", backoff_base=0.01, backoff_max=0.05, **kwargs
    )  # type: ignore[arg-type]


def test_success_acknowledges_only_after_commit(engine, tmp_path) -> None:
    queue, index = FakeQueue(), Index()
    message = _job(engine, tmp_path)
    queue.put(message)
    runtime = _runtime(queue, engine, index)
    assert runtime.run_once() == "completed"
    assert (
        _status(engine, message)[0] == "completed"
        and len(queue.acked) == 1
        and not queue.processing
    )
    assert runtime.run_once() == "idle"


def test_message_for_a_missing_job_is_dropped(engine) -> None:
    queue = FakeQueue()
    queue.put(IngestionMessage(uuid4(), "tenant-a", uuid4()))
    runtime = _runtime(queue, engine, Index())
    assert runtime.run_once() == "dropped" and len(queue.acked) == 1


def test_retryable_failures_are_bounded_and_leave_no_vectors(engine, tmp_path) -> None:
    queue, index = FakeQueue(), Index(httpx.ConnectError("qdrant down"))
    message = _job(engine, tmp_path, max_attempts=3)
    queue.put(message)
    runtime = _runtime(queue, engine, index, max_attempts=3)
    outcomes = [runtime.run_once() for _ in range(5)]
    assert outcomes[:3] == ["dependency_error", "dependency_error", "failed"]
    assert outcomes[3:] == ["idle", "idle"]
    assert _status(engine, message) == ("failed", 3)
    assert len(queue.released) == 2 and len(queue.acked) == 1 and len(index.upserts) == 3


def test_database_outage_releases_without_consuming_attempts(engine, tmp_path) -> None:
    message = _job(engine, tmp_path)
    queue = FakeQueue()
    queue.put(message)
    unreachable = create_engine(f"sqlite:///{tmp_path}/missing/dir/x.db")
    runtime = _runtime(queue, unreachable, Index())
    assert runtime.run_once() == "dependency_error"
    assert queue.released and queue.pending == queue.released
    assert _status(engine, message) == ("queued", 0)
    recovered = _runtime(queue, engine, Index())
    assert recovered.run_once() == "completed"


def test_redis_outage_recovers_messages_left_in_processing(engine, tmp_path) -> None:
    queue = FakeQueue()
    message = _job(engine, tmp_path)
    queue.put(message)
    queue.reserve("w")  # a previous delivery that was never acknowledged
    runtime = _runtime(queue, engine, Index())
    queue.fail = True
    assert runtime.run_once() == "dependency_error"
    queue.fail = False
    assert runtime.run_once() == "completed"
    assert runtime.counters.requeued_inflight == 1 and _status(engine, message)[0] == "completed"


def test_backoff_is_capped_jittered_and_reset_after_success(engine, tmp_path) -> None:
    runtime = _runtime(FakeQueue(), engine, Index())
    delays = [runtime.next_backoff() for _ in range(20)]
    assert all(0.01 <= delay <= 0.05 for delay in delays)
    queue = FakeQueue()
    queue.put(_job(engine, tmp_path))
    runtime.queue = queue  # type: ignore[assignment]
    runtime.run_once()
    assert runtime._consecutive_failures == 0


def test_stop_event_ends_the_loop_promptly(engine) -> None:
    runtime = _runtime(FakeQueue(), engine, Index())
    stop = threading.Event()
    thread = threading.Thread(target=runtime.run, args=(stop,))
    thread.start()
    time.sleep(0.05)
    started = time.monotonic()
    stop.set()
    thread.join(timeout=2)
    assert not thread.is_alive() and time.monotonic() - started < 1.0


def test_vectors_are_removed_when_the_index_step_is_not_committed(
    engine, tmp_path, monkeypatch
) -> None:
    message = _job(engine, tmp_path)
    index = Index()
    with Session(engine) as session:
        worker = IngestionWorker(session, vector_index=index)

        original_flush = session.flush

        def broken_flush(*args, **kwargs):
            # The database connection drops after the vectors were written.
            if index.upserts:
                raise OperationalError("flush", {}, Exception("connection lost"))
            return original_flush(*args, **kwargs)

        monkeypatch.setattr(session, "flush", broken_flush)
        with pytest.raises(OperationalError):
            worker.process(message.job_id, "tenant-a")
    assert len(index.upserts) == 1 and index.deleted == [str(index.upserts[0])]


def test_failed_job_redelivery_is_a_noop(engine, tmp_path) -> None:
    message = _job(engine, tmp_path, max_attempts=1)
    with transaction(engine) as session:
        IngestionWorker(session).record_failure(message.job_id, "tenant-a", "worker_error")
    index = Index()
    with transaction(engine) as session:
        job = IngestionWorker(session, vector_index=index).process(message.job_id, "tenant-a")
        assert job.status == "failed"
    assert index.upserts == []
    with Session(engine) as session:
        assert (
            session.get(
                DocumentVersion,
                (
                    "tenant-a",
                    session.get(IngestionJob, ("tenant-a", message.job_id)).document_version_id,
                ),
            ).ingestion_status
            == "uploaded"
        )


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (OperationalError("x", {}, Exception()), "database_transient"),
        (RedisConnectionError(), "redis_unavailable"),
        (httpx.ConnectError("x"), "qdrant_unavailable"),
        (RuntimeError(), "worker_error"),
    ],
)
def test_failure_codes_map_dependencies_without_messages(error, code) -> None:
    assert failure_code(error) == code
