"""Queue abstraction with deterministic tests and optional Redis transport."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from threading import Lock
from typing import Any, cast
from uuid import UUID


@dataclass(frozen=True)
class IngestionMessage:
    job_id: UUID
    tenant_id: str
    correlation_id: UUID


class IngestionQueue:
    def enqueue(self, message: IngestionMessage) -> None:  # pragma: no cover - protocol-like base
        raise NotImplementedError

    def cancel(self, job_id: UUID, tenant_id: str) -> bool:
        raise NotImplementedError

    def status(self) -> dict[str, int]:
        raise NotImplementedError


class InMemoryIngestionQueue(IngestionQueue):
    def __init__(self) -> None:
        self._items: list[IngestionMessage] = []
        self._cancelled: set[UUID] = set()
        self._lock = Lock()

    def enqueue(self, message: IngestionMessage) -> None:
        with self._lock:
            if message.job_id not in self._cancelled and message not in self._items:
                self._items.append(message)

    def dequeue(self) -> IngestionMessage | None:
        with self._lock:
            while self._items:
                message = self._items.pop(0)
                if message.job_id not in self._cancelled:
                    return message
        return None

    def cancel(self, job_id: UUID, tenant_id: str) -> bool:
        with self._lock:
            owned = any(
                item.job_id == job_id and item.tenant_id == tenant_id for item in self._items
            )
            if owned:
                self._cancelled.add(job_id)
            return owned

    def status(self) -> dict[str, int]:
        with self._lock:
            return {"queued": sum(item.job_id not in self._cancelled for item in self._items)}


class RedisIngestionQueue(IngestionQueue):
    """Redis list transport with reliable delivery; Redis is imported only when used.

    reserve() atomically moves a message into a per-worker processing list (BLMOVE), so a
    message is only removed by ack() after the job's database commit. release() returns it
    to the queue after a failure and requeue_inflight() recovers messages a stopped or
    crashed worker left in its processing list. Socket timeouts bound every call.
    """

    def __init__(
        self, url: str, name: str = "ragops:ingestion", *, timeout_seconds: float = 5.0
    ) -> None:
        if not url.startswith(("redis://", "rediss://")):
            raise ValueError("Redis URL must use redis:// or rediss://")
        if not 0 < timeout_seconds <= 30:
            raise ValueError("Redis timeout must be between 0 and 30 seconds")
        try:
            import redis
            from redis.backoff import NoBackoff
            from redis.retry import Retry
        except ImportError as exc:  # pragma: no cover - dependency-gated integration
            raise RuntimeError("redis package is required for RedisIngestionQueue") from exc
        # No hidden client retries: the worker's bounded backoff is the only retry loop.
        self.client: redis.Redis = redis.Redis.from_url(
            url,
            decode_responses=True,
            socket_connect_timeout=timeout_seconds,
            socket_timeout=timeout_seconds,
            retry=Retry(NoBackoff(), 0),
        )
        self.name = name
        self.timeout_seconds = timeout_seconds

    def enqueue(self, message: IngestionMessage) -> None:
        self.client.rpush(self.name, _serialize(message))

    def dequeue(self, timeout: int = 1) -> IngestionMessage | None:
        item = cast(list[Any] | None, self.client.blpop([self.name], timeout=timeout))
        if not item:
            return None
        return _parse(item[1])

    def processing_list(self, worker_id: str) -> str:
        return f"{self.name}:processing:{worker_id}"

    def reserve(self, worker_id: str, timeout: float = 1.0) -> tuple[IngestionMessage, str] | None:
        if not 0 < timeout < self.timeout_seconds:
            raise ValueError("reserve timeout must be shorter than the socket timeout")
        raw = cast(
            str | None,
            self.client.blmove(
                # Redis >= 6 accepts fractional timeouts; the redis-py stub says int.
                self.name,
                self.processing_list(worker_id),
                timeout,  # type: ignore[arg-type]
                "LEFT",
                "RIGHT",
            ),
        )
        if raw is None:
            return None
        return _parse(raw), raw

    def ack(self, raw: str, worker_id: str) -> None:
        self.client.lrem(self.processing_list(worker_id), 1, raw)

    def release(self, raw: str, worker_id: str) -> None:
        with self.client.pipeline(transaction=True) as pipeline:
            pipeline.lrem(self.processing_list(worker_id), 1, raw)
            pipeline.rpush(self.name, raw)
            pipeline.execute()

    def requeue_inflight(self, worker_id: str) -> int:
        moved = 0
        while self.client.lmove(self.processing_list(worker_id), self.name, "LEFT", "RIGHT"):
            moved += 1
        return moved

    def cancel(self, job_id: UUID, tenant_id: str) -> bool:
        return False  # queued cancellation is persisted by the worker's DB transition

    def status(self) -> dict[str, int]:
        return {"queued": cast(int, self.client.llen(self.name))}


def _serialize(message: IngestionMessage) -> str:
    return json.dumps({key: str(value) for key, value in asdict(message).items()})


def _parse(raw: str) -> IngestionMessage:
    payload = json.loads(raw)
    return IngestionMessage(
        UUID(payload["job_id"]), payload["tenant_id"], UUID(payload["correlation_id"])
    )
