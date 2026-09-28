"""Readiness aggregation: bounded probes, critical vs unconfigured, stable sanitized reasons."""

from __future__ import annotations

import json
import time

import pytest

from ragops.ops.readiness import DependencyCheck, ReadinessService


def ok() -> None:
    return None


def refused() -> None:
    raise ConnectionError("connect to postgresql://user:secret@10.0.0.5:5432/db refused")


def hangs() -> None:
    time.sleep(3)


def test_all_critical_dependencies_up_is_ready() -> None:
    service = ReadinessService(
        [DependencyCheck("postgres", True, ok), DependencyCheck("redis", True, ok)]
    )
    status, payload = service.evaluate()
    assert status == 200 and payload["status"] == "ready"
    assert payload["components"]["postgres"] == {"critical": True, "status": "up", "reason": "ok"}


def test_critical_dependency_down_is_unready_with_sanitized_reason() -> None:
    service = ReadinessService([DependencyCheck("postgres", True, refused)])
    status, payload = service.evaluate()
    assert status == 503 and payload["status"] == "unready"
    assert payload["components"]["postgres"]["reason"] == "unreachable"
    serialized = json.dumps(payload)
    for secret in ("secret", "10.0.0.5", "postgresql://", "refused"):
        assert secret not in serialized


def test_unconfigured_dependency_is_reported_but_not_blocking() -> None:
    service = ReadinessService(
        [DependencyCheck("qdrant", False, None), DependencyCheck("postgres", True, ok)]
    )
    status, payload = service.evaluate()
    assert status == 200
    assert payload["components"]["qdrant"] == {
        "critical": False,
        "status": "not_configured",
        "reason": "not_configured",
    }


def test_hanging_probe_is_bounded_by_the_readiness_timeout() -> None:
    service = ReadinessService([DependencyCheck("qdrant", True, hangs)], timeout_seconds=0.2)
    started = time.monotonic()
    status, payload = service.evaluate()
    assert time.monotonic() - started < 1.0
    assert status == 503 and payload["components"]["qdrant"]["reason"] == "timeout"


@pytest.mark.parametrize(
    ("error", "reason"),
    [
        (TimeoutError(), "timeout"),
        (ConnectionRefusedError(), "unreachable"),
        (RuntimeError("x"), "error"),
    ],
)
def test_reason_codes_are_stable(error, reason) -> None:
    def probe() -> None:
        raise error

    _, payload = ReadinessService([DependencyCheck("redis", True, probe)]).evaluate()
    assert payload["components"]["redis"]["reason"] == reason


def test_shutdown_turns_unready_first() -> None:
    service = ReadinessService([DependencyCheck("postgres", True, ok)])
    service.begin_shutdown()
    status, payload = service.evaluate()
    assert status == 503 and payload["status"] == "shutting_down"


def test_timeout_bounds_are_validated() -> None:
    with pytest.raises(ValueError):
        ReadinessService([], timeout_seconds=0)
    with pytest.raises(ValueError):
        ReadinessService([], timeout_seconds=60)
