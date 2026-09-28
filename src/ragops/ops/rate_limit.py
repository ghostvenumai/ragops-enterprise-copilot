"""Fixed-window request limits keyed by authenticated tenant, user and endpoint class.

The window is fixed, not sliding: a caller can use up to the limit at the end of one
window and again at the start of the next. Redis enforcement runs as one Lua script,
so concurrent callers and multiple app instances share one atomic counter.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from time import monotonic
from typing import Any, Protocol

MAX_LIMIT = 100_000
MAX_WINDOW_SECONDS = 86_400


@dataclass
class RateLimitResult:
    allowed: bool
    remaining: int
    retry_after: int = 0
    limit: int = 0
    reset_after: int = 0


class RateLimiterUnavailable(RuntimeError):
    """The limiter backend could not decide; protected routes must fail closed."""


class RateLimiter(Protocol):
    limit: int
    window_seconds: int

    def check(self, tenant_id: str, user_id: str, endpoint_class: str) -> RateLimitResult: ...


def validate_policy(limit: object, window_seconds: object) -> tuple[int, int]:
    if (
        isinstance(limit, bool)
        or isinstance(window_seconds, bool)
        or not isinstance(limit, int)
        or not isinstance(window_seconds, int)
        or not 1 <= limit <= MAX_LIMIT
        or not 1 <= window_seconds <= MAX_WINDOW_SECONDS
    ):
        raise ValueError("rate limit values must be positive and bounded")
    return limit, window_seconds


def _identity(tenant_id: str, user_id: str, endpoint_class: str) -> tuple[str, str, str]:
    if not tenant_id or not user_id or not endpoint_class:
        raise ValueError("authoritative rate-limit identity is required")
    return tenant_id, user_id, endpoint_class


class DeterministicRateLimiter:
    def __init__(self, limit: int = 60, window_seconds: int = 60) -> None:
        self.limit, self.window_seconds = validate_policy(limit, window_seconds)
        # Tuple keys: identifiers containing delimiters cannot collide.
        self._buckets: dict[tuple[str, str, str], tuple[float, int]] = {}

    def check(self, tenant_id: str, user_id: str, endpoint_class: str) -> RateLimitResult:
        key = _identity(tenant_id, user_id, endpoint_class)
        now = monotonic()
        started, count = self._buckets.get(key, (now, 0))
        if now - started >= self.window_seconds:
            started, count = now, 0
        reset_after = max(1, math.ceil(self.window_seconds - (now - started)))
        if count >= self.limit:
            return RateLimitResult(False, 0, reset_after, self.limit, reset_after)
        self._buckets[key] = (started, count + 1)
        return RateLimitResult(True, self.limit - count - 1, 0, self.limit, reset_after)


# Deny without incrementing once the limit is reached, so retry storms cannot inflate
# the counter or extend the window; heal a key that lost its TTL instead of keeping it.
_FIXED_WINDOW = """
local current = tonumber(redis.call('GET', KEYS[1]) or '0')
local limit = tonumber(ARGV[1])
local window = tonumber(ARGV[2])
local allowed = 0
if current < limit then
  current = redis.call('INCR', KEYS[1])
  allowed = 1
  if current == 1 then redis.call('PEXPIRE', KEYS[1], window) end
end
local ttl = redis.call('PTTL', KEYS[1])
if ttl < 0 then
  redis.call('PEXPIRE', KEYS[1], window)
  ttl = window
end
return {allowed, current, ttl}
"""


def rate_limit_key(namespace: str, tenant_id: str, user_id: str, endpoint_class: str) -> str:
    """Pseudonymized, collision-free key: no raw tenant or user identifier reaches Redis."""
    identity = json.dumps(_identity(tenant_id, user_id, endpoint_class), separators=(",", ":"))
    return f"{namespace}:{hashlib.sha256(identity.encode()).hexdigest()}"


class RedisRateLimiter:
    def __init__(
        self,
        url: str,
        *,
        limit: int = 60,
        window_seconds: int = 60,
        namespace: str = "ragops:rate-limit",
        timeout_seconds: float = 0.25,
        client: Any | None = None,
    ) -> None:
        self.limit, self.window_seconds = validate_policy(limit, window_seconds)
        if not namespace or not 0 < timeout_seconds <= 5:
            raise ValueError("rate limit namespace and a bounded timeout are required")
        self.namespace = namespace
        if client is None:
            if not url.startswith(("redis://", "rediss://")):
                raise ValueError("Redis URL must use redis:// or rediss://")
            import redis
            from redis.backoff import NoBackoff
            from redis.retry import Retry

            # One bounded attempt: redis-py 6 would otherwise retry 3 times per request.
            client = redis.Redis.from_url(
                url,
                socket_timeout=timeout_seconds,
                socket_connect_timeout=timeout_seconds,
                retry=Retry(NoBackoff(), 0),
            )
        self.client = client
        self._script = client.register_script(_FIXED_WINDOW)

    def check(self, tenant_id: str, user_id: str, endpoint_class: str) -> RateLimitResult:
        key = rate_limit_key(self.namespace, tenant_id, user_id, endpoint_class)
        try:
            allowed, count, ttl_ms = self._script(
                keys=[key], args=[self.limit, self.window_seconds * 1000]
            )
        except Exception as exc:  # redis-py raises backend-specific error classes
            raise RateLimiterUnavailable(type(exc).__name__) from None
        reset_after = max(1, math.ceil(int(ttl_ms) / 1000))
        if int(allowed) != 1:
            return RateLimitResult(False, 0, reset_after, self.limit, reset_after)
        return RateLimitResult(True, max(0, self.limit - int(count)), 0, self.limit, reset_after)
