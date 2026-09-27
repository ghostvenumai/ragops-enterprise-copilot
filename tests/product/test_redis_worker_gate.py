from __future__ import annotations

import json
import tomllib
from pathlib import Path
from types import SimpleNamespace

import scripts.redis_worker_gate as redis_gate
from scripts.redis_worker_gate import (
    bounded_ready_timeout,
    collect_startup_diagnostics,
    compose_up_command,
    docker_start_failure_status,
    host_redis_url,
    is_docker_service_url,
    parse_loopback_published_port,
    redis_client_available,
    sanitize_startup_logs,
    status_for_exit_code,
)


def test_docker_hostname_uses_localhost_endpoint() -> None:
    assert is_docker_service_url("redis://redis:6379/0") is True
    assert is_docker_service_url("redis://127.0.0.1:49321/0") is False
    assert host_redis_url(49321) == "redis://127.0.0.1:49321/0"
    assert host_redis_url(49322) == "redis://127.0.0.1:49322/0"


def test_compose_bootstrap_targets_only_ragops_redis(monkeypatch) -> None:
    monkeypatch.setattr("scripts.redis_worker_gate.shutil.which", lambda name: "/usr/bin/docker")
    command = compose_up_command()
    assert command is not None
    assert command[-1] == "rc-redis"
    assert "up" in command
    assert "--remove-orphans" not in command
    assert "down" not in command
    assert "-p" in command and "ragops-enterprise-copilot-rc" in command
    binding = '"127.0.0.1::6379"'
    config = Path("docker-compose.rc.yml").read_text()
    assert config.count(binding) == 1
    assert "6381" not in config
    assert "user: redis" in config
    assert "no-new-privileges:true" in config
    assert "cap_drop:" in config and "  - ALL" in config
    assert "privileged: true" not in config
    assert "volumes:" not in config
    assert "mode=1777" in config


def test_compose_unavailable_is_not_reused_or_fuzzy_matched(monkeypatch) -> None:
    monkeypatch.setattr("scripts.redis_worker_gate.shutil.which", lambda _name: None)
    assert compose_up_command() is None


def test_readiness_timeout_is_bounded() -> None:
    assert bounded_ready_timeout("0") == 1
    assert bounded_ready_timeout("999") == 120
    assert bounded_ready_timeout("bad") == 30


def test_dynamic_published_port_requires_loopback() -> None:
    assert parse_loopback_published_port("127.0.0.1:49321\n") == ("127.0.0.1", 49321)
    assert parse_loopback_published_port("0.0.0.0:49321\n") is None
    assert parse_loopback_published_port("localhost:49321\n") is None
    assert parse_loopback_published_port("127.0.0.1:99999\n") is None


def test_published_port_lookup_is_project_scoped(monkeypatch) -> None:
    monkeypatch.setattr("scripts.redis_worker_gate.shutil.which", lambda _name: "/usr/bin/docker")
    command = compose_up_command()
    assert command is not None
    calls: list[list[str]] = []

    def fake_run(args, **_kwargs):
        calls.append(args)
        return SimpleNamespace(returncode=0, stdout="127.0.0.1:49321\n", stderr="")

    monkeypatch.setattr(redis_gate.subprocess, "run", fake_run)
    endpoint, command_exit_code = redis_gate.published_endpoint(command)
    assert endpoint == ("127.0.0.1", 49321)
    assert command_exit_code == 0
    assert calls[0][-2:] == ["rc-redis", "6379"]
    assert "-p" in calls[0] and "ragops-enterprise-copilot-rc" in calls[0]


def test_startup_diagnostics_preserve_docker_exit_code_and_error(monkeypatch) -> None:
    monkeypatch.setattr("scripts.redis_worker_gate.shutil.which", lambda _name: "/usr/bin/docker")
    command = compose_up_command()
    assert command is not None

    def fake_run(args, **_kwargs):
        if "ps" in args:
            return SimpleNamespace(returncode=0, stdout="0123456789abcdef\n", stderr="")
        if "inspect" in args:
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps(
                    {
                        "Status": "created",
                        "ExitCode": 128,
                        "Error": "Bind for 127.0.0.1 failed: port is already allocated",
                    }
                ),
                stderr="",
            )
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(redis_gate.subprocess, "run", fake_run)
    diagnostics = collect_startup_diagnostics(command)
    gate_evidence = {"compose_command_exit_code": 1, **diagnostics}
    assert gate_evidence["compose_command_exit_code"] == 1
    assert gate_evidence["container_exit_code"] == 128
    assert gate_evidence["container_state"] == "created"
    assert "port is already allocated" in gate_evidence["sanitized_container_error"]


def test_gate_exit_classification() -> None:
    assert status_for_exit_code(0) == "PASS"
    assert status_for_exit_code(2) == "BLOCKED"
    assert status_for_exit_code(1) == "FAIL"
    assert docker_start_failure_status("Cannot connect to the Docker daemon") == "BLOCKED"
    assert docker_start_failure_status("port is already allocated") == "FAIL"


