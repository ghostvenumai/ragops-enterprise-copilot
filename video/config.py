"""Typed configuration and path boundaries for video production."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def repository_path(relative: str) -> Path:
    path = (REPO_ROOT / relative).resolve()
    if not path.is_relative_to(REPO_ROOT):
        raise ValueError(f"video path escapes repository: {relative}")
    return path


@dataclass(frozen=True)
class VideoConfig:
    timeline_path: Path
    tmp_dir: Path
    logs_dir: Path
    dist_dir: Path
    language: str = "de"
    width: int = 1920
    height: int = 1080
    fps: int = 30
    min_duration_seconds: float = 120.0
    max_duration_seconds: float = 180.0
    api_port: int = 8765
    dashboard_port: int = 8766
    chrome_debug_port: int = 8767
    tts_model: str = "gpt-4o-mini-tts"
    tts_voice: str = "coral"

    @classmethod
    def from_env(cls) -> VideoConfig:
        return cls(
            timeline_path=repository_path("video/script/timeline.json"),
            tmp_dir=repository_path("video/tmp"),
            logs_dir=repository_path("video/logs"),
            dist_dir=repository_path("dist"),
            language=os.getenv("VIDEO_LANGUAGE", "de"),
            width=int(os.getenv("VIDEO_WIDTH", "1920")),
            height=int(os.getenv("VIDEO_HEIGHT", "1080")),
            fps=int(os.getenv("VIDEO_FPS", "30")),
            api_port=int(os.getenv("VIDEO_API_PORT", "8765")),
            dashboard_port=int(os.getenv("VIDEO_DASHBOARD_PORT", "8766")),
            chrome_debug_port=int(os.getenv("VIDEO_CHROME_DEBUG_PORT", "8767")),
            tts_model=os.getenv("OPENAI_TTS_MODEL", "gpt-4o-mini-tts"),
            tts_voice=os.getenv("OPENAI_TTS_VOICE", "coral"),
        )

    def prepare_directories(self) -> None:
        for path in (self.tmp_dir, self.logs_dir, self.dist_dir):
            if not path.resolve().is_relative_to(REPO_ROOT):
                raise ValueError(f"unsafe output path: {path}")
            path.mkdir(parents=True, exist_ok=True)
