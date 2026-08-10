"""Construct and run maintainable FFmpeg commands for the demo video."""

from __future__ import annotations

from pathlib import Path

from video.config import REPO_ROOT, VideoConfig
from video.timeline import Scene, Timeline
from video.tools import require_tool, run_checked


def _drawtext_value(value: str) -> str:
    return value.replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")


def build_segment_command(
    ffmpeg: str,
    scene: Scene,
    image_path: Path,
    audio_path: Path,
    output_path: Path,
    config: VideoConfig,
) -> list[str]:
    overlay = _drawtext_value(scene.overlay)
    video_filter = (
        f"scale={config.width}:{config.height}:force_original_aspect_ratio=decrease,"
        f"pad={config.width}:{config.height}:(ow-iw)/2:(oh-ih)/2:#101722,"
        "format=yuv420p,"
        "drawbox=x=74:y=62:w=610:h=62:color=#101722CC:t=fill,"
        f"drawtext=font='DejaVu Sans':text='{overlay}':x=98:y=80:"
        "fontsize=22:fontcolor=white"
    )
    return [
        ffmpeg,
        "-y",
        "-v",
        "error",
        "-loop",
        "1",
        "-framerate",
        str(config.fps),
        "-i",
        str(image_path),
        "-i",
        str(audio_path),
        "-filter_complex",
        f"[0:v]{video_filter}[v];[1:a]apad,atrim=0:{scene.planned_duration},aresample=48000[a]",
        "-map",
        "[v]",
        "-map",
        "[a]",
        "-t",
        str(scene.planned_duration),
        "-r",
        str(config.fps),
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "20",
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        str(output_path),
    ]


def _subtitle_filter(path: Path) -> str:
    escaped = str(path).replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")
    style = (
        "FontName=DejaVu Sans,FontSize=12,PrimaryColour=&H00FFFFFF,"
        "OutlineColour=&H00101722,BorderStyle=3,Outline=1,Shadow=0,"
        "BackColour=&HA0101722,MarginV=42,Alignment=2"
    )
    return f"subtitles='{escaped}':force_style='{style}'"


def render_video(
    timeline: Timeline,
    images: dict[str, Path],
    audio: dict[str, Path],
    subtitles: Path,
    config: VideoConfig,
    *,
    preview: bool,
) -> Path:
    ffmpeg = require_tool("ffmpeg")
    segment_dir = config.tmp_dir / "segments"
    segment_dir.mkdir(parents=True, exist_ok=True)
    segment_paths: list[Path] = []
    for scene in timeline.scenes:
        output = segment_dir / f"{scene.order:03d}_{scene.id}.mp4"
        run_checked(
            build_segment_command(ffmpeg, scene, images[scene.id], audio[scene.id], output, config),
            cwd=REPO_ROOT,
            timeout=180,
            log_path=config.logs_dir / f"render-{scene.order:03d}-{scene.id}.log",
        )
        segment_paths.append(output)
    concat_path = config.tmp_dir / "segments.txt"
    concat_path.write_text(
        "".join(f"file '{path.as_posix()}'\n" for path in segment_paths), encoding="utf-8"
    )
    output = config.dist_dir / ("solcom_demo_preview.mp4" if preview else "solcom_demo.mp4")
    run_checked(
        [
            ffmpeg,
            "-y",
            "-v",
            "error",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(concat_path),
            "-vf",
            _subtitle_filter(subtitles),
            "-r",
            str(config.fps),
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "20",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-movflags",
            "+faststart",
            str(output),
        ],
        cwd=REPO_ROOT,
        timeout=600,
        log_path=config.logs_dir / "render-final.log",
    )
    return output
