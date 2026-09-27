"""Run the real Redis + PostgreSQL worker gate and emit sanitized evidence."""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

ROOT = Path(__file__).resolve().parents[1]
COMPOSE_PROJECT = "ragops-enterprise-copilot-rc"
QUEUE_PREFIX = "ragops-rc-ingestion"
COMPOSE_SERVICE = "rc-redis"


def is_docker_service_url(redis_url: str) -> bool:
    try:
        return make_url(redis_url).host == "redis"
    except Exception:  # noqa: BLE001
        return False


def compose_up_command() -> list[str] | None:
    docker = shutil.which("docker")
    standalone = shutil.which("docker-compose")
    if docker:
        command = [docker, "compose"]
    elif standalone:
        command = [standalone]
    else:
        return None
    return [
        *command,
        "-p",
        COMPOSE_PROJECT,
        "-f",
        "docker-compose.yml",
        "-f",
        "docker-compose.rc.yml",
        "up",
        "-d",
        "--no-deps",
        COMPOSE_SERVICE,
    ]


def host_redis_url(host_port: int) -> str:
    return f"redis://127.0.0.1:{host_port}/0"


def parse_loopback_published_port(output: str) -> tuple[str, int] | None:
    endpoints = [line.strip() for line in output.splitlines() if line.strip()]
    if len(endpoints) != 1:
        return None
    match = re.fullmatch(r"127\.0\.0\.1:(\d{1,5})", endpoints[0])
    if match is None:
        return None
    port = int(match.group(1))
    if not 1 <= port <= 65535:
        return None
    return "127.0.0.1", port


def bounded_ready_timeout(raw: str | None) -> int:
    try:
        requested = int(raw or "30")
    except ValueError:
        requested = 30
    return min(max(requested, 1), 120)


def status_for_exit_code(exit_code: int) -> str:
    return "PASS" if exit_code == 0 else "BLOCKED" if exit_code == 2 else "FAIL"


def redis_client_available() -> bool:
    return importlib.util.find_spec("redis") is not None


def docker_start_failure_status(output: str) -> str:
    unavailable_markers = (
        "cannot connect to the docker daemon",
        "is the docker daemon running",
        "error during connect",
    )
    lowered = output.lower()
    return "BLOCKED" if any(marker in lowered for marker in unavailable_markers) else "FAIL"


def wait_for_redis(redis_url: str, timeout_seconds: int) -> bool:
    try:
        from redis import Redis
    except ImportError:
        return False
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        client = None
        try:
            client = Redis.from_url(redis_url, socket_connect_timeout=1, socket_timeout=1)
            if client.ping():
                return True
        except Exception:  # noqa: BLE001
            time.sleep(min(0.5, max(0, deadline - time.monotonic())))
        finally:
            if client is not None:
                client.close()
    return False


