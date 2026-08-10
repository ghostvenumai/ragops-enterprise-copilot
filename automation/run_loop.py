"""One-command persistent master loop for application and video production."""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import UTC, datetime

from video.config import REPO_ROOT, VideoConfig

from automation.diagnostics import classify_error
from automation.gates import GateResult, execute_phase
from automation.reports import print_summary, write_reports
from automation.retry import RetryPolicy
from automation.state import (
    BuildStatus,
    ErrorCategory,
    ExitCode,
    LoopState,
    Phase,
    StateStore,
    next_phase,
)

STATE_PATH = REPO_ROOT / "automation/state/loop_state.json"
DRY_RUN_STATE_PATH = REPO_ROOT / "automation/state/dry_run_state.json"
HISTORY_PATH = REPO_ROOT / "automation/state/history.jsonl"


def _new_state() -> LoopState:
    return LoopState(build_id=datetime.now(UTC).strftime("ragops-%Y%m%dT%H%M%SZ"))


def _save_event(state: LoopState, result: GateResult) -> None:
    HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    with HISTORY_PATH.open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                {
                    "build_id": state.build_id,
                    "iteration": state.global_iterations,
                    "phase": state.current_phase,
                    "status": result.status,
                    "detail": result.detail,
                },
                sort_keys=True,
            )
            + "\n"
        )


def run_dry_run() -> tuple[LoopState, ExitCode]:
    state = _new_state()
    store = StateStore(DRY_RUN_STATE_PATH)
    policy = RetryPolicy.from_env()
    config = VideoConfig.from_env()
    config.prepare_directories()
    phases = (Phase.DISCOVER, Phase.PRECHECK, Phase.PLAN, Phase.IMPLEMENT, Phase.DEMO_PRECHECK)
    for phase in phases:
        state.current_phase = phase.value
        result = execute_phase(phase, state, policy, config)
        state.global_iterations += 1
        state.record_success(result.detail)
        store.save(state)
    state.current_phase = Phase.COMPLETE.value
    state.status = BuildStatus.DRY_RUN_COMPLETE.value
    store.save(state)
    write_reports(state, config.dist_dir)
    print_summary(state)
    return state, ExitCode.SUCCESS


def _resume_state(store: StateStore) -> LoopState:
    state = store.load()
    if state.status == BuildStatus.READY_EXCEPT_EXTERNAL_BLOCKER.value and os.getenv(
        "OPENAI_API_KEY"
    ):
        voice_index = list(Phase).index(Phase.GENERATE_VOICE)
        rerun = {phase.value for phase in list(Phase)[voice_index:]}
        state.completed_phases = [item for item in state.completed_phases if item not in rerun]
        state.failed_phases = [item for item in state.failed_phases if item not in rerun]
        state.blocked_phases = [
            item for item in state.blocked_phases if item != Phase.GENERATE_VOICE.value
        ]
        state.blocker_details.pop(Phase.GENERATE_VOICE.value, None)
        state.current_phase = Phase.GENERATE_VOICE.value
        state.status = BuildStatus.RUNNING.value
    return state


def _failure_exit(phase: Phase, category: ErrorCategory) -> ExitCode:
    if category is ErrorCategory.SECURITY_BLOCK:
        return ExitCode.SECURITY_BLOCK
    if category is ErrorCategory.TEST_FAILURE:
        return ExitCode.TEST_FAILURE
    if phase in {Phase.RECORD, Phase.RENDER, Phase.VIDEO_QA}:
        return ExitCode.VIDEO_FAILURE
    return ExitCode.FAILURE


def run_master_loop(*, resume: bool = False) -> tuple[LoopState, ExitCode]:
    store = StateStore(STATE_PATH)
    state = _resume_state(store) if resume and STATE_PATH.exists() else _new_state()
    policy = RetryPolicy.from_env()
    config = VideoConfig.from_env()
    config.prepare_directories()
    state.status = BuildStatus.RUNNING.value
    store.save(state)
    while state.phase is not Phase.COMPLETE:
        if state.global_iterations >= policy.max_global_iterations:
            state.status = BuildStatus.FAILED.value
            state.last_error_category = ErrorCategory.CONFIGURATION_ERROR.value
            state.last_error = "maximale globale Iterationszahl erreicht"
            break
        phase = state.phase
        state.global_iterations += 1
        try:
            result = execute_phase(phase, state, policy, config)
            _save_event(state, result)
            if result.status == "blocked" and result.category:
                state.record_blocker(result.category, result.detail)
            else:
                state.record_success(result.detail)
            state.transition(next_phase(phase))
            store.save(state)
        except Exception as exc:
            category = classify_error(phase, exc)
            attempts = state.record_failure(category, f"{type(exc).__name__}: {exc}")
            _save_event(state, GateResult("failed", state.last_error, category))
            store.save(state)
            if policy.can_retry(category, attempts):
                continue
            state.status = BuildStatus.FAILED.value
            store.save(state)
            write_reports(state, config.dist_dir)
            print_summary(state)
            return state, _failure_exit(phase, category)
    if state.phase is Phase.COMPLETE:
        state.status = (
            BuildStatus.READY_EXCEPT_EXTERNAL_BLOCKER.value
            if state.blocked_phases
            else BuildStatus.COMPLETE.value
        )
    store.save(state)
    write_reports(state, config.dist_dir)
    print_summary(state)
    if state.status == BuildStatus.READY_EXCEPT_EXTERNAL_BLOCKER.value:
        return state, ExitCode.EXTERNAL_BLOCKER
    return (
        state,
        ExitCode.SUCCESS if state.status == BuildStatus.COMPLETE.value else ExitCode.FAILURE,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="RAGOps autonomous application/video loop")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args(argv)
    try:
        _, exit_code = run_dry_run() if args.dry_run else run_master_loop(resume=args.resume)
    except Exception as exc:
        print(f"MASTER LOOP FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
        return int(ExitCode.FAILURE)
    return int(exit_code)


if __name__ == "__main__":
    raise SystemExit(main())
