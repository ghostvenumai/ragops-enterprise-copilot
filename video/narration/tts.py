"""Scene-based, cached text-to-speech generation."""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
from pathlib import Path
from typing import Protocol

from video.config import VideoConfig
from video.timeline import Timeline


class MissingCredentialError(RuntimeError):
    """Raised when final OpenAI voice generation cannot be authorized."""


class VoiceProvider(Protocol):
    def generate(self, text: str, output_path: Path) -> None: ...


class OpenAITTSProvider:
    def __init__(self, api_key: str, model: str, voice: str) -> None:
        if not api_key:
            raise MissingCredentialError("OPENAI_API_KEY is not configured")
        from openai import OpenAI

        self.client = OpenAI(api_key=api_key)
        self.model = model
        self.voice = voice

    def generate(self, text: str, output_path: Path) -> None:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with self.client.audio.speech.with_streaming_response.create(
            model=self.model,
            voice=self.voice,
            input=text,
            response_format="wav",
            instructions=(
                "Sprich auf Deutsch, professionell, ruhig, technisch und natürlich. "
                "Formuliere deutlich und ohne Werbeton."
            ),
        ) as response:
            response.stream_to_file(output_path)


def _cache_key(text: str, config: VideoConfig) -> str:
    material = f"{config.language}\0{config.tts_model}\0{config.tts_voice}\0{text}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def generate_voice_assets(
    timeline: Timeline,
    config: VideoConfig,
    provider: VoiceProvider | None = None,
) -> dict[str, Path]:
    provider = provider or OpenAITTSProvider(
        os.getenv("OPENAI_API_KEY", ""), config.tts_model, config.tts_voice
    )
    audio_dir = config.tmp_dir / "audio"
    audio_dir.mkdir(parents=True, exist_ok=True)
    assets: dict[str, Path] = {}
    for scene in timeline.scenes:
        path = audio_dir / f"{scene.order:03d}_{scene.id}.wav"
        digest = _cache_key(scene.narration, config)
        digest_path = path.with_suffix(".sha256")
        if path.exists() and digest_path.exists() and digest_path.read_text().strip() == digest:
            assets[scene.id] = path
            continue
        provider.generate(scene.narration, path)
        digest_path.write_text(digest + "\n", encoding="utf-8")
        assets[scene.id] = path
    return assets


def generate_preview_silence(timeline: Timeline, config: VideoConfig) -> dict[str, Path]:
    """Create explicit silent-preview audio; never presented as final voiceover."""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("ffmpeg is required for preview audio generation")
    audio_dir = config.tmp_dir / "audio-preview"
    audio_dir.mkdir(parents=True, exist_ok=True)
    assets: dict[str, Path] = {}
    for scene in timeline.scenes:
        path = audio_dir / f"{scene.order:03d}_{scene.id}.wav"
        completed = subprocess.run(  # noqa: S603 - fixed ffmpeg argument list.
            [
                ffmpeg,
                "-y",
                "-v",
                "error",
                "-f",
                "lavfi",
                "-i",
                "anullsrc=channel_layout=stereo:sample_rate=48000",
                "-t",
                str(scene.planned_duration),
                "-c:a",
                "pcm_s16le",
                str(path),
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        if completed.returncode != 0:
            raise RuntimeError(f"ffmpeg silence generation failed: {completed.stderr[-500:]}")
        assets[scene.id] = path
    return assets
