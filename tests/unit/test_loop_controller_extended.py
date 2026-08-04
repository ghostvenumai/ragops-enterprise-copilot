from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from loop import controller
from loop.controller import LoopLimits, Task


def patch_loop_paths(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    loop_dir = tmp_path / ".loop"
    monkeypatch.setattr(controller, "LOOP_DIR", loop_dir)
    monkeypatch.setattr(controller, "LOG_DIR", loop_dir / "logs")
    monkeypatch.setattr(controller, "STATE_PATH", loop_dir / "state.json")
    monkeypatch.setattr(controller, "HISTORY_PATH", loop_dir / "history.jsonl")
    monkeypatch.setattr(controller, "CURRENT_TASK_PATH", loop_dir / "current_task.json")
    monkeypatch.setattr(controller, "LAST_RESULT_PATH", loop_dir / "last_result.json")
    monkeypatch.setattr(controller, "BLOCKERS_PATH", loop_dir / "blockers.json")


def test_limits_parsers_and_prompt(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("MAX_ITERATIONS", "4")
    monkeypatch.setenv("MAX_CONSECUTIVE_FAILURES", "2")
    monkeypatch.setenv("MAX_NO_PROGRESS_ITERATIONS", "2")
    monkeypatch.setenv("ITERATION_TIMEOUT_SECONDS", "30")
    monkeypatch.setenv("MAX_LOG_BYTES", "100")
    monkeypatch.setenv("MAX_NEW_PROD_DEPENDENCIES", "1")
    limits = LoopLimits.from_env()
    assert limits.max_iterations == 4
    assert limits.max_log_bytes == 100

    assert controller.parse_inline_list("[]") == []
    assert controller.parse_inline_list("[one, 'two']") == ["one", "two"]
    assert controller.parse_inline_list("single") == ["single"]

    task_file = tmp_path / "TASKS.yaml"
    task_file.write_text(
        """
tasks:
  - id: T-1
    title: Synthetic task
    description: Bounded work
    priority: 2
    dependencies: []
    allowed_paths:
      - src/**
    acceptance_criteria:
      - Tests pass.
    status: pending
    attempts: 1
""".strip()
        + "\n",
        encoding="utf-8",
    )
    tasks = controller.load_tasks(task_file)
    assert len(tasks) == 1
    assert tasks[0].attempts == 1
    assert "Tests pass." in tasks[0].to_prompt()
    with pytest.raises(FileNotFoundError):
        controller.read_required_text(tmp_path / "missing")


def test_json_history_repository_and_command_helpers(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    patch_loop_paths(monkeypatch, tmp_path)
    target = tmp_path / "state.json"
    assert controller.load_json(target, {"empty": True}) == {"empty": True}
    controller.atomic_write_json(target, [{"ok": True}])
    assert controller.load_json(target, []) == [{"ok": True}]

    controller.append_history({"status": "synthetic"})
    history = controller.HISTORY_PATH.read_text(encoding="utf-8")
    assert json.loads(history)["status"] == "synthetic"

    assert controller.truncate_log("short", 100) == "short"
    assert controller.truncate_log("0123456789", 5).startswith("[log truncated]")
    assert controller._decode_subprocess_output(b"bytes") == "bytes"
    assert controller._decode_subprocess_output(None) == ""

    monkeypatch.setattr(controller, "REPO_ROOT", tmp_path)
    assert controller.check_repository()["ok"] is False
    (tmp_path / ".git").mkdir()
    repository = controller.check_repository()
    assert "returncode" in repository

    result = controller.run_command(
        [sys.executable, "-c", "print('synthetic')"],
        timeout_seconds=5,
        max_log_bytes=1000,
    )
    assert result["returncode"] == 0
    assert "synthetic" in result["stdout"]

    timeout = controller.run_command(
        [sys.executable, "-c", "import time; time.sleep(0.05)"],
        timeout_seconds=0,
        max_log_bytes=1000,
    )
    assert timeout["returncode"] == 124


def test_state_selection_and_stop_branches() -> None:
    done = Task(task_id="A", title="a", description="", priority=1, status="done")
    blocked = Task(
        task_id="B",
        title="b",
        description="",
        priority=2,
        dependencies=["A"],
        status="blocked",
    )
    assert controller.select_task([done, blocked]) is None
    assert controller.should_stop({"critical_security_finding": True}, LoopLimits()) == (
        "critical_security_finding"
    )
    assert controller.should_stop({}, LoopLimits()) is None

    state = controller.update_state_after_result(
        {"iteration": 2, "consecutive_failures": 1, "no_progress_iterations": 1},
        success=False,
        progress=False,
    )
    assert state["iteration"] == 3
    assert state["consecutive_failures"] == 2
    success = controller.update_state_after_result(state, success=True, progress=True)
    assert success["consecutive_failures"] == 0
    assert success["no_progress_iterations"] == 0


def test_iteration_dry_run_complete_blocked_and_repository_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    patch_loop_paths(monkeypatch, tmp_path)
    monkeypatch.setattr(controller, "check_repository", lambda: {"ok": True})
    monkeypatch.setattr(
        controller,
        "load_context",
        lambda: {"agents": "a", "spec": "s", "tasks_raw": "t", "state": {}},
    )
    task = Task(task_id="T-1", title="task", description="work", priority=1)
    monkeypatch.setattr(controller, "load_tasks", lambda: [task])
    dry_run = controller.run_iteration(dry_run=True)
    assert dry_run["status"] == "dry_run"
    assert controller.CURRENT_TASK_PATH.exists()

    monkeypatch.setattr(controller, "load_tasks", lambda: [])
    complete = controller.run_iteration()
    assert complete["status"] == "complete"

    monkeypatch.setattr(
        controller,
        "load_tasks",
        lambda: [
            Task(
                task_id="T-2",
                title="blocked",
                description="",
                priority=1,
                status="blocked",
            )
        ],
    )
    blocked = controller.run_iteration()
    assert blocked["status"] == "blocked"

    monkeypatch.setattr(controller, "check_repository", lambda: {"ok": False, "reason": "bad git"})
    failed = controller.run_iteration()
    assert failed["status"] == "blocked"
    assert controller.BLOCKERS_PATH.exists()


def test_iteration_stop_success_failure_status_and_main(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    patch_loop_paths(monkeypatch, tmp_path)
    controller.atomic_write_json(controller.STATE_PATH, {"iteration": 1})
    stopped = controller.run_iteration(limits=LoopLimits(max_iterations=1))
    assert stopped["status"] == "stopped"

    controller.atomic_write_json(controller.STATE_PATH, {"iteration": 0})
    monkeypatch.setattr(controller, "check_repository", lambda: {"ok": True})
    monkeypatch.setattr(controller, "load_context", lambda: {"spec": "synthetic"})
    task = Task(task_id="T-1", title="task", description="work", priority=1)
    monkeypatch.setattr(controller, "load_tasks", lambda: [task])
    monkeypatch.setattr(
        controller,
        "run_command",
        lambda command, timeout, max_log: {
            "command": command,
            "returncode": 0,
            "stdout": "",
            "stderr": "",
        },
    )
    success = controller.run_iteration()
    assert success["status"] == "success"
    assert controller.status()["last_result"]["status"] == "success"
    assert controller.main(["status"]) == 0

    monkeypatch.setattr(
        controller,
        "run_command",
        lambda command, timeout, max_log: {
            "command": command,
            "returncode": 1,
            "stdout": "",
            "stderr": "synthetic failure",
        },
    )
    failed = controller.run_iteration()
    assert failed["status"] == "failed"
    assert controller.BLOCKERS_PATH.exists()

    monkeypatch.setattr(controller, "run_iteration", lambda dry_run=False: {"status": "dry_run"})
    assert controller.main(["dry-run"]) == 0
