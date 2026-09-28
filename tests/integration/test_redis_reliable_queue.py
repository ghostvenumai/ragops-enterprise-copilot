"""Reliable Redis delivery against a live Redis; skipped without an authorized service."""

from __future__ import annotations

import os
import socket
import time
from uuid import uuid4

import pytest

redis = pytest.importorskip("redis", reason="install the async extra for Redis integration")

from ragops.workers.queue import IngestionMessage, RedisIngestionQueue  # noqa: E402


@pytest.fixture
def queue():
    url = os.getenv("RAGOPS_REDIS_URL")
    if not url:
        pytest.skip("RAGOPS_REDIS_URL is not configured")
    name = f"ragops:test:reliable:{uuid4().hex}"
    instance = RedisIngestionQueue(url, name, timeout_seconds=2.0)
    yield instance
    keys = list(instance.client.scan_iter(match=f"{name}*"))
    if keys:
        instance.client.delete(*keys)
    instance.client.close()


def _message() -> IngestionMessage:
    return IngestionMessage(uuid4(), "tenant-a", uuid4())


@pytest.mark.integration
def test_reserved_message_survives_until_acknowledged(queue) -> None:
    message = _message()
    queue.enqueue(message)
    reserved = queue.reserve("w1", timeout=0.2)
    assert reserved is not None and reserved[0].job_id == message.job_id
    assert queue.client.llen(queue.processing_list("w1")) == 1
    assert queue.reserve("w2", timeout=0.1) is None  # not visible to another worker
    queue.ack(reserved[1], "w1")
    assert queue.client.llen(queue.processing_list("w1")) == 0
    assert queue.client.llen(queue.name) == 0


@pytest.mark.integration
def test_release_and_crash_recovery_redeliver(queue) -> None:
    queue.enqueue(_message())
    _, raw = queue.reserve("w1", timeout=0.2)
    queue.release(raw, "w1")
    assert queue.client.llen(queue.name) == 1
    queue.reserve("w1", timeout=0.2)  # the worker "crashes" holding the message
    assert queue.requeue_inflight("w1") == 1
    assert queue.reserve("w2", timeout=0.2) is not None


@pytest.mark.integration
def test_unreachable_redis_fails_fast_without_client_retries() -> None:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    queue = RedisIngestionQueue(f"redis://127.0.0.1:{port}/0", "ragops:test:down")
    started = time.monotonic()
    with pytest.raises(redis.exceptions.ConnectionError):
        queue.enqueue(_message())
    assert time.monotonic() - started < 1.0
