"""Cache-first, scene-based text-to-speech generation."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import time
import unicodedata
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal, Protocol, cast

from video.config import VideoConfig
from video.narration.cache import CACHE_SCHEMA_VERSION, AudioValidator, TTSCache
from video.timeline import Timeline

_KEY_PATTERN = re.compile(r"\bsk-[A-Za-z0-9_-]{4,}\b")
_AUTH_PATTERN = re.compile(r"(?i)(authorization\s*:\s*bearer\s+)[^\s,;]+")
AudioFormat = Literal["aac", "flac", "mp3", "opus", "wav"]


class MissingCredentialError(RuntimeError):
    """Raised when missing cache entries cannot be generated without a credential."""

    def __init__(self, message: str, stats: TTSCacheStats | None = None) -> None:
        super().__init__(message)
        self.stats = stats


class CacheOnlyMissError(RuntimeError):
    """Raised when cache-only mode finds one or more missing voice segments."""

    def __init__(self, message: str, stats: TTSCacheStats | None = None) -> None:
        super().__init__(message)
        self.stats = stats


class TTSSafetyLimitError(RuntimeError):
    """Raised before requests exceed the configured per-build safety limit."""


class TTSProviderError(RuntimeError):
    """Sanitized provider failure."""


class RetryableTTSProviderError(TTSProviderError):
    """Sanitized transient provider failure eligible for a bounded retry."""


@dataclass(frozen=True)
class TTSRequest:
    text: str
    model: str
    voice: str
    instructions: str
    audio_format: AudioFormat
    speed: float


class TTSProvider(Protocol):
    def synthesize(self, request: TTSRequest, output_path: Path) -> None: ...


class OpenAITTSProvider:
    def __init__(self, api_key: str) -> None:
        if not api_key:
            raise MissingCredentialError("OPENAI_API_KEY is not configured")
        from openai import OpenAI

        self._api_key = api_key
        self.client = OpenAI(api_key=api_key)

    def synthesize(self, request: TTSRequest, output_path: Path) -> None:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with self.client.audio.speech.with_streaming_response.create(
                model=request.model,
                voice=request.voice,
                input=request.text,
                response_format=request.audio_format,
                instructions=request.instructions,
                speed=request.speed,
            ) as response:
                response.stream_to_file(output_path)
        except Exception as exc:
            message = redact_secrets(f"{type(exc).__name__}: {exc}", self._api_key)
            status_code = getattr(exc, "status_code", None)
            if status_code in {408, 409, 429} or (
                isinstance(status_code, int) and status_code >= 500
            ):
                raise RetryableTTSProviderError(message) from None
            if type(exc).__name__ in {"APIConnectionError", "APITimeoutError"}:
                raise RetryableTTSProviderError(message) from None
            raise TTSProviderError(message) from None


@dataclass(frozen=True)
class TTSPlanItem:
    scene_id: str
    order: int
    cache_key: str
    text_hash: str
    status: str
    path: Path
    request: TTSRequest


@dataclass(frozen=True)
class TTSPlan:
    items: tuple[TTSPlanItem, ...]
    cache_hits: int
    cache_misses: int
    corrupt_entries: int
    api_calls_needed: int
    characters_to_generate: int


@dataclass(frozen=True)
class TTSCacheStats:
    status: str
    segments_total: int
    cache_hits: int
    cache_misses: int
    corrupt_entries: int
    api_calls_needed: int
    api_requests: int
    reused_audio: int
    characters_to_generate: int

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class VoiceGenerationResult:
    assets: dict[str, Path]
    stats: TTSCacheStats


def redact_secrets(message: str, api_key: str | None = None) -> str:
    redacted = _AUTH_PATTERN.sub(r"\1[REDACTED]", message)
    redacted = _KEY_PATTERN.sub("[REDACTED]", redacted)
    if api_key:
        redacted = redacted.replace(api_key, "[REDACTED]")
    return redacted


def normalize_narration(text: str) -> str:
    return " ".join(unicodedata.normalize("NFC", text).split())


def _request_for(text: str, config: VideoConfig) -> TTSRequest:
    return TTSRequest(
        text=normalize_narration(text),
        model=config.tts_model,
        voice=config.tts_voice,
        instructions=normalize_narration(config.tts_instructions),
        audio_format=cast(AudioFormat, config.tts_audio_format),
        speed=config.tts_speed,
    )


def _cache_key(request: TTSRequest, config: VideoConfig) -> str:
    payload = {
        "audio_format": request.audio_format,
        "cache_schema": CACHE_SCHEMA_VERSION,
        "instructions": request.instructions,
        "language": config.language,
        "model": request.model,
        "provider": config.tts_provider,
        "speed": request.speed,
        "text": request.text,
        "voice": request.voice,
    }
    serialized = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def plan_voice_assets(
    timeline: Timeline, config: VideoConfig, *, force_tts: bool = False
) -> TTSPlan:
    config.validate_tts()
    cache = TTSCache(
        config.tts_cache_dir, AudioValidator(min_size_bytes=config.tts_min_audio_bytes)
    )
    items: list[TTSPlanItem] = []
    for scene in timeline.scenes:
        request = _request_for(scene.narration, config)
        cache_key = _cache_key(request, config)
        inspection = cache.inspect(cache_key, request.audio_format)
        status = "FORCED" if force_tts else inspection.status
        items.append(
            TTSPlanItem(
                scene_id=scene.id,
                order=scene.order,
                cache_key=cache_key,
                text_hash=_text_hash(request.text),
                status=status,
                path=inspection.path,
                request=request,
            )
        )
    hits = sum(item.status == "HIT" for item in items)
    misses = len(items) - hits
    unique_missing = {item.cache_key: item for item in items if item.status != "HIT"}
    return TTSPlan(
        items=tuple(items),
        cache_hits=hits,
        cache_misses=misses,
        corrupt_entries=sum(item.status == "CORRUPT" for item in items),
        api_calls_needed=len(unique_missing),
        characters_to_generate=sum(len(item.request.text) for item in unique_missing.values()),
    )


def _print_plan(plan: TTSPlan, *, dry_run: bool = False) -> None:
    print("TTS DRY RUN" if dry_run else "TTS CACHE SUMMARY")
    print(f"Segments:          {len(plan.items)}")
    print(f"Cache hits:        {plan.cache_hits}")
    print(f"Cache misses:      {plan.cache_misses}")
    print(f"API calls needed:  {plan.api_calls_needed}")
    print(f"Characters:        {plan.characters_to_generate}")
    if dry_run:
        print("NO API REQUEST SENT")


def _status_line(scene_id: str, status: str) -> None:
    dots = "." * max(1, 20 - len(scene_id))
    print(f"[VOICE] {scene_id} {dots} {status}")


def _provider_from_environment() -> TTSProvider:
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise MissingCredentialError("OPENAI_API_KEY is not configured")
    return OpenAITTSProvider(api_key)


def _write_cache_report(config: VideoConfig, stats: TTSCacheStats) -> Path:
    path = config.tmp_dir / "tts-cache-report.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".json.partial")
    temporary.write_text(
        json.dumps(stats.as_dict(), indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    os.replace(temporary, path)
    return path


def generate_voice_assets(
    timeline: Timeline,
    config: VideoConfig,
    provider: TTSProvider | None = None,
    *,
    dry_run: bool = False,
    cache_only: bool = False,
    force_tts: bool = False,
) -> VoiceGenerationResult:
    if dry_run and force_tts:
        raise ValueError("dry-run and force-tts cannot be combined")
    if cache_only and force_tts:
        raise ValueError("cache-only and force-tts cannot be combined")
    plan = plan_voice_assets(timeline, config, force_tts=force_tts)
    _print_plan(plan, dry_run=dry_run)
    if plan.api_calls_needed > config.tts_max_api_calls_per_build:
        raise TTSSafetyLimitError(
            "TTS_SAFETY_LIMIT_REACHED: required segments exceed TTS_MAX_API_CALLS_PER_BUILD"
        )
    initial_assets = {item.scene_id: item.path for item in plan.items if item.status == "HIT"}
    if dry_run:
        stats = TTSCacheStats(
            status="DRY_RUN",
            segments_total=len(plan.items),
            cache_hits=plan.cache_hits,
            cache_misses=plan.cache_misses,
            corrupt_entries=plan.corrupt_entries,
            api_calls_needed=plan.api_calls_needed,
            api_requests=0,
            reused_audio=plan.cache_hits,
            characters_to_generate=plan.characters_to_generate,
        )
        _write_cache_report(config, stats)
        return VoiceGenerationResult(initial_assets, stats)
    if cache_only and plan.cache_misses:
        stats = TTSCacheStats(
            status="CACHE_MISS",
            segments_total=len(plan.items),
            cache_hits=plan.cache_hits,
            cache_misses=plan.cache_misses,
            corrupt_entries=plan.corrupt_entries,
            api_calls_needed=plan.api_calls_needed,
            api_requests=0,
            reused_audio=plan.cache_hits,
            characters_to_generate=plan.characters_to_generate,
        )
        _write_cache_report(config, stats)
        raise CacheOnlyMissError(
            "CACHE_MISS: cache-only mode cannot synthesize missing audio", stats
        )
    if plan.cache_misses and provider is None and not os.environ.get("OPENAI_API_KEY"):
        stats = TTSCacheStats(
            status="BLOCKED_EXTERNAL_CREDENTIAL",
            segments_total=len(plan.items),
            cache_hits=plan.cache_hits,
            cache_misses=plan.cache_misses,
            corrupt_entries=plan.corrupt_entries,
            api_calls_needed=plan.api_calls_needed,
            api_requests=0,
            reused_audio=plan.cache_hits,
            characters_to_generate=plan.characters_to_generate,
        )
        _write_cache_report(config, stats)
        raise MissingCredentialError(
            "OPENAI_API_KEY is not configured for missing TTS cache entries", stats
        )
    cache = TTSCache(
        config.tts_cache_dir, AudioValidator(min_size_bytes=config.tts_min_audio_bytes)
    )
    assets = dict(initial_assets)
    api_requests = 0
    reused = plan.cache_hits
    active_provider = provider
    refreshed_keys: set[str] = set()
    for item in plan.items:
        if item.status == "HIT":
            _status_line(item.scene_id, "CACHE HIT")
            continue
        status = "CACHE CORRUPT" if item.status == "CORRUPT" else "CACHE MISS"
        _status_line(item.scene_id, status)
        with cache.segment_lock(item.cache_key):
            current = cache.inspect(item.cache_key, item.request.audio_format)
            if current.status == "HIT" and (not force_tts or item.cache_key in refreshed_keys):
                assets[item.scene_id] = current.path
                reused += 1
                _status_line(item.scene_id, "CACHE HIT AFTER LOCK")
                continue
            if current.status == "CORRUPT":
                cache.discard(item.cache_key, item.request.audio_format)
            if active_provider is None:
                active_provider = _provider_from_environment()
            partial = config.tts_cache_dir / (
                f"{item.cache_key}.{item.request.audio_format}.partial-{uuid.uuid4().hex}"
            )
            try:
                attempts = 0
                while True:
                    if api_requests >= config.tts_max_api_calls_per_build:
                        raise TTSSafetyLimitError(
                            "TTS_SAFETY_LIMIT_REACHED: maximum API requests reached"
                        )
                    api_requests += 1
                    attempts += 1
                    _status_line(item.scene_id, "GENERATING")
                    try:
                        active_provider.synthesize(item.request, partial)
                        break
                    except RetryableTTSProviderError:
                        if attempts > config.tts_max_retries:
                            raise
                        time.sleep(min(0.5 * (2 ** (attempts - 1)), 2.0))
                destination, _ = cache.commit(
                    item.cache_key,
                    partial,
                    item.request.audio_format,
                    provider=config.tts_provider,
                    language=config.language,
                    text_hash=item.text_hash,
                )
            finally:
                if partial.exists() or partial.is_symlink():
                    partial.unlink()
            assets[item.scene_id] = destination
            refreshed_keys.add(item.cache_key)
            _status_line(item.scene_id, "CACHED")
    stats = TTSCacheStats(
        status="PASS",
        segments_total=len(plan.items),
        cache_hits=plan.cache_hits,
        cache_misses=plan.cache_misses,
        corrupt_entries=plan.corrupt_entries,
        api_calls_needed=plan.api_calls_needed,
        api_requests=api_requests,
        reused_audio=reused,
        characters_to_generate=plan.characters_to_generate,
    )
    _write_cache_report(config, stats)
    print("TTS CACHE: PASS")
    print(f"API REQUESTS THIS BUILD: {api_requests}")
    return VoiceGenerationResult(assets, stats)


def clear_tts_cache(config: VideoConfig) -> int:
    cache = TTSCache(
        config.tts_cache_dir, AudioValidator(min_size_bytes=config.tts_min_audio_bytes)
    )
    return cache.clear()


def generate_preview_silence(timeline: Timeline, config: VideoConfig) -> dict[str, Path]:
    """Create explicit silent-preview audio only for the user-selected preview mode."""
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
