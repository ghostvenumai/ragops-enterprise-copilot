"""Redis ingestion worker runtime with bounded backoff, reliable delivery and clean shutdown.

A message is reserved into this worker's processing list, the job is processed in one
database transaction and the message is acknowledged only after the commit. Any failure
rolls back, records one attempt when PostgreSQL is reachable (the job's bounded retry
budget) and returns the message to the queue; dependency failures back off exponentially
with jitter up to a cap instead of spinning or exiting. After Redis recovers, messages left
in the processing list are requeued. The same process recovers without a restart; a stop
event finishes the in-flight message and then closes clients.
"""

from __future__ import annotations

import random
import signal
from collections.abc import Callable
from dataclasses import dataclass, field
from threading import Event
from typing import Any
from uuid import uuid4

from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from ragops.config.settings import Settings
from ragops.persistence.database import open_engine
from ragops.workers.queue import IngestionMessage, RedisIngestionQueue
from ragops.workers.worker import IngestionWorker


def failure_code(exc: BaseException) -> str:
    """Map a dependency failure to the worker's retryable codes without its message."""
    module, name = type(exc).__module__, type(exc).__name__.lower()
    if module.startswith(("sqlalchemy", "psycopg")):
        return "database_transient"
    if module.startswith("redis"):
        return "redis_unavailable"
    if module.startswith(("qdrant_client", "httpx", "httpcore")) or "qdrant" in name:
        return "qdrant_unavailable"
    return "provider_timeout" if "timeout" in name else "worker_error"


@dataclass
class RuntimeCounters:
    reserved: int = 0
    completed: int = 0
    dropped: int = 0
    released: int = 0
    requeued_inflight: int = 0
    dependency_errors: int = 0
    reserve_calls: int = 0
    loop_iterations: int = 0
    backoff_seconds: list[float] = field(default_factory=list)


