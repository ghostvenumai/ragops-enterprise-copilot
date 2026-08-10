"""Typed state machine and atomic persistence for the master loop."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from enum import IntEnum, StrEnum
from pathlib import Path


class Phase(StrEnum):
    DISCOVER = "DISCOVER"
    PRECHECK = "PRECHECK"
    PLAN = "PLAN"
    IMPLEMENT = "IMPLEMENT"
    STATIC_CHECK = "STATIC_CHECK"
    UNIT_TEST = "UNIT_TEST"
    INTEGRATION_TEST = "INTEGRATION_TEST"
    SECURITY_CHECK = "SECURITY_CHECK"
    APPLICATION_QA = "APPLICATION_QA"
    DEMO_PRECHECK = "DEMO_PRECHECK"
    DEMO_RUN = "DEMO_RUN"
    RECORD = "RECORD"
    GENERATE_NARRATION = "GENERATE_NARRATION"
    GENERATE_VOICE = "GENERATE_VOICE"
    GENERATE_SUBTITLES = "GENERATE_SUBTITLES"
    RENDER = "RENDER"
    VIDEO_QA = "VIDEO_QA"
    FINAL_VERIFY = "FINAL_VERIFY"
    COMPLETE = "COMPLETE"


PHASE_ORDER = tuple(Phase)


class BuildStatus(StrEnum):
    RUNNING = "RUNNING"
    FAILED = "FAILED"
    COMPLETE = "COMPLETE"
    READY_EXCEPT_EXTERNAL_BLOCKER = "READY_EXCEPT_EXTERNAL_BLOCKER"
    DRY_RUN_COMPLETE = "DRY_RUN_COMPLETE"


class ErrorCategory(StrEnum):
    APPLICATION_ERROR = "APPLICATION_ERROR"
    TEST_FAILURE = "TEST_FAILURE"
    DEPENDENCY_MISSING = "DEPENDENCY_MISSING"
    DISPLAY_UNAVAILABLE = "DISPLAY_UNAVAILABLE"
    RECORDING_FAILURE = "RECORDING_FAILURE"
    TTS_FAILURE = "TTS_FAILURE"
    NETWORK_FAILURE = "NETWORK_FAILURE"
    RENDER_FAILURE = "RENDER_FAILURE"
    VIDEO_QA_FAILURE = "VIDEO_QA_FAILURE"
    EXTERNAL_CREDENTIAL_MISSING = "EXTERNAL_CREDENTIAL_MISSING"
    SECURITY_BLOCK = "SECURITY_BLOCK"
    CONFIGURATION_ERROR = "CONFIGURATION_ERROR"


class ExitCode(IntEnum):
    SUCCESS = 0
    FAILURE = 1
    CONFIGURATION_ERROR = 2
    TEST_FAILURE = 3
    SECURITY_BLOCK = 4
    VIDEO_FAILURE = 5
    EXTERNAL_BLOCKER = 10


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def next_phase(phase: Phase) -> Phase:
    index = PHASE_ORDER.index(phase)
    return PHASE_ORDER[min(index + 1, len(PHASE_ORDER) - 1)]


def is_allowed_transition(current: Phase, target: Phase) -> bool:
    return target == current or target == next_phase(current)


@dataclass
class LoopState:
    build_id: str
    current_phase: str = Phase.DISCOVER.value
    status: str = BuildStatus.RUNNING.value
    completed_phases: list[str] = field(default_factory=list)
    failed_phases: list[str] = field(default_factory=list)
    blocked_phases: list[str] = field(default_factory=list)
    blocker_details: dict[str, str] = field(default_factory=dict)
    retry_counts: dict[str, int] = field(default_factory=dict)
    global_iterations: int = 0
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)
    last_error: str = ""
    last_error_category: str = ""
    last_successful_action: str = ""
    artifacts: dict[str, str] = field(default_factory=dict)

    @property
    def phase(self) -> Phase:
        return Phase(self.current_phase)

    def transition(self, target: Phase) -> None:
        if not is_allowed_transition(self.phase, target):
            raise ValueError(f"invalid phase transition: {self.phase.value} -> {target.value}")
        self.current_phase = target.value
        self.updated_at = utc_now()

    def record_success(self, detail: str = "") -> None:
        if self.current_phase not in self.completed_phases:
            self.completed_phases.append(self.current_phase)
        self.last_successful_action = detail or self.current_phase
        self.last_error = ""
        self.last_error_category = ""
        self.updated_at = utc_now()

    def record_failure(self, category: ErrorCategory, message: str) -> int:
        if self.current_phase not in self.failed_phases:
            self.failed_phases.append(self.current_phase)
        self.retry_counts[self.current_phase] = self.retry_counts.get(self.current_phase, 0) + 1
        self.last_error_category = category.value
        self.last_error = message[:2000]
        self.updated_at = utc_now()
        return self.retry_counts[self.current_phase]

    def record_blocker(self, category: ErrorCategory, message: str) -> None:
        if self.current_phase not in self.blocked_phases:
            self.blocked_phases.append(self.current_phase)
        self.last_error_category = category.value
        self.last_error = message[:2000]
        self.blocker_details[self.current_phase] = message[:2000]
        self.updated_at = utc_now()


class StateStore:
    def __init__(self, path: Path) -> None:
        self.path = path

    def save(self, state: LoopState) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(self.path.name + ".tmp")
        temporary.write_text(
            json.dumps(asdict(state), indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        temporary.replace(self.path)

    def load(self) -> LoopState:
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("loop state must be a JSON object")
        return LoopState(**payload)
