"""Construct and run maintainable FFmpeg commands for the demo video."""

from __future__ import annotations

from pathlib import Path

from video.config import REPO_ROOT, VideoConfig
from video.narration.cache import AudioValidator
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
    audio_duration: float,
    segment_duration: float,
    config: VideoConfig,
) -> list[str]:
    overlay = _drawtext_value(scene.overlay)
    delay_ms = round(scene.pause_before * 1000)
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
        (
            f"[0:v]{video_filter}[v];"
            f"[1:a]atrim=0:{audio_duration:.3f},adelay={delay_ms}:all=1,"
            f"apad,atrim=0:{segment_duration:.3f},aresample=48000[a]"
        ),
        "-map",
        "[v]",
        "-map",
        "[a]",
        "-t",
        f"{segment_duration:.3f}",
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


def measure_audio_durations(
    timeline: Timeline,
    audio: dict[str, Path],
    config: VideoConfig,
    *,
    preview: bool,
) -> dict[str, float]:
    validator = AudioValidator(min_size_bytes=config.tts_min_audio_bytes)
    durations: dict[str, float] = {}
    for scene in timeline.scenes:
        if preview:
            durations[scene.id] = scene.narration_duration
            continue
        path = audio[scene.id]
        expected_format = path.suffix.lower().lstrip(".")
        metadata = validator.validate(path, expected_format)
        durations[scene.id] = metadata.duration_seconds
    return durations


def scene_render_durations(
    timeline: Timeline,
    speech_durations: dict[str, float],
) -> dict[str, float]:
    return {
        scene.id: round(
            scene.pause_before + speech_durations[scene.id] + scene.pause_after,
            3,
        )
        for scene in timeline.scenes
    }


def build_final_command(
    ffmpeg: str,
    segment_paths: list[Path],
    segment_durations: list[float],
    subtitles: Path | None,
    output: Path,
    config: VideoConfig,
) -> list[str]:
    if len(segment_paths) != len(segment_durations) or not segment_paths:
        raise ValueError("segments and durations must be non-empty and aligned")
    command = [ffmpeg, "-y", "-v", "error"]
    for path in segment_paths:
        command.extend(["-i", str(path)])

    filters: list[str] = []
    video_label = "0:v"
    audio_label = "0:a"
    accumulated = segment_durations[0]
    transition = min(config.transition_seconds, min(segment_durations) / 4)
    for index, duration in enumerate(segment_durations[1:], start=1):
        video_output = f"v{index}"
        audio_output = f"a{index}"
        offset = accumulated - transition
        filters.append(
            f"[{video_label}][{index}:v]xfade=transition=fade:"
            f"duration={transition:.3f}:offset={offset:.3f}[{video_output}]"
        )
        filters.append(
            f"[{audio_label}][{index}:a]acrossfade=d={transition:.3f}:c1=tri:c2=tri[{audio_output}]"
        )
        video_label = video_output
        audio_label = audio_output
        accumulated += duration - transition

    if len(segment_paths) == 1:
        filters.extend(["[0:v]null[vbase]", "[0:a]anull[abase]"])
        video_label = "vbase"
        audio_label = "abase"
    if subtitles is not None:
        filters.append(f"[{video_label}]{_subtitle_filter(subtitles)}[vout]")
        video_label = "vout"

    command.extend(
        [
            "-filter_complex",
            ";".join(filters),
            "-map",
            f"[{video_label}]",
            "-map",
            f"[{audio_label}]",
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
        ]
    )
    return command


def render_video(
    timeline: Timeline,
    images: dict[str, Path],
    audio: dict[str, Path],
    subtitles: Path | None,
    config: VideoConfig,
    *,
    preview: bool,
) -> Path:
    ffmpeg = require_tool("ffmpeg")
    speech_durations = measure_audio_durations(timeline, audio, config, preview=preview)
    durations = scene_render_durations(timeline, speech_durations)
    segment_dir = config.tmp_dir / "segments"
    segment_dir.mkdir(parents=True, exist_ok=True)
    segment_paths: list[Path] = []
    for scene in timeline.scenes:
        output = segment_dir / f"{scene.order:03d}_{scene.id}.mp4"
        run_checked(
            build_segment_command(
                ffmpeg,
                scene,
                images[scene.id],
                audio[scene.id],
                output,
                speech_durations[scene.id],
                durations[scene.id],
                config,
            ),
            cwd=REPO_ROOT,
            timeout=180,
            log_path=config.logs_dir / f"render-{scene.order:03d}-{scene.id}.log",
        )
        segment_paths.append(output)
    output = config.dist_dir / ("solcom_demo_preview.mp4" if preview else "solcom_demo.mp4")
    run_checked(
        build_final_command(
            ffmpeg,
            segment_paths,
            [durations[scene.id] for scene in timeline.scenes],
            subtitles if config.burn_subtitles else None,
            output,
            config,
        ),
        cwd=REPO_ROOT,
        timeout=600,
        log_path=config.logs_dir / "render-final.log",
    )
    return output
