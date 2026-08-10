"""Validate rendered video properties with ffprobe and FFmpeg."""

from __future__ import annotations

import json
from fractions import Fraction
from pathlib import Path
from typing import Any

from video.config import REPO_ROOT, VideoConfig
from video.tools import require_tool, run_checked


def build_probe_command(ffprobe: str, video_path: Path) -> list[str]:
    return [
        ffprobe,
        "-v",
        "error",
        "-show_entries",
        "format=duration,size:stream=index,codec_name,codec_type,width,height,r_frame_rate,duration",
        "-of",
        "json",
        str(video_path),
    ]


def validate_probe_payload(
    payload: dict[str, Any], config: VideoConfig, *, preview: bool
) -> tuple[list[str], list[str], dict[str, object]]:
    errors: list[str] = []
    warnings: list[str] = []
    streams = payload.get("streams", [])
    if not isinstance(streams, list):
        streams = []
    videos = [stream for stream in streams if stream.get("codec_type") == "video"]
    audios = [stream for stream in streams if stream.get("codec_type") == "audio"]
    if not videos:
        errors.append("video stream missing")
    if not audios:
        errors.append("audio stream missing")
    video = videos[0] if videos else {}
    audio = audios[0] if audios else {}
    if video.get("codec_name") != "h264":
        errors.append("video codec is not H.264")
    if audio.get("codec_name") != "aac":
        errors.append("audio codec is not AAC")
    if video.get("width") != config.width or video.get("height") != config.height:
        errors.append("video dimensions do not match 1920x1080 configuration")
    try:
        fps = float(Fraction(str(video.get("r_frame_rate", "0/1"))))
    except (ValueError, ZeroDivisionError):
        fps = 0.0
    if abs(fps - config.fps) > 0.01:
        errors.append(f"unexpected frame rate: {fps}")
    format_data = payload.get("format", {})
    if not isinstance(format_data, dict):
        format_data = {}
    try:
        duration = float(format_data.get("duration", 0))
        size = int(format_data.get("size", 0))
    except (TypeError, ValueError):
        duration, size = 0.0, 0
    if not config.min_duration_seconds <= duration <= config.max_duration_seconds:
        errors.append(f"duration outside expected range: {duration:.3f}s")
    if size < 200_000:
        errors.append(f"video file is implausibly small: {size} bytes")
    if preview:
        warnings.append("preview uses intentional silence; final AI voice is not present")
    metrics: dict[str, object] = {
        "duration_seconds": round(duration, 3),
        "size_bytes": size,
        "width": video.get("width", 0),
        "height": video.get("height", 0),
        "fps": round(fps, 3),
        "video_codec": video.get("codec_name", "missing"),
        "audio_codec": audio.get("codec_name", "missing"),
        "video_streams": len(videos),
        "audio_streams": len(audios),
    }
    return errors, warnings, metrics


def validate_video(video_path: Path, config: VideoConfig, *, preview: bool) -> dict[str, object]:
    if not video_path.exists():
        raise RuntimeError(f"video output does not exist: {video_path}")
    ffprobe = require_tool("ffprobe")
    ffmpeg = require_tool("ffmpeg")
    probe = run_checked(
        build_probe_command(ffprobe, video_path),
        cwd=REPO_ROOT,
        timeout=30,
        log_path=config.logs_dir / "video-qa-ffprobe.log",
    )
    payload = json.loads(probe.stdout)
    errors, warnings, metrics = validate_probe_payload(payload, config, preview=preview)
    run_checked(
        [ffmpeg, "-v", "error", "-i", str(video_path), "-f", "null", "-"],
        cwd=REPO_ROOT,
        timeout=300,
        log_path=config.logs_dir / "video-qa-decode.log",
    )
    sample_dir = config.tmp_dir / "qa-frames"
    sample_dir.mkdir(parents=True, exist_ok=True)
    duration_value = metrics.get("duration_seconds", 0.0)
    duration = float(duration_value) if isinstance(duration_value, int | float | str) else 0.0
    sample_paths: list[str] = []
    for index, ratio in enumerate((0.15, 0.5, 0.85), start=1):
        output = sample_dir / f"sample-{index}.png"
        run_checked(
            [
                ffmpeg,
                "-y",
                "-v",
                "error",
                "-ss",
                f"{duration * ratio:.3f}",
                "-i",
                str(video_path),
                "-frames:v",
                "1",
                str(output),
            ],
            cwd=REPO_ROOT,
            timeout=45,
        )
        if not output.exists() or output.stat().st_size < 10_000:
            errors.append(f"sample frame {index} is missing or implausibly small")
        sample_paths.append(str(output.relative_to(REPO_ROOT)))
    report: dict[str, object] = {
        "status": "passed" if not errors else "failed",
        "preview": preview,
        "path": str(video_path.relative_to(REPO_ROOT)),
        "metrics": metrics,
        "errors": errors,
        "warnings": warnings,
        "sample_frames": sample_paths,
        "full_decode": "passed",
    }
    report_path = config.dist_dir / "video_qa_report.json"
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if errors:
        raise RuntimeError("video quality gate failed: " + "; ".join(errors))
    return report
