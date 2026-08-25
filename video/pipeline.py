"""End-to-end deterministic demo-video build orchestration."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

from ragops.demo.controller import DemoController
from video.config import REPO_ROOT, VideoConfig, repository_path
from video.narration.generator import generate_narration
from video.narration.tts import (
    CacheOnlyMissError,
    MissingCredentialError,
    TTSCacheStats,
    clear_tts_cache,
    generate_preview_silence,
    generate_voice_assets,
    redact_secrets,
)
from video.qa.narration import validate_narration
from video.qa.validator import validate_video
from video.recording.capture import capture_scenes
from video.rendering.renderer import measure_audio_durations, render_video
from video.subtitles.generator import generate_subtitles
from video.timeline import Timeline, load_timeline

EXTERNAL_BLOCKER_EXIT = 10
CACHE_MISS_EXIT = 11


@dataclass(frozen=True)
class VideoBuildResult:
    build_id: str
    status: str
    application: str
    recording: str
    narration: str
    narration_qa: str
    voiceover: str
    subtitles: str
    rendering: str
    video_qa: str
    output: str | None
    report: str
    warnings: tuple[str, ...]
    external_blockers: tuple[str, ...]
    tts_cache: dict[str, object]


def _atomic_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def video_precheck(config: VideoConfig, timeline: Timeline) -> dict[str, object]:
    tools = {name: shutil.which(name) for name in ("ffmpeg", "ffprobe", "google-chrome")}
    checks = {
        "python_3_12_or_newer": sys.version_info >= (3, 12),
        "virtual_environment": sys.prefix != sys.base_prefix,
        "timeline_valid": bool(timeline.scenes),
        "timeline_duration_valid": (
            config.min_duration_seconds <= timeline.total_duration <= config.max_duration_seconds
        ),
        "synthetic_documents_present": (REPO_ROOT / "data/synthetic/documents").is_dir(),
        "ffmpeg_available": bool(tools["ffmpeg"]),
        "ffprobe_available": bool(tools["ffprobe"]),
        "chrome_available": bool(tools["google-chrome"]),
        "resolution_1920x1080": (config.width, config.height) == (1920, 1080),
        "fps_30": config.fps == 30,
    }
    report: dict[str, object] = {
        "status": "passed" if all(checks.values()) else "failed",
        "checks": checks,
        "tools": {name: Path(path).name if path else "missing" for name, path in tools.items()},
        "tts_credential_configured": bool(os.getenv("OPENAI_API_KEY")),
        "timeline_seconds": timeline.total_duration,
        "language": timeline.language,
    }
    _atomic_json(config.tmp_dir / "video-precheck.json", report)
    if report["status"] != "passed":
        failed = [name for name, passed in checks.items() if not passed]
        raise RuntimeError("video precheck failed: " + ", ".join(failed))
    return report


def _tts_report_table(stats: dict[str, object]) -> str:
    labels = (
        ("Segmente gesamt", "segments_total"),
        ("Cache Hits", "cache_hits"),
        ("Cache Misses", "cache_misses"),
        ("Defekte Eintraege", "corrupt_entries"),
        ("Erforderliche API-Aufrufe", "api_calls_needed"),
        ("Neue API-Aufrufe", "api_requests"),
        ("Wiederverwendetes Audio", "reused_audio"),
        ("Zu generierende Zeichen", "characters_to_generate"),
    )
    return "\n".join(f"| {label} | {stats.get(key, 0)} |" for label, key in labels)


def _write_build_report(result: VideoBuildResult, config: VideoConfig) -> None:
    warnings = "\n".join(f"- {item}" for item in result.warnings) or "- Keine"
    blockers = "\n".join(f"- {item}" for item in result.external_blockers) or "- Keine"
    output = result.output or "Nicht erzeugt"
    tts_rows = _tts_report_table(result.tts_cache)
    text = f"""# RAGOps Demo Build Report

- Build ID: `{result.build_id}`
- Datum: `{datetime.now(UTC).isoformat()}`
- Finaler Status: **{result.status}**

| Phase | Status |
|---|---|
| Anwendung | {result.application} |
| Aufnahme | {result.recording} |
| Narration | {result.narration} |
| Narration QA | {result.narration_qa} |
| AI Voice | {result.voiceover} |
| Untertitel | {result.subtitles} |
| Rendering | {result.rendering} |
| Video QA | {result.video_qa} |

## TTS Cache

| Messwert | Wert |
|---|---:|
{tts_rows}

TTS-Status: **{result.tts_cache.get("status", "NOT_RUN")}**

## Ausgabe

`{output}`

## Warnungen

{warnings}

## Externe Blocker

