from __future__ import annotations

import json
from pathlib import Path

from loop.controller import (
    LoopLimits,
    Task,
    atomic_write_json,
    build_codex_command,
    select_task,
    should_stop,
)


def test_atomic_write_json_replaces_complete_document(tmp_path: Path) -> None:
    target = tmp_path / "state.json"
    atomic_write_json(target, {"iteration": 1, "ok": True})

    assert json.loads(target.read_text(encoding="utf-8")) == {"iteration": 1, "ok": True}


def test_select_task_prefers_in_progress_then_priority_order() -> None:
    tasks = [
        Task(task_id="P2", title="two", description="", priority=2, status="pending"),
        Task(task_id="P1", title="one", description="", priority=1, status="pending"),
    ]

    selected = select_task(tasks)

    assert selected is not None
    assert selected.task_id == "P1"


def test_select_task_respects_dependencies() -> None:
    tasks = [
        Task(
            task_id="P2",
            title="blocked",
            description="",
            priority=1,
            dependencies=["P1"],
            status="pending",
        ),
        Task(task_id="P1", title="base", description="", priority=2, status="pending"),
    ]

    selected = select_task(tasks)

    assert selected is not None
    assert selected.task_id == "P1"


def test_codex_command_is_fixed_and_noninteractive() -> None:
    task = Task(task_id="T1", title="title", description="desc", priority=1)

    command = build_codex_command(task)

    assert command[:6] == [
        "codex",
        "exec",
        "--sandbox",
        "workspace-write",
        "--ask-for-approval",
        "never",
    ]
    assert len(command) == 7
    assert "desc" in command[-1]


def test_stop_limits_are_enforced() -> None:
    limits = LoopLimits(
        max_iterations=3,
        max_consecutive_failures=2,
        max_no_progress_iterations=2,
    )

    assert should_stop({"iteration": 3}, limits) == "max_iterations"
    assert (
        should_stop({"iteration": 1, "consecutive_failures": 2}, limits)
        == "max_consecutive_failures"
    )
    assert (
        should_stop(
            {"iteration": 1, "consecutive_failures": 0, "no_progress_iterations": 2},
            limits,
        )
        == "max_no_progress_iterations"
    )
