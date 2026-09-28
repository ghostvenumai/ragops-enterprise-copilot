"""Truthful liveness/readiness: bounded dependency probes with stable, non-secret reasons.

Liveness (/health) never touches dependencies. Readiness (/ready) probes every configured
dependency in parallel under one deadline and reports 503 when a critical dependency is
unavailable or the instance is shutting down. Probes reuse the application's own engine and
clients, so a recovered dependency is observed without restarting the process.
"""

from __future__ import annotations

import concurrent.futures
from collections.abc import Callable
from dataclasses import dataclass
from threading import Event
from typing import Any

UP, DOWN, NOT_CONFIGURED = "up", "down", "not_configured"


@dataclass(frozen=True)
class DependencyCheck:
    name: str
    critical: bool
    probe: Callable[[], None] | None  # None: dependency is not configured


def _reason(exc: BaseException) -> str:
    """Stable reason codes; never the message, host, URL or stack."""
    name = type(exc).__name__.lower()
    if isinstance(exc, TimeoutError) or "timeout" in name:
        return "timeout"
    if isinstance(exc, ConnectionError) or any(
        part in name for part in ("connection", "operational", "interface", "connect")
    ):
        return "unreachable"
    return "error"


class ReadinessService:
    def __init__(self, checks: list[DependencyCheck], timeout_seconds: float = 2.0) -> None:
        if not 0 < timeout_seconds <= 10:
            raise ValueError("readiness timeout must be between 0 and 10 seconds")
        self.checks, self.timeout_seconds = checks, timeout_seconds
        self.shutting_down = Event()
        # Probe threads are daemonic; a blackholed probe can outlive one /ready call but
        # never blocks it, and the pool bounds how many can pile up.
        self._pool = concurrent.futures.ThreadPoolExecutor(
            max_workers=max(1, 2 * len(checks)), thread_name_prefix="readiness"
        )

    def evaluate(self) -> tuple[int, dict[str, Any]]:
        futures = {
            check.name: self._pool.submit(check.probe)
            for check in self.checks
            if check.probe is not None
        }
        components: dict[str, dict[str, Any]] = {}
        done, _pending = concurrent.futures.wait(futures.values(), timeout=self.timeout_seconds)
        for check in self.checks:
            state: dict[str, Any] = {"critical": check.critical}
            future = futures.get(check.name)
            if future is None:
                state.update(status=NOT_CONFIGURED, reason="not_configured")
            elif future not in done:
                state.update(status=DOWN, reason="timeout")
            elif future.exception() is not None:
                state.update(status=DOWN, reason=_reason(future.exception()))  # type: ignore[arg-type]
            else:
                state.update(status=UP, reason="ok")
            components[check.name] = state
        blocking = [
            name
            for name, state in components.items()
            if state["critical"] and state["status"] != UP
        ]
        if self.shutting_down.is_set():
            return 503, {"status": "shutting_down", "components": components}
        if blocking:
            return 503, {"status": "unready", "components": components}
        return 200, {"status": "ready", "components": components}

    def begin_shutdown(self) -> None:
        self.shutting_down.set()

    def close(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)


def postgres_probe(engine_factory: Callable[[], Any]) -> Callable[[], None]:
    from sqlalchemy import text

    def probe() -> None:
        with engine_factory().connect() as connection:
            connection.execute(text("SELECT 1"))

    return probe


def redis_probe(client_factory: Callable[[], Any]) -> Callable[[], None]:
    def probe() -> None:
        if not client_factory().ping():
            raise ConnectionError("redis ping failed")

    return probe


def qdrant_probe(client_factory: Callable[[], Any], collection: str) -> Callable[[], None]:
    def probe() -> None:
        client_factory().collection_exists(collection)

    return probe