def main() -> int:
    redis_url = os.getenv("RAGOPS_REDIS_URL", "")
    database_url = os.getenv("RAGOPS_TEST_DATABASE_URL", "")
    queue_name = f"{QUEUE_PREFIX}-{uuid4().hex[:12]}"
    evidence: dict[str, Any] = {
        "gate": "redis_worker",
        "tested_commit": _commit(),
        "timestamp": datetime.now(UTC).isoformat(),
        "redis_backend": "unknown",
        "redis_endpoint_kind": "unknown",
        "redis_reachable": False,
        "compose_service": COMPOSE_SERVICE,
        "redis_bootstrap_status": "NOT_RUN",
        "compose_command_exit_code": None,
        "published_port_command_exit_code": None,
        "container_state": None,
        "container_exit_code": None,
        "sanitized_container_error": None,
        "sanitized_startup_log_summary": None,
        "published_host": None,
        "published_port": None,
        "redis_url_configured": bool(redis_url),
        "redis_client_available": redis_client_available(),
        "postgres_url_configured": bool(database_url),
        "postgres_isolated_database": False,
        "postgres_reachable": False,
        "worker_entrypoint_available": Path("src/ragops/workers/runtime.py").is_file(),
        "migration_status": "NOT_RUN",
        "live_test_status": "NOT_RUN",
        "transition_sequence": None,
        "enqueue_count": None,
        "claim_count": None,
        "retry_attempt_count": None,
        "duplicate_delivery": None,
        "cancellation": None,
        "terminal_status": None,
        "tenant_leakage": None,
        "queue_name": queue_name,
        "worker_mode": "bounded-pytest-integration",
        "test_command": ".venv/bin/pytest tests/integration/test_redis_worker_live.py -q -s",
    }
    if not evidence["worker_entrypoint_available"]:
        return _finish(evidence, "FAIL", "worker entrypoint is unavailable", 1)
    try:
        db = make_url(database_url)
        if db.drivername != "postgresql+psycopg" or not (db.database or "").startswith(
            "ragops_test_"
        ):
            return _finish(evidence, "BLOCKED", "isolated ragops_test_* PostgreSQL URL required", 2)
        evidence["postgres_isolated_database"] = True
    except Exception:  # noqa: BLE001
        return _finish(evidence, "BLOCKED", "PostgreSQL integration URL unavailable", 2)
    try:
        engine = create_engine(db, hide_parameters=True, connect_args={"connect_timeout": 3})
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        engine.dispose()
        evidence["postgres_reachable"] = True
    except Exception:  # noqa: BLE001
        return _finish(evidence, "BLOCKED", "isolated PostgreSQL database is unreachable", 2)
    if not evidence["redis_client_available"]:
        return _finish(evidence, "BLOCKED", "Redis client package is unavailable", 2)
    if not redis_url or not redis_url.startswith(("redis://", "rediss://")):
        return _finish(evidence, "BLOCKED", "Redis URL unavailable or invalid", 2)
    if not is_docker_service_url(redis_url) and wait_for_redis(redis_url, 2):
        effective_redis_url = redis_url
        evidence["redis_reachable"] = True
        evidence["redis_endpoint_kind"] = "configured_direct"
        evidence["redis_backend"] = "redis"
        evidence["redis_bootstrap_status"] = "NOT_REQUIRED"
    elif is_docker_service_url(redis_url):
        command = compose_up_command()
        if command is None:
            return _finish(evidence, "BLOCKED", "Docker Compose is unavailable", 2)
        evidence["redis_bootstrap_status"] = "STARTING"
        try:
            started = subprocess.run(  # noqa: S603
                command,
                cwd=ROOT,
                capture_output=True,
                text=True,
                check=False,
            )
        except OSError:
            return _finish(evidence, "BLOCKED", "Docker Compose could not be started", 2)
        evidence["compose_command_exit_code"] = started.returncode
        if started.returncode != 0:
            status = docker_start_failure_status(started.stderr or started.stdout)
            if status == "BLOCKED":
                evidence["redis_bootstrap_status"] = "BLOCKED"
                return _finish(evidence, status, "Docker daemon is unavailable", 2)
            evidence["redis_bootstrap_status"] = "FAIL"
            evidence.update(collect_startup_diagnostics(command))
            return _finish(
                evidence, "FAIL", "RAGOps RC Redis service failed to start", started.returncode
            )
        evidence["compose_command_exit_code"] = 0
        endpoint, port_command_exit_code = published_endpoint(command)
        evidence["published_port_command_exit_code"] = port_command_exit_code
        if endpoint is None:
            evidence["redis_bootstrap_status"] = "FAIL"
            evidence.update(collect_startup_diagnostics(command))
            return _finish(evidence, "FAIL", "RC Redis did not publish a loopback endpoint", 1)
        published_host, published_port = endpoint
        evidence["published_host"] = published_host
        evidence["published_port"] = published_port
        effective_redis_url = host_redis_url(published_port)
        evidence["redis_endpoint_kind"] = "dynamic_loopback"
        timeout_seconds = bounded_ready_timeout(os.getenv("RAGOPS_RC_REDIS_READY_TIMEOUT_SECONDS"))
        if not wait_for_redis(effective_redis_url, timeout_seconds):
            evidence["redis_bootstrap_status"] = "FAIL"
            diagnostics = collect_startup_diagnostics(command)
            evidence.update(diagnostics)
            return _finish(evidence, "FAIL", "RAGOps RC Redis did not become reachable", 1)
        evidence["redis_reachable"] = True
        evidence["redis_backend"] = "redis"
        evidence["redis_bootstrap_status"] = "PASS"
    else:
        return _finish(evidence, "BLOCKED", "Configured Redis endpoint is unreachable", 2)
    environment = os.environ.copy()
    environment["RAGOPS_DATABASE_URL"] = database_url
    environment["RAGOPS_REDIS_URL"] = effective_redis_url
    environment["RAGOPS_RC_QUEUE_NAME"] = queue_name
    migration = subprocess.run(  # noqa: S603
        [".venv/bin/alembic", "upgrade", "head"],
        capture_output=True,
        text=True,
        check=False,
        env=environment,
    )
    if migration.returncode != 0:
        evidence["migration_status"] = "FAIL"
        evidence["migration_exit_code"] = migration.returncode
        return _finish(evidence, "FAIL", "Alembic migration failed", migration.returncode)
    current = subprocess.run(  # noqa: S603
        [".venv/bin/alembic", "current"],
        capture_output=True,
        text=True,
        check=False,
        env=environment,
        cwd=ROOT,
    )
    heads = subprocess.run(  # noqa: S603
        [".venv/bin/alembic", "heads"],
        capture_output=True,
        text=True,
        check=False,
        cwd=ROOT,
    )
    current_revisions = {line.split()[0] for line in current.stdout.splitlines() if line.strip()}
    head_revisions = {line.split()[0] for line in heads.stdout.splitlines() if line.strip()}
    if current.returncode or heads.returncode or not head_revisions.issubset(current_revisions):
        evidence["migration_status"] = "FAIL"
        evidence["migration_exit_code"] = 1
        return _finish(evidence, "FAIL", "Alembic revision is not at head", 1)
    evidence["migration_status"] = "PASS"
    evidence["migration_exit_code"] = 0
    result = subprocess.run(  # noqa: S603
        [".venv/bin/pytest", "tests/integration/test_redis_worker_live.py", "-q", "-s"],
        capture_output=True,
        text=True,
        check=False,
        env=environment,
    )
    combined = result.stdout + result.stderr
    for line in combined.splitlines():
        if line.startswith("REDIS_WORKER_RESULT "):
            try:
                evidence.update(json.loads(line.removeprefix("REDIS_WORKER_RESULT ")))
            except json.JSONDecodeError:
                pass
    evidence["status"] = "PASS" if result.returncode == 0 else "FAIL"
    evidence["gate_exit_code"] = result.returncode
    evidence["integration_test_exit_code"] = result.returncode
    if result.returncode != 0:
        evidence["reason"] = "Redis/worker integration assertion failed"
        evidence["live_test_status"] = "FAIL"
    else:
        evidence["live_test_status"] = "PASS"
    evidence["detail"] = (
        "Integration assertions passed"
        if result.returncode == 0
        else "Integration failed; secret-bearing diagnostics omitted from evidence"
    )
    _write(evidence)
    return 0 if result.returncode == 0 else 1


