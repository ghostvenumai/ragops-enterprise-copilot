"""Generate reviewable narration text from the validated timeline."""

from __future__ import annotations

from pathlib import Path

from video.timeline import Timeline


def generate_narration(timeline: Timeline, output_path: Path) -> Path:
    lines = [f"# Sprechertext: {timeline.title}", "", f"Sprache: `{timeline.language}`", ""]
    for scene in timeline.scenes:
        lines.extend(
            [
                f"## {scene.order:02d} · {scene.overlay}",
                "",
                scene.narration,
                "",
                f"Geplante Szenendauer: {scene.planned_duration:.1f} Sekunden",
                "",
            ]
        )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    return output_path
