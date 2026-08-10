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
from video.config import REPO_ROOT, VideoConfig
from video.narration.generator import generate_narration
from video.narration.tts import (
    MissingCredentialError,
    generate_preview_silence,
    generate_voice_assets,
)
from video.qa.validator import validate_video
from video.recording.capture import capture_scenes
from video.rendering.renderer import render_video
from video.subtitles.generator import generate_subtitles
from video.timeline import Timeline, load_timeline

EXTERNAL_BLOCKER_EXIT = 10


@dataclass(frozen=True)
class VideoBuildResult:
    build_id: str
    status: str
    application: str
    recording: str
    narration: str
    voiceover: str
    subtitles: str
    rendering: str
    video_qa: str
    output: str | None
    report: str
    warnings: tuple[str, ...]
    external_blockers: tuple[str, ...]


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


def _write_build_report(result: VideoBuildResult, config: VideoConfig) -> None:
    warnings = "\n".join(f"- {item}" for item in result.warnings) or "- Keine"
    blockers = "\n".join(f"- {item}" for item in result.external_blockers) or "- Keine"
    output = result.output or "Nicht erzeugt"
    text = f"""# RAGOps Demo Build Report

- Build ID: `{result.build_id}`
- Datum: `{datetime.now(UTC).isoformat()}`
- Finaler Status: **{result.status}**

| Phase | Status |
|---|---|
| Anwendung | {result.application} |
| Aufnahme | {result.recording} |
| Narration | {result.narration} |
| AI Voice | {result.voiceover} |
| Untertitel | {result.subtitles} |
| Rendering | {result.rendering} |
| Video QA | {result.video_qa} |

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
        ("VOICEOVER", result.voiceover),
        ("SUBTITLES", result.subtitles),
        ("RENDER", result.rendering),
        ("VIDEO QA", result.video_qa),
    ):
        print(f"{label + ':':18}{value}")
    print(f"\nFINAL STATUS:     {result.status}")
    print(f"VIDEO:            {result.output or 'nicht erzeugt'}")
    print(f"REPORT:           {result.report}")
    print("=" * 56)


def run_pipeline(*, dry_run: bool = False, skip_tts: bool = False) -> VideoBuildResult:
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
        generate_subtitles(timeline, config.tmp_dir / "dry-run-subtitles.srt")
        result = VideoBuildResult(
            build_id=build_id,
            status="DRY_RUN_COMPLETE",
            application="CHECKED",
            recording="CHECKED",
            narration="CHECKED",
            voiceover="CHECKED_NO_REQUEST",
            subtitles="CHECKED",
            rendering="CHECKED",
            video_qa="CHECKED",
            output=None,
            report=str(config.tmp_dir / "video-precheck.json"),
            warnings=(),
            external_blockers=(),
        )
        _atomic_json(config.tmp_dir / "video-build-state.json", asdict(result))
        _print_summary(result)
        return result

    DemoController(REPO_ROOT / "data/synthetic", config.tmp_dir / "application").run()
    narration_path = generate_narration(timeline, REPO_ROOT / "video/script/narration.md")
    subtitle_path = generate_subtitles(timeline, config.tmp_dir / "subtitles.srt")
    images = capture_scenes(timeline, config)
    blockers: list[str] = []
    warnings: list[str] = []
    preview = skip_tts
    voice_status = "SKIPPED_EXPLICITLY" if skip_tts else "PASS"
    if skip_tts:
        audio = generate_preview_silence(timeline, config)
        warnings.append("Expliziter --skip-tts Preview-Build ohne Sprecherstimme")
    else:
        try:
            audio = generate_voice_assets(timeline, config)
        except MissingCredentialError:
            preview = True
            voice_status = "BLOCKED_EXTERNAL_CREDENTIAL"
            blockers.append("OPENAI_API_KEY fehlt; finales AI Voiceover wurde nicht erzeugt")
            warnings.append("Gerendertes MP4 ist ein klar benannter stummer Preview-Build")
            audio = generate_preview_silence(timeline, config)
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
    status = (
        "READY_EXCEPT_EXTERNAL_BLOCKER"
        if blockers
        else ("PREVIEW_COMPLETE" if preview else "COMPLETE")
    )
    result = VideoBuildResult(
        build_id=build_id,
        status=status,
        application="PASS",
        recording="PASS",
        narration=f"PASS ({narration_path.relative_to(REPO_ROOT)})",
        voiceover=voice_status,
        subtitles="PASS",
        rendering="PASS_PREVIEW" if preview else "PASS",
        video_qa="PASS_PREVIEW" if preview else "PASS",
        output=str(output.relative_to(REPO_ROOT)),
        report="dist/build_report.md",
        warnings=tuple(dict.fromkeys(warnings)),
        external_blockers=tuple(blockers),
    )
    _write_build_report(result, config)
    _atomic_json(config.tmp_dir / "video-build-state.json", asdict(result))
    _print_summary(result)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build the RAGOps technical demo video")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--skip-tts", action="store_true")
    parser.add_argument("--resume", action="store_true", help="Reuse cached TTS assets when valid")
    args = parser.parse_args(argv)
    try:
        result = run_pipeline(dry_run=args.dry_run, skip_tts=args.skip_tts)
    except Exception as exc:
        print(f"VIDEO BUILD FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    return EXTERNAL_BLOCKER_EXIT if result.status == "READY_EXCEPT_EXTERNAL_BLOCKER" else 0


if __name__ == "__main__":
    raise SystemExit(main())
