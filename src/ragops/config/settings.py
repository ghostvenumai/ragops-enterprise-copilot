"""Runtime settings with environment-variable overrides."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    environment: str = "local"
    data_dir: Path = Path("data/synthetic")
    evidence_dir: Path = Path("evidence")
    llm_provider: str = "deterministic"
    prompt_version: str = "ragops-prompt-v1"
    model: str = "deterministic-ragops-v1"
    max_input_chars: int = 4000
    max_output_chars: int = 6000

    @classmethod
    def from_env(cls) -> Settings:
        return cls(
            environment=os.getenv("RAGOPS_ENV", "local"),
            data_dir=Path(os.getenv("RAGOPS_DATA_DIR", "data/synthetic")),
            evidence_dir=Path(os.getenv("RAGOPS_EVIDENCE_DIR", "evidence")),
            llm_provider=os.getenv("RAGOPS_LLM_PROVIDER", "deterministic"),
            prompt_version=os.getenv("RAGOPS_PROMPT_VERSION", "ragops-prompt-v1"),
            model=os.getenv("RAGOPS_MODEL", "deterministic-ragops-v1"),
            max_input_chars=int(os.getenv("RAGOPS_MAX_INPUT_CHARS", "4000")),
            max_output_chars=int(os.getenv("RAGOPS_MAX_OUTPUT_CHARS", "6000")),
        )
