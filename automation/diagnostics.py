"""Classify failures without swallowing their original diagnostics."""

from __future__ import annotations

import subprocess

from video.narration.tts import MissingCredentialError

from automation.state import ErrorCategory, Phase


def classify_error(phase: Phase, error: BaseException) -> ErrorCategory:
    if isinstance(error, MissingCredentialError):
        return ErrorCategory.EXTERNAL_CREDENTIAL_MISSING
    if isinstance(error, subprocess.TimeoutExpired):
        return ErrorCategory.NETWORK_FAILURE
    if isinstance(error, FileNotFoundError):
        return ErrorCategory.DEPENDENCY_MISSING
    mapping = {
        Phase.UNIT_TEST: ErrorCategory.TEST_FAILURE,
        Phase.INTEGRATION_TEST: ErrorCategory.TEST_FAILURE,
        Phase.STATIC_CHECK: ErrorCategory.TEST_FAILURE,
        Phase.SECURITY_CHECK: ErrorCategory.SECURITY_BLOCK,
        Phase.RECORD: ErrorCategory.RECORDING_FAILURE,
        Phase.GENERATE_VOICE: ErrorCategory.TTS_FAILURE,
        Phase.RENDER: ErrorCategory.RENDER_FAILURE,
        Phase.VIDEO_QA: ErrorCategory.VIDEO_QA_FAILURE,
    }
    return mapping.get(phase, ErrorCategory.APPLICATION_ERROR)
