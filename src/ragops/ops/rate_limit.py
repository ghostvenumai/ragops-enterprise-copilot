from __future__ import annotations

from dataclasses import dataclass
from time import monotonic


@dataclass
class RateLimitResult:
    allowed: bool
    remaining: int
    retry_after: int = 0


class DeterministicRateLimiter:
    def __init__(self, limit: int = 60, window_seconds: int = 60) -> None:
        if limit < 1 or window_seconds < 1:
            raise ValueError("rate limit values must be positive")
        self.limit, self.window_seconds = limit, window_seconds
        self._buckets: dict[str, tuple[float, int]] = {}

    def check(self, tenant_id: str, user_id: str, endpoint_class: str) -> RateLimitResult:
        if not tenant_id or not user_id or not endpoint_class:
            raise ValueError("authoritative rate-limit identity is required")
        key = f"{tenant_id}:{user_id}:{endpoint_class}"
        now = monotonic()
        started, count = self._buckets.get(key, (now, 0))
        if now - started >= self.window_seconds:
            started, count = now, 0
        if count >= self.limit:
            return RateLimitResult(False, 0, max(1, int(self.window_seconds - (now - started))))
        self._buckets[key] = (started, count + 1)
        return RateLimitResult(True, self.limit - count - 1)