class WorkerRuntime:
    def __init__(
        self,
        queue: RedisIngestionQueue,
        engine: Engine,
        vector_index: Any | None = None,
        *,
        worker_id: str | None = None,
        max_attempts: int = 3,
        reserve_timeout: float = 1.0,
        backoff_base: float = 0.1,
        backoff_max: float = 5.0,
        audit: Callable[[str, Any], None] | None = None,
    ) -> None:
        if not 0 < backoff_base <= backoff_max <= 60:
            raise ValueError("worker backoff must satisfy 0 < base <= max <= 60 seconds")
        self.queue, self.engine, self.vector_index = queue, engine, vector_index
        self.worker_id = worker_id or f"worker-{uuid4()}"
        self.max_attempts = max_attempts
        self.reserve_timeout = reserve_timeout
        self.backoff_base, self.backoff_max = backoff_base, backoff_max
        self.audit = audit
        self.counters = RuntimeCounters()
        self._consecutive_failures = 0
        self._needs_recovery = True

    def _worker(self, session: Session) -> IngestionWorker:
        return IngestionWorker(
            session,
            vector_index=self.vector_index,
            worker_id=self.worker_id,
            max_attempts=self.max_attempts,
            audit=self.audit,
        )

    def next_backoff(self) -> float:
        """Capped exponential backoff with full jitter; grows only while failures persist."""
        ceiling = min(self.backoff_max, self.backoff_base * 2**self._consecutive_failures)
        self._consecutive_failures += 1
        delay = random.uniform(self.backoff_base, max(self.backoff_base, ceiling))  # noqa: S311
        self.counters.backoff_seconds.append(round(delay, 3))
        return delay

    def run_once(self) -> str:
        """One bounded step: reserve, process, commit, acknowledge. Never raises."""
        self.counters.loop_iterations += 1
        try:
            if self._needs_recovery:
                self.counters.requeued_inflight += self.queue.requeue_inflight(self.worker_id)
                self._needs_recovery = False
            self.counters.reserve_calls += 1
            reserved = self.queue.reserve(self.worker_id, self.reserve_timeout)
        except Exception:  # noqa: BLE001 - Redis outage: keep the process alive and back off
            self._needs_recovery = True
            self.counters.dependency_errors += 1
            return "dependency_error"
        if reserved is None:
            self._consecutive_failures = 0
            return "idle"
        self.counters.reserved += 1
        message, raw = reserved
        outcome = self._handle(message, raw)
        if outcome in {"completed", "dropped"}:
            self._consecutive_failures = 0
        return outcome

    def _handle(self, message: IngestionMessage, raw: str) -> str:
        try:
            with Session(self.engine) as session:
                worker = self._worker(session)
                job = worker.process(message.job_id, message.tenant_id)
                tenant, version = job.tenant_id, job.document_version_id
                try:
                    session.commit()
                except Exception:
                    worker.remove_vectors(tenant, version)
                    raise
        except ValueError:
            # The job does not exist (its upload was rolled back) or is invalid: no durable
            # effect exists, so the message is dropped instead of retried forever.
            return self._settle(raw, "dropped")
        except Exception as exc:  # noqa: BLE001 - dependency failure during processing
            self.counters.dependency_errors += 1
            return self._fail(message, raw, failure_code(exc))
        return self._settle(raw, "completed")

    def _fail(self, message: IngestionMessage, raw: str, code: str) -> str:
        terminal = False
        try:
            with Session(self.engine) as session:
                job = self._worker(session).record_failure(message.job_id, message.tenant_id, code)
                terminal = job.status in {"failed", "completed", "cancelled"}
                session.commit()
        except Exception:  # noqa: BLE001, S110 - PostgreSQL itself is down: attempts are kept
            pass
        try:
            if terminal:
                self.queue.ack(raw, self.worker_id)
                self.counters.dropped += 1
                return "failed"
            self.queue.release(raw, self.worker_id)
            self.counters.released += 1
        except Exception:  # noqa: BLE001 - message stays in the processing list for recovery
            self._needs_recovery = True
        return "dependency_error"

    def _settle(self, raw: str, outcome: str) -> str:
        try:
            self.queue.ack(raw, self.worker_id)
        except Exception:  # noqa: BLE001 - redelivery is a no-op for terminal jobs
            self._needs_recovery = True
        if outcome == "completed":
            self.counters.completed += 1
        else:
            self.counters.dropped += 1
        return outcome

    def run(self, stop: Event) -> None:
        while not stop.is_set():
            if self.run_once() == "dependency_error":
                stop.wait(self.next_backoff())

    def close(self) -> None:
        self.queue.client.close()
        self.engine.dispose()


def build_runtime(settings: Settings) -> WorkerRuntime:
    settings.validate_async_configuration()
    if not settings.redis_url:
        raise RuntimeError("RAGOPS_REDIS_URL is required for worker runtime")
    queue = RedisIngestionQueue(
        settings.redis_url,
        settings.queue_name,
        timeout_seconds=max(2.0, settings.dependency_timeout_seconds * 2),
    )
    vector_index = None
    if settings.vector_provider == "qdrant" and settings.qdrant_url:
        from ragops.vector.index import QdrantVectorIndex

        vector_index = QdrantVectorIndex(
            settings.qdrant_url,
            settings.qdrant_collection,
            settings.embedding_dimension,
            api_key=settings.qdrant_api_key,
            timeout_seconds=max(1, round(settings.dependency_timeout_seconds)),
        )
    return WorkerRuntime(
        queue,
        open_engine(timeout_seconds=settings.dependency_timeout_seconds * 2),
        vector_index,
        max_attempts=settings.job_max_attempts,
        backoff_max=settings.worker_backoff_max_seconds,
    )


def main() -> None:
    runtime = build_runtime(Settings.from_env())
    stop = Event()
    for handled in (signal.SIGTERM, signal.SIGINT):
        signal.signal(handled, lambda _signum, _frame: stop.set())
    try:
        runtime.run(stop)
    finally:
        runtime.close()


if __name__ == "__main__":
    main()
