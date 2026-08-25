"""Typed configuration and path boundaries for video production."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def environment_flag(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


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
    tts_cache_dir: Path
    language: str = "de"
    width: int = 1920
    height: int = 1080
    fps: int = 30
    min_duration_seconds: float = 120.0
    max_duration_seconds: float = 180.0
    api_port: int = 8765
    dashboard_port: int = 8766
    chrome_debug_port: int = 8767
    burn_subtitles: bool = False
    transition_seconds: float = 0.35
    tts_model: str = "gpt-4o-mini-tts"
    tts_voice: str = "coral"
    tts_provider: str = "openai"
    tts_instructions: str = (
        "Sprich auf Deutsch, professionell, ruhig, technisch und natuerlich. "
        "Formuliere deutlich und ohne Werbeton."
    )
    tts_audio_format: str = "wav"
    tts_speed: float = 1.0
    tts_max_api_calls_per_build: int = 20
    tts_max_retries: int = 2
    tts_min_audio_bytes: int = 256

    @classmethod
    def from_env(cls) -> VideoConfig:
        return cls(
            timeline_path=repository_path("video/script/timeline.json"),
            tmp_dir=repository_path("video/tmp"),
            logs_dir=repository_path("video/logs"),
            dist_dir=repository_path("dist"),
            tts_cache_dir=repository_path("video/cache/tts"),
            language=os.getenv("VIDEO_LANGUAGE", "de"),
            width=int(os.getenv("VIDEO_WIDTH", "1920")),
            height=int(os.getenv("VIDEO_HEIGHT", "1080")),
            fps=int(os.getenv("VIDEO_FPS", "30")),
            api_port=int(os.getenv("VIDEO_API_PORT", "8765")),
            dashboard_port=int(os.getenv("VIDEO_DASHBOARD_PORT", "8766")),
            chrome_debug_port=int(os.getenv("VIDEO_CHROME_DEBUG_PORT", "8767")),
            burn_subtitles=environment_flag("VIDEO_BURN_SUBTITLES"),
            transition_seconds=float(os.getenv("VIDEO_TRANSITION_SECONDS", "0.35")),
            tts_provider=os.getenv("TTS_PROVIDER", "openai"),
            tts_model=(
                os.getenv("TTS_MODEL") or os.getenv("OPENAI_TTS_MODEL") or "gpt-4o-mini-tts"
            ),
            tts_voice=os.getenv("TTS_VOICE") or os.getenv("OPENAI_TTS_VOICE") or "coral",
            tts_instructions=(
                os.getenv("TTS_SPEECH_INSTRUCTIONS")
                or (
                    "Sprich auf Deutsch, professionell, ruhig, technisch und natuerlich. "
                    "Formuliere deutlich und ohne Werbeton."
                )
            ),
            tts_audio_format=os.getenv("TTS_AUDIO_FORMAT", "wav").lower(),
            tts_speed=float(os.getenv("TTS_SPEAKING_SPEED", "1.0")),
            tts_max_api_calls_per_build=int(os.getenv("TTS_MAX_API_CALLS_PER_BUILD", "20")),
            tts_max_retries=int(os.getenv("MAX_TTS_RETRIES", "2")),
            tts_min_audio_bytes=int(os.getenv("TTS_MIN_AUDIO_BYTES", "256")),
        )

    def prepare_directories(self) -> None:
        self.validate_tts()
        for path in (self.tmp_dir, self.logs_dir, self.dist_dir, self.tts_cache_dir):
            if not path.resolve().is_relative_to(REPO_ROOT):
                raise ValueError(f"unsafe output path: {path}")
            path.mkdir(parents=True, exist_ok=True)

    def validate_tts(self) -> None:
        if not 0.1 <= self.transition_seconds <= 1.0:
            raise ValueError("VIDEO_TRANSITION_SECONDS must be between 0.1 and 1.0")
        if self.tts_provider != "openai":
            raise ValueError("TTS_PROVIDER must be 'openai'")
        if self.tts_audio_format not in {"aac", "flac", "mp3", "opus", "wav"}:
            raise ValueError("unsupported TTS_AUDIO_FORMAT")
        if not 0.25 <= self.tts_speed <= 4.0:
            raise ValueError("TTS_SPEAKING_SPEED must be between 0.25 and 4.0")
        if self.tts_max_api_calls_per_build < 1:
            raise ValueError("TTS_MAX_API_CALLS_PER_BUILD must be positive")
        if not 0 <= self.tts_max_retries <= 5:
            raise ValueError("MAX_TTS_RETRIES must be between 0 and 5")
        if self.tts_min_audio_bytes < 44:
            raise ValueError("TTS_MIN_AUDIO_BYTES must be at least 44")