def _finish(evidence: dict[str, Any], status: str, reason: str, exit_code: int) -> int:
    evidence.update({"status": status, "reason": reason, "gate_exit_code": exit_code})
    _write(evidence)
    return exit_code


def collect_startup_diagnostics(command: list[str]) -> dict[str, Any]:
    """Read only the exact RC service state/log tail; never enumerate containers."""
    try:
        up_index = command.index("up")
    except ValueError:
        return {
            "container_state": "unknown",
            "container_exit_code": None,
            "sanitized_container_error": None,
            "sanitized_startup_log_summary": None,
        }
    compose = command[:up_index]
    state = "unknown"
    container_exit_code: int | None = None
    ps = subprocess.run(  # noqa: S603
        [*compose, "ps", "-a", "-q", COMPOSE_SERVICE],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if ps.returncode == 0:
        container_id = ps.stdout.strip().splitlines()
        if len(container_id) == 1 and re.fullmatch(r"[0-9a-fA-F]{12,64}", container_id[0]):
            docker_cli = shutil.which("docker")
            inspected = subprocess.run(  # noqa: S603
                [docker_cli or "docker", "inspect", "--format", "{{json .State}}", container_id[0]],
                cwd=ROOT,
                capture_output=True,
                text=True,
                check=False,
            )
            if inspected.returncode == 0:
                try:
                    payload = json.loads(inspected.stdout)
                    state = str(payload.get("Status", "unknown"))
                    code = payload.get("ExitCode")
                    container_exit_code = int(code) if code is not None else None
                    container_error = sanitize_text(str(payload.get("Error", ""))) or None
                except (json.JSONDecodeError, TypeError, ValueError):
                    state = "unparsed"
                    container_error = None
            else:
                container_error = None
        else:
            container_error = None
    else:
        container_error = None
    logs = subprocess.run(  # noqa: S603
        [*compose, "logs", "--tail", "20", COMPOSE_SERVICE],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    return {
        "container_state": state,
        "container_exit_code": container_exit_code,
        "sanitized_container_error": container_error,
        "sanitized_startup_log_summary": sanitize_startup_logs(logs.stdout + logs.stderr),
    }


def published_endpoint(command: list[str]) -> tuple[tuple[str, int] | None, int]:
    try:
        up_index = command.index("up")
    except ValueError:
        return None, 1
    compose = command[:up_index]
    result = subprocess.run(  # noqa: S603
        [*compose, "port", COMPOSE_SERVICE, "6379"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return None, result.returncode
    return parse_loopback_published_port(result.stdout), result.returncode


def sanitize_text(value: str) -> str:
    safe = re.sub(r"(?i)\b[a-z][a-z0-9+.-]*://[^\s]+", "[redacted-url]", value)
    safe = re.sub(r"(?i)(password|passwd|token|secret)=\S+", r"\1=[redacted]", safe)
    return safe[:300]


def sanitize_startup_logs(output: str) -> str | None:
    relevant = (
        "setpriv:",
        "error",
        "failed",
        "operation not permitted",
        "permission denied",
        "exited",
    )
    lines = [
        line.strip()
        for line in output.splitlines()
        if any(marker in line.lower() for marker in relevant)
    ]
    if not lines:
        return None
    return sanitize_text(" | ".join(lines[-5:]))[:600]


def _commit() -> str:
    result = subprocess.run(  # noqa: S603
        ["git", "rev-parse", "HEAD"],  # noqa: S607
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout.strip() or "unknown"


def _write(evidence: dict[str, Any]) -> None:
    path = Path("evidence/product-v1/rc-live/redis-worker.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(evidence, indent=2), encoding="utf-8")


if __name__ == "__main__":
    sys.exit(main())
