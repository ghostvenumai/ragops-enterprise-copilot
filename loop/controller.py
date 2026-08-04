"""Controlled, resumable Codex build loop.

The controller intentionally never executes shell snippets returned by a model.
It can run only hard-coded command lists from this module.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
LOOP_DIR = REPO_ROOT / ".loop"
LOG_DIR = LOOP_DIR / "logs"
STATE_PATH = LOOP_DIR / "state.json"
HISTORY_PATH = LOOP_DIR / "history.jsonl"
CURRENT_TASK_PATH = LOOP_DIR / "current_task.json"
LAST_RESULT_PATH = LOOP_DIR / "last_result.json"
BLOCKERS_PATH = LOOP_DIR / "blockers.json"

DEFAULT_MAX_ITERATIONS = 30
DEFAULT_MAX_CONSECUTIVE_FAILURES = 3
DEFAULT_MAX_NO_PROGRESS_ITERATIONS = 3
DEFAULT_TIMEOUT_SECONDS = 900
DEFAULT_MAX_LOG_BYTES = 1_000_000
DEFAULT_MAX_NEW_PROD_DEPS = 2


@dataclass(frozen=True)
class LoopLimits:
    max_iterations: int = DEFAULT_MAX_ITERATIONS
    max_consecutive_failures: int = DEFAULT_MAX_CONSECUTIVE_FAILURES
    max_no_progress_iterations: int = DEFAULT_MAX_NO_PROGRESS_ITERATIONS
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS
    max_log_bytes: int = DEFAULT_MAX_LOG_BYTES
    max_new_prod_dependencies: int = DEFAULT_MAX_NEW_PROD_DEPS

    @classmethod
    def from_env(cls) -> LoopLimits:
        return cls(
            max_iterations=int(os.getenv("MAX_ITERATIONS", DEFAULT_MAX_ITERATIONS)),
            max_consecutive_failures=int(
                os.getenv("MAX_CONSECUTIVE_FAILURES", DEFAULT_MAX_CONSECUTIVE_FAILURES)
            ),
            max_no_progress_iterations=int(
                os.getenv("MAX_NO_PROGRESS_ITERATIONS", DEFAULT_MAX_NO_PROGRESS_ITERATIONS)
            ),
            timeout_seconds=int(os.getenv("ITERATION_TIMEOUT_SECONDS", DEFAULT_TIMEOUT_SECONDS)),
            max_log_bytes=int(os.getenv("MAX_LOG_BYTES", DEFAULT_MAX_LOG_BYTES)),
            max_new_prod_dependencies=int(
                os.getenv("MAX_NEW_PROD_DEPENDENCIES", DEFAULT_MAX_NEW_PROD_DEPS)
            ),
        )


@dataclass
class Task:
    task_id: str
    title: str
    description: str
    priority: int
    dependencies: list[str] = field(default_factory=list)
    allowed_paths: list[str] = field(default_factory=list)
    acceptance_criteria: list[str] = field(default_factory=list)
    status: str = "pending"
    attempts: int = 0

    def to_prompt(self) -> str:
        criteria = "\n".join(f"- {item}" for item in self.acceptance_criteria)
        paths = "\n".join(f"- {item}" for item in self.allowed_paths)
        return (
            "Work inside this repository only.\n"
            f"Task: {self.task_id} - {self.title}\n"
            f"Description: {self.description}\n"
            "Acceptance criteria:\n"
            f"{criteria}\n"
            "Allowed paths:\n"
            f"{paths}\n"
            "Make the smallest complete change and do not bypass security or tests."
        )


def atomic_write_json(path: Path, payload: dict[str, Any] | list[Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    with NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as tmp:
        tmp.write(encoded)
        tmp.flush()
        os.fsync(tmp.fileno())
        tmp_path = Path(tmp.name)
    tmp_path.replace(path)


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def append_history(entry: dict[str, Any]) -> None:
    HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    with HISTORY_PATH.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, sort_keys=True) + "\n")


def read_required_text(path: Path) -> str:
    if not path.exists():
        label = path.relative_to(REPO_ROOT) if path.is_relative_to(REPO_ROOT) else path
        raise FileNotFoundError(f"Required loop input missing: {label}")
    return path.read_text(encoding="utf-8")


def parse_inline_list(value: str) -> list[str]:
    stripped = value.strip()
    if stripped == "[]":
        return []
    if stripped.startswith("[") and stripped.endswith("]"):
        inner = stripped[1:-1].strip()
        if not inner:
            return []
        return [part.strip().strip("'\"") for part in inner.split(",")]
    return [stripped.strip("'\"")]


def load_tasks(path: Path = REPO_ROOT / "TASKS.yaml") -> list[Task]:
    """Parse the repository task file.

    The project keeps the YAML deliberately simple. A small parser avoids adding
    PyYAML just for loop bootstrapping.
    """

    text = read_required_text(path)
    tasks: list[Task] = []
    current: dict[str, Any] | None = None
    active_list_key: str | None = None

    for raw_line in text.splitlines():
        if (
            not raw_line.strip()
            or raw_line.lstrip().startswith("#")
            or raw_line.strip() == "tasks:"
        ):
            continue
        if raw_line.startswith("  - id:"):
            if current:
                tasks.append(_task_from_mapping(current))
            current = {"id": raw_line.split(":", 1)[1].strip()}
            active_list_key = None
            continue
        if current is None:
            continue
        stripped = raw_line.strip()
        if raw_line.startswith("    ") and not raw_line.startswith("      - ") and ":" in stripped:
            key, value = stripped.split(":", 1)
            value = value.strip()
            active_list_key = None
            if value == "":
                current[key] = []
                active_list_key = key
            elif key in {
                "dependencies",
                "allowed_paths",
                "acceptance_criteria",
                "test_results",
                "blockers",
            }:
                current[key] = parse_inline_list(value)
            else:
                current[key] = value
            continue
        if raw_line.startswith("      - ") and active_list_key:
            current.setdefault(active_list_key, []).append(stripped[2:].strip())

    if current:
        tasks.append(_task_from_mapping(current))
    return tasks


def _task_from_mapping(data: dict[str, Any]) -> Task:
    return Task(
        task_id=str(data.get("id", "")),
        title=str(data.get("title", "")),
        description=str(data.get("description", "")),
        priority=int(data.get("priority", 999)),
        dependencies=list(data.get("dependencies", [])),
        allowed_paths=list(data.get("allowed_paths", [])),
        acceptance_criteria=list(data.get("acceptance_criteria", [])),
        status=str(data.get("status", "pending")),
        attempts=int(data.get("attempts", 0)),
    )


def check_repository() -> dict[str, Any]:
    git_dir = REPO_ROOT / ".git"
    if not git_dir.exists():
        return {"ok": False, "reason": "missing .git directory"}
    try:
        result = subprocess.run(  # noqa: S603 - fixed repository status command.
            ["git", "status", "--short"],  # noqa: S607 - git is resolved by PATH in dev envs.
            cwd=REPO_ROOT,
            check=False,
            text=True,
            capture_output=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return {"ok": False, "reason": f"git status failed: {exc}"}
    return {
        "ok": result.returncode == 0,
        "returncode": result.returncode,
        "stdout": result.stdout,
        "stderr": result.stderr,
    }


def load_context() -> dict[str, Any]:
    return {
        "agents": read_required_text(REPO_ROOT / "AGENTS.md"),
        "spec": read_required_text(REPO_ROOT / "SPEC.md"),
        "tasks_raw": read_required_text(REPO_ROOT / "TASKS.yaml"),
        "last_result": load_json(LAST_RESULT_PATH, {}),
        "state": load_json(STATE_PATH, {}),
    }


def select_task(tasks: list[Task], completed: set[str] | None = None) -> Task | None:
    completed = completed or {task.task_id for task in tasks if task.status == "done"}
    candidates = [
        task
        for task in tasks
        if task.status in {"in_progress", "pending"}
        and all(dependency in completed for dependency in task.dependencies)
    ]
    if not candidates:
        return None
    return sorted(candidates, key=lambda item: (item.priority, item.task_id))[0]


def build_codex_command(task: Task) -> list[str]:
    return [
        "codex",
        "exec",
        "--sandbox",
        "workspace-write",
        "--ask-for-approval",
        "never",
        task.to_prompt(),
    ]


def build_quality_commands() -> list[list[str]]:
    return [
        [sys.executable, "scripts/verify.py", "--loop"],
        [sys.executable, "scripts/review.py"],
    ]


def truncate_log(text: str, max_bytes: int) -> str:
    encoded = text.encode("utf-8", errors="replace")
    if len(encoded) <= max_bytes:
        return text
    suffix = encoded[-max_bytes:].decode("utf-8", errors="replace")
    return "[log truncated]\n" + suffix


def _decode_subprocess_output(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def run_command(command: list[str], timeout_seconds: int, max_log_bytes: int) -> dict[str, Any]:
    started = time.perf_counter()
    try:
        completed = subprocess.run(  # noqa: S603 - command lists are controller-defined.
            command,
            cwd=REPO_ROOT,
            text=True,
            capture_output=True,
            timeout=timeout_seconds,
            check=False,
        )
        duration = time.perf_counter() - started
        return {
            "command": command[:6],
            "returncode": completed.returncode,
            "duration_seconds": round(duration, 3),
            "stdout": truncate_log(completed.stdout, max_log_bytes),
            "stderr": truncate_log(completed.stderr, max_log_bytes),
        }
    except subprocess.TimeoutExpired as exc:
        return {
            "command": command[:6],
            "returncode": 124,
            "duration_seconds": round(time.perf_counter() - started, 3),
            "stdout": truncate_log(_decode_subprocess_output(exc.stdout), max_log_bytes),
            "stderr": truncate_log(
                _decode_subprocess_output(exc.stderr) or "timeout", max_log_bytes
            ),
        }


def should_stop(state: dict[str, Any], limits: LoopLimits) -> str | None:
    if state.get("iteration", 0) >= limits.max_iterations:
        return "max_iterations"
    if state.get("consecutive_failures", 0) >= limits.max_consecutive_failures:
        return "max_consecutive_failures"
    if state.get("no_progress_iterations", 0) >= limits.max_no_progress_iterations:
        return "max_no_progress_iterations"
    if state.get("critical_security_finding"):
        return "critical_security_finding"
    return None


def update_state_after_result(
    state: dict[str, Any], success: bool, progress: bool
) -> dict[str, Any]:
    next_state = dict(state)
    next_state["iteration"] = int(next_state.get("iteration", 0)) + 1
    next_state["updated_at"] = int(time.time())
    next_state["consecutive_failures"] = (
        0 if success else int(next_state.get("consecutive_failures", 0)) + 1
    )
    next_state["no_progress_iterations"] = (
        0 if progress else int(next_state.get("no_progress_iterations", 0)) + 1
    )
    next_state["last_success"] = success
    return next_state


def run_iteration(dry_run: bool = False, limits: LoopLimits | None = None) -> dict[str, Any]:
    limits = limits or LoopLimits.from_env()
    LOOP_DIR.mkdir(exist_ok=True)
    LOG_DIR.mkdir(exist_ok=True)

    state = load_json(
        STATE_PATH,
        {
            "iteration": 0,
            "consecutive_failures": 0,
            "no_progress_iterations": 0,
            "created_at": int(time.time()),
        },
    )
    stop_reason = should_stop(state, limits)
    if stop_reason:
        result = {"status": "stopped", "reason": stop_reason, "state": state}
        atomic_write_json(LAST_RESULT_PATH, result)
        return result

    repository = check_repository()
    if not repository.get("ok"):
        blocker = {
            "status": "blocked",
            "reason": repository.get("reason", "repository check failed"),
        }
        atomic_write_json(BLOCKERS_PATH, [blocker])
        atomic_write_json(LAST_RESULT_PATH, blocker)
        return blocker

    context = load_context()
    tasks = load_tasks()
    task = select_task(tasks)
    if task is None:
        blocked_tasks = [item.task_id for item in tasks if item.status == "blocked"]
        if blocked_tasks:
            blocked = {
                "status": "blocked",
                "reason": "blocked tasks remain",
                "blocked_tasks": blocked_tasks,
            }
            atomic_write_json(LAST_RESULT_PATH, blocked)
            return blocked
        complete = {"status": "complete", "reason": "no pending eligible tasks"}
        atomic_write_json(LAST_RESULT_PATH, complete)
        return complete

    atomic_write_json(CURRENT_TASK_PATH, task.__dict__)
    codex_command = build_codex_command(task)
    quality_commands = build_quality_commands()

    if dry_run:
        result = {
            "status": "dry_run",
            "task_id": task.task_id,
            "codex_command": codex_command[:6],
            "quality_commands": quality_commands,
            "loaded_context_bytes": {
                key: len(value) if isinstance(value, str) else len(json.dumps(value))
                for key, value in context.items()
            },
        }
        atomic_write_json(LAST_RESULT_PATH, result)
        append_history(result)
        return result

    command_results = [run_command(codex_command, limits.timeout_seconds, limits.max_log_bytes)]
    for quality_command in quality_commands:
        command_results.append(
            run_command(quality_command, limits.timeout_seconds, limits.max_log_bytes)
        )

    success = all(item["returncode"] == 0 for item in command_results)
    progress = success
    new_state = update_state_after_result(state, success=success, progress=progress)
    atomic_write_json(STATE_PATH, new_state)
    result = {
        "status": "success" if success else "failed",
        "task_id": task.task_id,
        "commands": command_results,
        "state": new_state,
    }
    atomic_write_json(LAST_RESULT_PATH, result)
    append_history(result)
    if not success:
        blockers = load_json(BLOCKERS_PATH, [])
        blockers.append(
            {
                "task_id": task.task_id,
                "iteration": new_state["iteration"],
                "reason": "one or more allowlisted loop commands failed",
            }
        )
        atomic_write_json(BLOCKERS_PATH, blockers)
    return result


def status() -> dict[str, Any]:
    return {
        "state": load_json(STATE_PATH, {}),
        "current_task": load_json(CURRENT_TASK_PATH, {}),
        "last_result": load_json(LAST_RESULT_PATH, {}),
        "blockers": load_json(BLOCKERS_PATH, []),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Controlled RAGOps Codex loop controller")
    parser.add_argument("command", choices=["run", "status", "dry-run"], nargs="?", default="run")
    args = parser.parse_args(argv)

    if args.command == "status":
        print(json.dumps(status(), indent=2, sort_keys=True))
        return 0
    result = run_iteration(dry_run=args.command == "dry-run")
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result.get("status") in {"success", "dry_run", "complete", "stopped"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
