"""Rate-limit policy and key contracts; HTTP and real Redis behavior are tested separately."""

from __future__ import annotations

import pytest

from ragops.config.settings import Settings
from ragops.ops.rate_limit import (
    DeterministicRateLimiter,
    RateLimiterUnavailable,
    RedisRateLimiter,
    rate_limit_key,
    validate_policy,
)


@pytest.mark.parametrize(
    ("limit", "window"),
    [
        (0, 60),
        (-1, 60),
        (10, 0),
        (10, -5),
        (True, 60),
        (10, 1.5),
        ("10", 60),
        (100_001, 60),
        (10, 86_401),
    ],
)
def test_unsafe_policy_values_are_rejected(limit, window) -> None:
    with pytest.raises(ValueError):
        validate_policy(limit, window)
    with pytest.raises(ValueError):
        DeterministicRateLimiter(limit, window)


def test_settings_reject_malformed_or_unsafe_configuration(monkeypatch) -> None:
    with pytest.raises(ValueError):
        Settings(rate_limit_backend="nginx")
    with pytest.raises(ValueError):
        Settings(rate_limit_requests=0)
    with pytest.raises(ValueError):
        Settings(rate_limit_timeout_seconds=30)
    monkeypatch.setenv("RAGOPS_RATE_LIMIT_REQUESTS", "sixty")
    with pytest.raises(ValueError):
        Settings.from_env()
    with pytest.raises(RuntimeError):
        Settings(rate_limit_backend="redis").validate_rate_limit_configuration()
    with pytest.raises(RuntimeError, match="Production requires RAGOPS_RATE_LIMIT_BACKEND"):
        Settings(environment="production").validate_rate_limit_configuration()


def test_fixed_window_boundaries_and_remaining_never_negative() -> None:
    limiter = DeterministicRateLimiter(limit=3, window_seconds=60)
    results = [limiter.check("tenant-a", "user", "rag") for _ in range(6)]
    assert [r.allowed for r in results] == [True, True, True, False, False, False]
    assert [r.remaining for r in results] == [2, 1, 0, 0, 0, 0]
    assert all(1 <= r.retry_after <= 60 for r in results[3:])
    assert all(r.limit == 3 for r in results)


def test_tenant_route_and_delimiter_isolation() -> None:
    limiter = DeterministicRateLimiter(limit=1, window_seconds=60)
    assert limiter.check("a:b", "c", "rag").allowed
    # Previously "a:b"+"c" and "a"+"b:c" shared the key "a:b:c".
    assert limiter.check("a", "b:c", "rag").allowed
    assert limiter.check("tenant-b", "c", "rag").allowed
    assert limiter.check("a:b", "c", "upload").allowed
    assert not limiter.check("a:b", "c", "rag").allowed


def test_redis_keys_are_pseudonymized_and_collision_free() -> None:
    first = rate_limit_key("ragops:rate-limit", "a:b", "c", "rag")
    second = rate_limit_key("ragops:rate-limit", "a", "b:c", "rag")
    assert first != second and first.startswith("ragops:rate-limit:")
    assert first == rate_limit_key("ragops:rate-limit", "a:b", "c", "rag")
    for secret in ("a:b", "tenant", "user"):
        assert secret not in first.removeprefix("ragops:rate-limit:")
    with pytest.raises(ValueError):
        rate_limit_key("ns", "", "user", "rag")


class _FailingScript:
    def __init__(self, error: Exception | None = None, result: object = None) -> None:
        self.error, self.result = error, result

    def __call__(self, keys, args):
        if self.error:
            raise self.error
        return self.result


class _Client:
    def __init__(self, script: _FailingScript) -> None:
        self.script = script

    def register_script(self, source: str) -> _FailingScript:
        return self.script


@pytest.mark.parametrize(
    "script",
    [
        _FailingScript(error=ConnectionError("refused")),
        _FailingScript(error=TimeoutError("slow")),
        _FailingScript(result=[1, 1]),
        _FailingScript(result="garbage"),
    ],
)
def test_backend_failures_raise_unavailable_not_allow(script) -> None:
    limiter = RedisRateLimiter("redis://unused", client=_Client(script))
    with pytest.raises(RateLimiterUnavailable):
        limiter.check("tenant-a", "user", "rag")