def test_redis_client_is_pinned_as_runtime_dependency(monkeypatch) -> None:
    project = tomllib.loads(Path("pyproject.toml").read_text())
    assert "redis==6.4.0" in project["project"]["dependencies"]
    assert "redis==6.4.0" in Path("constraints.txt").read_text()
    monkeypatch.setattr(
        "scripts.redis_worker_gate.importlib.util.find_spec", lambda _name: object()
    )
    assert redis_client_available() is True
    monkeypatch.setattr("scripts.redis_worker_gate.importlib.util.find_spec", lambda _name: None)
    assert redis_client_available() is False


def test_docker_worker_image_installs_runtime_extras() -> None:
    dockerfile = Path("Dockerfile").read_text()
    assert '.[persistence,identity,async,vector]' in dockerfile


class _FakeConnection:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def execute(self, _statement):
        return 1


class _FakeEngine:
    def connect(self):
        return _FakeConnection()

    def dispose(self):
        return None


def _prepare_gate_environment(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    worker_path = tmp_path / "src/ragops/workers/runtime.py"
    worker_path.parent.mkdir(parents=True)
    worker_path.touch()
    monkeypatch.setenv("RAGOPS_REDIS_URL", "redis://127.0.0.1:49321/0")
    monkeypatch.setenv(
        "RAGOPS_TEST_DATABASE_URL",
        "postgresql+psycopg://runner:secret@localhost/ragops_test_gate",
    )
    monkeypatch.setattr(redis_gate, "create_engine", lambda *_a, **_k: _FakeEngine())
    monkeypatch.setattr(redis_gate, "wait_for_redis", lambda *_a, **_k: True)
    monkeypatch.setattr(redis_gate, "_commit", lambda: "test-commit")


def test_gate_continues_to_migrations_when_redis_dependency_exists(
    monkeypatch, tmp_path: Path
) -> None:
    _prepare_gate_environment(monkeypatch, tmp_path)
    monkeypatch.setattr(redis_gate, "redis_client_available", lambda: True)
    commands: list[list[str]] = []

    def fake_run(command, **_kwargs):
        commands.append(command)
        stdout = ""
        if command[0].endswith("alembic") and command[1:] == ["current"]:
            stdout = "0006_finops_reservations (head)\n"
        elif command[0].endswith("alembic") and command[1:] == ["heads"]:
            stdout = "0006_finops_reservations (head)\n"
        elif "tests/integration/test_redis_worker_live.py" in command:
            stdout = 'REDIS_WORKER_RESULT {"tenant_leakage":0}\n'
        return SimpleNamespace(returncode=0, stdout=stdout, stderr="")

    monkeypatch.setattr(redis_gate.subprocess, "run", fake_run)
    assert redis_gate.main() == 0
    assert any(
        command[0].endswith("alembic") and command[1:] == ["upgrade", "head"]
        for command in commands
    )
    assert any("tests/integration/test_redis_worker_live.py" in command for command in commands)
    evidence = json.loads(
        (tmp_path / "evidence/product-v1/rc-live/redis-worker.json").read_text()
    )
    assert evidence["redis_client_available"] is True
    assert evidence["redis_reachable"] is True
    assert evidence["migration_status"] == "PASS"
    assert evidence["live_test_status"] == "PASS"


def test_missing_redis_dependency_is_blocked_without_bootstrap(monkeypatch, tmp_path: Path) -> None:
    _prepare_gate_environment(monkeypatch, tmp_path)
    monkeypatch.setattr(redis_gate, "redis_client_available", lambda: False)
    monkeypatch.setattr(
        redis_gate.subprocess,
        "run",
        lambda *_a, **_k: SimpleNamespace(returncode=0, stdout="", stderr=""),
    )
    assert redis_gate.main() == 2
    evidence = json.loads(
        (tmp_path / "evidence/product-v1/rc-live/redis-worker.json").read_text()
    )
    assert evidence["status"] == "BLOCKED"
    assert evidence["reason"] == "Redis client package is unavailable"
    assert evidence["redis_bootstrap_status"] == "NOT_RUN"
    assert evidence["live_test_status"] == "NOT_RUN"


def test_startup_failure_summary_is_actionable_and_redacted() -> None:
    summary = sanitize_startup_logs(
        "setpriv: setresuid failed: Operation not permitted\n"
        "ERROR redis://user:secret@localhost:6379 password=hidden"
    )
    assert summary is not None
    assert "setpriv" in summary
    assert "Operation not permitted" in summary
    assert "secret" not in summary
    assert "hidden" not in summary
    assert "[redacted-url]" in summary


def test_live_gate_uses_real_redis_queue_and_worker() -> None:
    source = Path("tests/integration/test_redis_worker_live.py").read_text()
    assert "RedisIngestionQueue" in source
    assert "IngestionWorker" in source
    assert "InMemoryIngestionQueue" not in source
