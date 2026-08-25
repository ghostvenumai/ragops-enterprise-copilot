"""Persistent, validated and content-addressable TTS audio cache."""

from __future__ import annotations

import fcntl
import json
import os
import shutil
import subprocess
import wave
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

CACHE_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class AudioMetadata:
    duration_seconds: float
    size_bytes: int
    format_name: str


@dataclass(frozen=True)
class CacheInspection:
    status: str
    path: Path
    metadata: AudioMetadata | None = None


class AudioValidationError(RuntimeError):
    """Raised when a generated or cached audio file is unusable."""


class AudioValidator:
    """Validate real decoding, duration, size and expected container format."""

    _FORMAT_ALIASES = {
        "aac": {"aac"},
        "flac": {"flac"},
        "mp3": {"mp3"},
        "opus": {"ogg", "opus"},
        "wav": {"wav"},
    }

    def __init__(self, min_size_bytes: int = 256) -> None:
        self.min_size_bytes = min_size_bytes

    def validate(self, path: Path, expected_format: str) -> AudioMetadata:
        if not path.exists() or not path.is_file() or path.is_symlink():
            raise AudioValidationError("audio file is missing or not a regular file")
        try:
            size = path.stat().st_size
        except OSError as exc:
            raise AudioValidationError("audio metadata is unreadable") from exc
        if size < self.min_size_bytes:
            raise AudioValidationError("audio file is smaller than the configured minimum")
        ffprobe = shutil.which("ffprobe")
        if ffprobe:
            return self._validate_with_ffprobe(path, expected_format, ffprobe, size)
        if expected_format == "wav":
            return self._validate_wave(path, size)
        raise AudioValidationError("ffprobe is required to validate this audio format")

    def _validate_wave(self, path: Path, size: int) -> AudioMetadata:
        try:
            with wave.open(str(path), "rb") as audio:
                frame_rate = audio.getframerate()
                frames = audio.getnframes()
        except (EOFError, OSError, wave.Error) as exc:
            raise AudioValidationError("WAV audio is not decodable") from exc
        duration = frames / frame_rate if frame_rate else 0.0
        if duration <= 0:
            raise AudioValidationError("audio duration must be positive")
        return AudioMetadata(duration, size, "wav")

    def _validate_with_ffprobe(
        self, path: Path, expected_format: str, ffprobe: str, size: int
    ) -> AudioMetadata:
        try:
            completed = subprocess.run(  # noqa: S603 - fixed ffprobe argument list.
                [
                    ffprobe,
                    "-v",
                    "error",
                    "-show_entries",
                    "format=format_name,duration",
                    "-of",
                    "json",
                    str(path),
                ],
                check=False,
                capture_output=True,
                text=True,
                timeout=20,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise AudioValidationError("ffprobe could not validate audio") from exc
        if completed.returncode:
            raise AudioValidationError("audio is not decodable by ffprobe")
        try:
            payload = json.loads(completed.stdout)
            format_payload = payload["format"]
            format_names = {item.strip() for item in format_payload["format_name"].split(",")}
            duration = float(format_payload["duration"])
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise AudioValidationError("ffprobe returned incomplete audio metadata") from exc
        expected_names = self._FORMAT_ALIASES.get(expected_format, {expected_format})
        if not format_names.intersection(expected_names):
            raise AudioValidationError("audio container does not match the requested format")
        if duration <= 0:
            raise AudioValidationError("audio duration must be positive")
        return AudioMetadata(duration, size, sorted(format_names)[0])


class TTSCache:
    def __init__(self, root: Path, validator: AudioValidator) -> None:
        self.root = root
        self.validator = validator
        self.manifest_path = root / "manifest.json"
        self.lock_dir = root / ".locks"
        self.root.mkdir(parents=True, exist_ok=True)
        self.lock_dir.mkdir(parents=True, exist_ok=True)

    def audio_path(self, cache_key: str, audio_format: str) -> Path:
        if len(cache_key) != 64 or any(char not in "0123456789abcdef" for char in cache_key):
            raise ValueError("invalid TTS cache key")
        return self.root / f"{cache_key}.{audio_format}"

    def inspect(self, cache_key: str, audio_format: str) -> CacheInspection:
        path = self.audio_path(cache_key, audio_format)
        if not path.exists():
            return CacheInspection("MISS", path)
        try:
            metadata = self.validator.validate(path, audio_format)
        except AudioValidationError:
            return CacheInspection("CORRUPT", path)
        return CacheInspection("HIT", path, metadata)

    @contextmanager
    def segment_lock(self, cache_key: str) -> Iterator[None]:
        lock_path = self.lock_dir / f"{cache_key}.lock"
        with lock_path.open("a", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def discard(self, cache_key: str, audio_format: str) -> None:
        path = self.audio_path(cache_key, audio_format)
        if path.exists() or path.is_symlink():
            path.unlink()
        self._remove_manifest_entry(cache_key)

    def commit(
        self,
        cache_key: str,
        partial_path: Path,
        audio_format: str,
        *,
        provider: str,
        language: str,
        text_hash: str,
    ) -> tuple[Path, AudioMetadata]:
        metadata = self.validator.validate(partial_path, audio_format)
        destination = self.audio_path(cache_key, audio_format)
        os.replace(partial_path, destination)
        self._write_manifest_entry(
            cache_key,
            {
                "file": destination.name,
                "provider": provider,
                "language": language,
                "created_at": datetime.now(UTC).isoformat(),
                "text_hash": text_hash,
                "audio_duration": round(metadata.duration_seconds, 3),
                "size_bytes": metadata.size_bytes,
            },
        )
        return destination, metadata

    @contextmanager
    def _manifest_lock(self) -> Iterator[None]:
        lock_path = self.lock_dir / "manifest.lock"
        with lock_path.open("a", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _load_manifest(self) -> dict[str, Any]:
        if not self.manifest_path.exists():
            return {"cache_schema_version": CACHE_SCHEMA_VERSION, "entries": {}}
        try:
            payload = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {"cache_schema_version": CACHE_SCHEMA_VERSION, "entries": {}}
        if not isinstance(payload, dict) or not isinstance(payload.get("entries"), dict):
            return {"cache_schema_version": CACHE_SCHEMA_VERSION, "entries": {}}
        if payload.get("cache_schema_version") != CACHE_SCHEMA_VERSION:
            return {"cache_schema_version": CACHE_SCHEMA_VERSION, "entries": {}}
        return payload

    def _write_manifest_entry(self, cache_key: str, entry: dict[str, object]) -> None:
        with self._manifest_lock():
            payload = self._load_manifest()
            entries = payload["entries"]
            assert isinstance(entries, dict)
            entries[cache_key] = entry
            self._atomic_manifest(payload)

    def _remove_manifest_entry(self, cache_key: str) -> None:
        with self._manifest_lock():
            payload = self._load_manifest()
            entries = payload["entries"]
            assert isinstance(entries, dict)
            if entries.pop(cache_key, None) is not None:
                self._atomic_manifest(payload)

    def _atomic_manifest(self, payload: dict[str, Any]) -> None:
        temporary = self.manifest_path.with_suffix(".json.partial")
        temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(temporary, self.manifest_path)

    def clear(self) -> int:
        removed = 0
        for path in self.root.iterdir():
            if path == self.lock_dir:
                continue
            if path.is_file() or path.is_symlink():
                path.unlink()
                removed += 1
        return removed
