"""Real-Redis rate-limit behavior; skipped unless an authorized Redis URL is configured."""

from __future__ import annotations

import os
import time
from collections.abc import Iterator
from uuid import uuid4

import pytest

redis = pytest.importorskip("redis", reason="install the async extra for Redis integration")

from scripts import rate_limiting_gate as gate  # noqa: E402

from ragops.ops.rate_limit import RateLimitResult, RedisRateLimiter, rate_limit_key  # noqa: E402


@pytest.fixture
def live() -> Iterator[tuple[str, str, object]]:
    url = os.getenv("RAGOPS_REDIS_URL")
    if not url:
        pytest.skip("RAGOPS_REDIS_URL is not configured")
    namespace = f"ragops:test:rate-limit:{uuid4().hex[:10]}"
    client = redis.Redis.from_url(url, socket_timeout=2)
    yield url, namespace, client
    for key in client.scan_iter(match=f"{namespace}*"):
        client.delete(key)


def test_real_redis_gate_scenarios_pass(live) -> None:
    url, namespace, client = live
    # The sentinel must sit outside the namespace pattern, like the gate's own sentinel.
    sentinel = f"ragops:test:rate-limit-sentinel:{uuid4().hex[:10]}"
    result, paid_calls, routes = gate.run(client, url, namespace, sentinel)
    client.delete(sentinel)
    assert gate.classify(result, paid_calls)[0] == "PASS", (result.checks, result.errors)
    assert result.metrics["max_observed_allowed"] == gate.CONCURRENCY_LIMIT
    assert "/v1/query" in routes["enforced"]


def test_non_atomic_limiter_overshoot_is_detected(live, monkeypatch) -> None:
    url, namespace, client = live

    def check_then_increment(self, tenant_id, user_id, endpoint_class):
        key = rate_limit_key(self.namespace, tenant_id, user_id, endpoint_class)
        current = int(self.client.get(key) or 0)
        time.sleep(0.01)  # widen the read-modify-write race a naive design has
        if current >= self.limit:
            return RateLimitResult(False, 0, 1, self.limit, 1)
        count = self.client.incr(key)
        self.client.expire(key, self.window_seconds)
        return RateLimitResult(True, max(0, self.limit - count), 0, self.limit, 1)

    monkeypatch.setattr(RedisRateLimiter, "check", check_then_increment)
    result = gate.RateLimitResult()
    gate.check_concurrency(url, namespace, client, result)
    assert result.metrics["max_observed_allowed"] > gate.CONCURRENCY_LIMIT
    assert result.checks["atomicity_verified"] is False
    assert gate.classify(result, 0) == (
        "FAIL",
        "concurrent requests exceeded the configured capacity",
    )


def test_key_without_ttl_is_healed_instead_of_becoming_immortal(live) -> None:
    url, namespace, client = live
    limiter = RedisRateLimiter(url, limit=5, window_seconds=30, namespace=namespace)
    key = rate_limit_key(namespace, "tenant-a", "user", "rag")
    client.set(key, 2)  # simulate a counter that lost its expiry
    assert limiter.check("tenant-a", "user", "rag").remaining == 2
    assert 0 < client.pttl(key) <= 30_000
