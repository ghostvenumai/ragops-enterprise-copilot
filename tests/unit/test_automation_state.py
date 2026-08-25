from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

import pytest
from automation.diagnostics import classify_error
from automation.reports import portable_state, write_reports
from automation.retry import RetryPolicy
from automation.run_loop import _resume_state
from automation.state import (
    BuildStatus,
    ErrorCategory,
    LoopState,
    Phase,
    StateStore,
    next_phase,
)


def test_state_machine_allows_only_current_or_next_phase() -> None:
    state = LoopState(build_id="test-build")

    state.transition(Phase.PRECHECK)

    assert state.phase is Phase.PRECHECK
    with pytest.raises(ValueError, match="invalid phase transition"):
        state.transition(Phase.UNIT_TEST)


def test_phase_order_reaches_complete() -> None:
    phase = Phase.DISCOVER
    visited = {phase}
    while phase is not Phase.COMPLETE:
        phase = next_phase(phase)
        assert phase not in visited
        visited.add(phase)

    assert len(visited) == len(Phase)


def test_state_store_is_atomic_and_resumable(tmp_path: Path) -> None:
    path = tmp_path / "state" / "loop_state.json"
    store = StateStore(path)
    state = LoopState(build_id="test-build")
    state.record_success("Repository geprüft")

    store.save(state)
    restored = store.load()

    assert asdict(restored) == asdict(state)
    assert not path.with_name("loop_state.json.tmp").exists()
    assert json.loads(path.read_text(encoding="utf-8"))["build_id"] == "test-build"


def test_blocker_detail_and_retry_count_are_persisted() -> None:
    state = LoopState(build_id="test-build", current_phase=Phase.GENERATE_VOICE.value)

    state.record_blocker(ErrorCategory.EXTERNAL_CREDENTIAL_MISSING, "Schlüssel fehlt")
    attempts = state.record_failure(ErrorCategory.TTS_FAILURE, "temporärer TTS-Fehler")

    assert state.blocked_phases == [Phase.GENERATE_VOICE.value]
    assert state.blocker_details == {Phase.GENERATE_VOICE.value: "Schlüssel fehlt"}
    assert attempts == 1


def test_resume_clears_every_blocker_in_the_voice_rerun_range(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "loop-state.json")
    state = LoopState(
        build_id="blocked-build",
        current_phase=Phase.COMPLETE.value,
        status=BuildStatus.READY_EXCEPT_EXTERNAL_BLOCKER.value,
        blocked_phases=[
            Phase.GENERATE_VOICE.value,
            Phase.RENDER.value,
            Phase.VIDEO_QA.value,
        ],
        blocker_details={
            Phase.GENERATE_VOICE.value: "credential missing",
            Phase.RENDER.value: "voice unavailable",
            Phase.VIDEO_QA.value: "video unavailable",
        },
    )
    store.save(state)

    resumed = _resume_state(store)

    assert resumed.phase is Phase.VERIFY_NARRATION
    assert resumed.status == BuildStatus.RUNNING.value
    assert resumed.blocked_phases == []
    assert resumed.blocker_details == {}


def test_retry_policy_retries_only_transient_failures() -> None:
    policy = RetryPolicy(max_retries_per_state=3, max_global_iterations=30)

    assert policy.can_retry(ErrorCategory.NETWORK_FAILURE, attempts=1)
    assert not policy.can_retry(ErrorCategory.NETWORK_FAILURE, attempts=3)
    assert not policy.can_retry(ErrorCategory.SECURITY_BLOCK, attempts=1)


def test_diagnostics_classifies_security_and_application_errors() -> None:
    assert (
        classify_error(Phase.SECURITY_CHECK, RuntimeError("finding"))
        is ErrorCategory.SECURITY_BLOCK
    )
    assert (
        classify_error(Phase.APPLICATION_QA, RuntimeError("broken"))
        is ErrorCategory.APPLICATION_ERROR
    )


def test_reports_redact_external_paths_and_copy_evidence(tmp_path: Path) -> None:
    state = LoopState(build_id="test-build")
    state.artifacts = {
        "repository": str(Path(__file__).resolve()),
        "external": "/opt/private/demo.json",
    }

    markdown_path, json_path = write_reports(
        state,
        tmp_path / "dist",
        tmp_path / "evidence",
    )

    payload = portable_state(state)
    assert payload["artifacts"] == {
        "repository": "tests/unit/test_automation_state.py",
        "external": "external-path-redacted",
    }
    assert markdown_path.exists()
    assert json_path.exists()
    assert (tmp_path / "evidence/master-loop-report.json").exists()
    assert (tmp_path / "evidence/master-loop-release-report.md").exists()
