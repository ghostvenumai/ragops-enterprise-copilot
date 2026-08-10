"""Bounded retry policy for the master loop."""

from __future__ import annotations

import os
from dataclasses import dataclass

from automation.state import ErrorCategory


@dataclass(frozen=True)
class RetryPolicy:
    max_retries_per_state: int = 3
    max_global_iterations: int = 30
    command_timeout_seconds: int = 900

    @classmethod
    def from_env(cls) -> RetryPolicy:
        policy = cls(
            max_retries_per_state=int(os.getenv("MASTER_MAX_RETRIES_PER_STATE", "3")),
            max_global_iterations=int(os.getenv("MASTER_MAX_GLOBAL_ITERATIONS", "30")),
            command_timeout_seconds=int(os.getenv("MASTER_COMMAND_TIMEOUT_SECONDS", "900")),
        )
        if (
            min(
                policy.max_retries_per_state,
                policy.max_global_iterations,
                policy.command_timeout_seconds,
            )
            < 1
        ):
            raise ValueError("master-loop limits must be positive")
        return policy

    def can_retry(self, category: ErrorCategory, attempts: int) -> bool:
        retryable = {
            ErrorCategory.DISPLAY_UNAVAILABLE,
            ErrorCategory.RECORDING_FAILURE,
            ErrorCategory.NETWORK_FAILURE,
            ErrorCategory.RENDER_FAILURE,
        }
        return category in retryable and attempts < self.max_retries_per_state
