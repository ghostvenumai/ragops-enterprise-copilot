"""Optional live Redis gate; intentionally skipped without an authorized service."""

from __future__ import annotations

import os

import pytest

redis = pytest.importorskip("redis", reason="install the async extra for Redis integration")


@pytest.mark.integration
def test_redis_queue_round_trip() -> None:
    url = os.getenv("RAGOPS_REDIS_URL")
    if not url:
        pytest.skip("RAGOPS_REDIS_URL is not configured")
    client = redis.Redis.from_url(url)
    assert client.ping()