{blockers}
"""
    path = config.dist_dir / "build_report.md"
    path.write_text(text, encoding="utf-8")


def _print_summary(result: VideoBuildResult) -> None:
    print("=" * 56)
    print("RAGOPS ENTERPRISE COPILOT - VIDEO BUILD REPORT")
    print("=" * 56)
    for label, value in (
        ("APPLICATION", result.application),
        ("RECORDING", result.recording),
        ("NARRATION", result.narration),
        ("NARRATION QA", result.narration_qa),
        ("VOICEOVER", result.voiceover),
        ("SUBTITLES", result.subtitles),
        ("RENDER", result.rendering),
        ("VIDEO QA", result.video_qa),
    ):
        print(f"{label + ':':18}{value}")
    print(f"TTS CACHE:         {result.tts_cache.get('status', 'NOT_RUN')}")
    print(f"API REQUESTS:      {result.tts_cache.get('api_requests', 0)}")
    print(f"\nFINAL STATUS:     {result.status}")
    print(f"VIDEO:            {result.output or 'nicht erzeugt'}")
    print(f"REPORT:           {result.report}")
    print("=" * 56)


def _no_request_stats(status: str, segments: int) -> TTSCacheStats:
    return TTSCacheStats(
        status=status,
        segments_total=segments,
        cache_hits=0,
        cache_misses=0,
        corrupt_entries=0,
        api_calls_needed=0,
        api_requests=0,
        reused_audio=0,
        characters_to_generate=0,
    )


def _blocked_result(
    *,
    build_id: str,
    config: VideoConfig,
    narration_path: Path,
    stats: TTSCacheStats,
    cache_only: bool,
) -> VideoBuildResult:
    if cache_only:
        status = "CACHE_MISS"
        voiceover = "CACHE_MISS"
        warning = "Cache-only-Modus: mindestens ein validiertes Voice-Segment fehlt"
        blockers: tuple[str, ...] = ()
    else:
        status = "READY_EXCEPT_EXTERNAL_BLOCKER"
        voiceover = "BLOCKED_EXTERNAL_CREDENTIAL"
        warning = (
            "Kein stummes Ersatz-Audio erzeugt; Rendering wartet auf Voice-Cache oder Credential"
        )
        blockers = ("OPENAI_API_KEY fehlt fuer nicht gecachte Voice-Segmente",)
    result = VideoBuildResult(
        build_id=build_id,
        status=status,
        application="PASS",
        recording="PASS",
        narration=f"PASS ({narration_path.relative_to(REPO_ROOT)})",
        narration_qa="PASS",
        voiceover=voiceover,
        subtitles="PASS_SIDECAR",
        rendering="NOT_RUN_TTS_UNAVAILABLE",
        video_qa="NOT_RUN",
        output=None,
        report="dist/build_report.md",
        warnings=(warning,),
        external_blockers=blockers,
        tts_cache=stats.as_dict(),
    )
    _write_build_report(result, config)
    _atomic_json(config.tmp_dir / "video-build-state.json", asdict(result))
    _print_summary(result)
    return result


def run_pipeline(
    *,
    dry_run: bool = False,
    skip_tts: bool = False,
    cache_only: bool = False,
    force_tts: bool = False,
) -> VideoBuildResult:
    config = VideoConfig.from_env()
    config.prepare_directories()
    timeline = load_timeline(
        config.timeline_path,
        min_duration=config.min_duration_seconds,
        max_duration=config.max_duration_seconds,
    )
    video_precheck(config, timeline)
    build_id = datetime.now(UTC).strftime("video-%Y%m%dT%H%M%SZ")
    if dry_run:
        generate_narration(timeline, config.tmp_dir / "dry-run-narration.md")
        validate_narration(
            timeline,
            config,
            config.tmp_dir / "dry-run-narration-qa.json",
            require_capture=False,
        )
        generate_subtitles(timeline, config.tmp_dir / "dry-run-subtitles.srt")
        voice = generate_voice_assets(timeline, config, dry_run=True)
        result = VideoBuildResult(
            build_id=build_id,
            status="DRY_RUN_COMPLETE",
            application="CHECKED",
            recording="CHECKED",
            narration="CHECKED",
            narration_qa="PASS_PLANNED",
            voiceover="CHECKED_NO_REQUEST",
            subtitles="CHECKED_SIDECAR",
            rendering="CHECKED",
            video_qa="CHECKED",
            output=None,
            report="dist/build_report.md",
            warnings=(),
            external_blockers=(),
            tts_cache=voice.stats.as_dict(),
        )
        _write_build_report(result, config)
        _atomic_json(config.tmp_dir / "video-build-state.json", asdict(result))
        _print_summary(result)
        return result

    DemoController(REPO_ROOT / "data/synthetic", config.tmp_dir / "application").run()
    narration_path = generate_narration(timeline, REPO_ROOT / "video/script/narration.md")
    subtitle_path = generate_subtitles(timeline, config.tmp_dir / "subtitles.srt")
    images = capture_scenes(timeline, config)
    validate_narration(
        timeline,
        config,
        config.dist_dir / "narration_qa_report.json",
        capture_manifest_path=config.tmp_dir / "capture-manifest.json",
        require_capture=True,
    )
    warnings: list[str] = []
    preview = skip_tts
    if skip_tts:
        audio = generate_preview_silence(timeline, config)
        stats = _no_request_stats("SKIPPED_EXPLICITLY", len(timeline.scenes))
        warnings.append("Expliziter --skip-tts Preview-Build ohne Sprecherstimme")
        voice_status = "SKIPPED_EXPLICITLY"
    else:
        if force_tts:
            print("WARNING: FORCE TTS MODE WILL GENERATE NEW API REQUESTS")
            warnings.append("Force-TTS wurde explizit aktiviert; vorhandene Cache-Hits ignoriert")
        try:
            voice = generate_voice_assets(
                timeline,
                config,
                cache_only=cache_only,
                force_tts=force_tts,
            )
        except MissingCredentialError as exc:
            stats = exc.stats or _no_request_stats(
                "BLOCKED_EXTERNAL_CREDENTIAL", len(timeline.scenes)
            )
            return _blocked_result(
                build_id=build_id,
                config=config,
                narration_path=narration_path,
                stats=stats,
                cache_only=False,
            )
        except CacheOnlyMissError as exc:
            stats = exc.stats or _no_request_stats("CACHE_MISS", len(timeline.scenes))
            return _blocked_result(
                build_id=build_id,
                config=config,
                narration_path=narration_path,
                stats=stats,
                cache_only=True,
            )
        audio = voice.assets
        stats = voice.stats
        voice_status = "PASS_CACHE_ONLY" if cache_only else "PASS"
    speech_durations = measure_audio_durations(timeline, audio, config, preview=preview)
    generate_subtitles(
        timeline,
        subtitle_path,
        speech_durations=speech_durations,
        transition_seconds=config.transition_seconds,
    )
    output = render_video(
        timeline,
        images,
        audio,
        subtitle_path,
        config,
        preview=preview,
    )
    qa = validate_video(output, config, preview=preview)
    qa_warnings = qa.get("warnings", [])
    if isinstance(qa_warnings, list):
        warnings.extend(str(item) for item in qa_warnings)
    status = "PREVIEW_COMPLETE" if preview else "COMPLETE"
    result = VideoBuildResult(
        build_id=build_id,
        status=status,
        application="PASS",
        recording="PASS",
        narration=f"PASS ({narration_path.relative_to(REPO_ROOT)})",
        narration_qa="PASS",
        voiceover=voice_status,
        subtitles="PASS_BURNED" if config.burn_subtitles else "PASS_SIDECAR",
        rendering="PASS_PREVIEW" if preview else "PASS",
        video_qa="PASS_PREVIEW" if preview else "PASS",
        output=str(output.relative_to(REPO_ROOT)),
        report="dist/build_report.md",
        warnings=tuple(dict.fromkeys(warnings)),
        external_blockers=(),
        tts_cache=stats.as_dict(),
    )
    _write_build_report(result, config)
    _atomic_json(config.tmp_dir / "video-build-state.json", asdict(result))
    _print_summary(result)
    return result


def _clean_temp(config: VideoConfig) -> None:
    expected = repository_path("video/tmp")
    if config.tmp_dir.resolve() != expected:
        raise ValueError("refusing to clean an unexpected temporary directory")
    if config.tmp_dir.exists():
        shutil.rmtree(config.tmp_dir)
    config.tmp_dir.mkdir(parents=True, exist_ok=True)
    print("VIDEO TEMP: CLEARED")
    print("TTS CACHE: PRESERVED")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build the RAGOps technical demo video")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--skip-tts", action="store_true")
    parser.add_argument("--cache-only", action="store_true")
    parser.add_argument("--force-tts", action="store_true")
    parser.add_argument("--clean-temp", action="store_true")
    parser.add_argument("--clear-tts-cache", action="store_true")
    parser.add_argument("--resume", action="store_true", help="Use normal cache-first behavior")
    args = parser.parse_args(argv)
    exclusive = sum((args.dry_run, args.skip_tts, args.cache_only, args.force_tts))
    if exclusive > 1:
        parser.error("--dry-run, --skip-tts, --cache-only and --force-tts are mutually exclusive")
    try:
        config = VideoConfig.from_env()
        config.prepare_directories()
        if args.clean_temp:
            _clean_temp(config)
            return 0
        if args.clear_tts_cache:
            removed = clear_tts_cache(config)
            print(f"TTS CACHE: CLEARED ({removed} files)")
            return 0
        result = run_pipeline(
            dry_run=args.dry_run,
            skip_tts=args.skip_tts,
            cache_only=args.cache_only,
            force_tts=args.force_tts,
        )
    except Exception as exc:
        safe = redact_secrets(str(exc), os.getenv("OPENAI_API_KEY"))
        print(f"VIDEO BUILD FAILED: {type(exc).__name__}: {safe}", file=sys.stderr)
        return 1
    if result.status == "READY_EXCEPT_EXTERNAL_BLOCKER":
        return EXTERNAL_BLOCKER_EXIT
    if result.status == "CACHE_MISS":
        return CACHE_MISS_EXIT
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
