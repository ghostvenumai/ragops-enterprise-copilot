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
    """Redis list transport; Redis is imported only when this adapter is used."""

    def __init__(self, url: str, name: str = "ragops:ingestion") -> None:
        if not url.startswith(("redis://", "rediss://")):
            raise ValueError("Redis URL must use redis:// or rediss://")
        try:
            import redis
        except ImportError as exc:  # pragma: no cover - dependency-gated integration
            raise RuntimeError("redis package is required for RedisIngestionQueue") from exc
        self.client: redis.Redis = redis.Redis.from_url(url, decode_responses=True)
        self.name = name

    def enqueue(self, message: IngestionMessage) -> None:
        self.client.rpush(
            self.name, json.dumps({key: str(value) for key, value in asdict(message).items()})
        )

    def dequeue(self, timeout: int = 1) -> IngestionMessage | None:
        item = cast(list[Any] | None, self.client.blpop([self.name], timeout=timeout))
        if not item:
            return None
        payload = json.loads(item[1])
        return IngestionMessage(
            UUID(payload["job_id"]), payload["tenant_id"], UUID(payload["correlation_id"])
        )

    def cancel(self, job_id: UUID, tenant_id: str) -> bool:
        return False  # queued cancellation is persisted by the worker's DB transition

    def status(self) -> dict[str, int]:
        return {"queued": cast(int, self.client.llen(self.name))}
