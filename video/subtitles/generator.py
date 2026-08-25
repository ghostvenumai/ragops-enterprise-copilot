"""Generate non-overlapping SRT subtitles from scene narration."""

from __future__ import annotations

import textwrap
from pathlib import Path

from video.timeline import Timeline


def srt_timestamp(seconds: float) -> str:
    milliseconds = max(0, round(seconds * 1000))
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    secs, millis = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def _caption_chunks(text: str, words_per_caption: int = 11) -> list[str]:
    words = text.split()
    chunks = [
        " ".join(words[index : index + words_per_caption])
        for index in range(0, len(words), words_per_caption)
    ]
    return ["\n".join(textwrap.wrap(chunk, width=46)) for chunk in chunks]


def generate_subtitles(
    timeline: Timeline,
    output_path: Path,
    *,
    speech_durations: dict[str, float] | None = None,
    transition_seconds: float = 0.0,
) -> Path:
    entries: list[str] = []
    cursor = 0.0
    sequence = 1
    previous_end = 0.0
    for scene_index, scene in enumerate(timeline.scenes):
        chunks = _caption_chunks(scene.narration)
        speech_start = cursor + scene.pause_before
        speech_duration = (
            speech_durations[scene.id] if speech_durations else scene.narration_duration
        )
        slot = speech_duration / len(chunks)
        for index, caption in enumerate(chunks):
            start = max(previous_end, speech_start + index * slot)
            end = speech_start + (index + 1) * slot
            entries.append(
                f"{sequence}\n{srt_timestamp(start)} --> {srt_timestamp(end)}\n{caption}\n"
            )
            previous_end = end
            sequence += 1
        cursor += scene.pause_before + speech_duration + scene.pause_after
        if scene_index < len(timeline.scenes) - 1:
            cursor -= transition_seconds
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("\n".join(entries).rstrip() + "\n", encoding="utf-8")
    return output_path
