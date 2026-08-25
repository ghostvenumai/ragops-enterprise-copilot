from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import cast

import pytest
from video.config import VideoConfig
from video.qa.narration import NarrationQualityError, validate_narration
from video.rendering.renderer import (
    build_final_command,
    build_segment_command,
    scene_render_durations,
)
from video.timeline import EvidenceReference, NarrationClaim, Scene, Timeline


def qa_config(tmp_path: Path) -> VideoConfig:
    return VideoConfig(
        timeline_path=tmp_path / "timeline.json",
        tmp_dir=tmp_path / "tmp",
        logs_dir=tmp_path / "logs",
        dist_dir=tmp_path / "dist",
        tts_cache_dir=tmp_path / "cache",
        min_duration_seconds=1,
        max_duration_seconds=30,
    )


def qa_timeline() -> Timeline:
    narration = "Die Pipeline erzeugt belegte Antworten und prüft jede Quelle."
    return Timeline(
        title="Narration QA",
        language="de",
        scenes=(
            Scene(
                id="intro",
                order=1,
                action="show_intro",
                planned_duration=8,
                narration=narration,
                overlay="TEST",
                capture="title:intro",
                claims=(
                    NarrationClaim(
                        statement=narration,
                        evidence=(
                            EvidenceReference(
                                path="src/ragops/workflows/state_machine.py",
                                contains="def _citation_validator",
                            ),
                        ),
                    ),
                ),
                visual_terms=("RAGOps Enterprise Copilot", "100 % synthetisch"),
            ),
        ),
    )


def capture_manifest(tmp_path: Path, timeline: Timeline) -> Path:
    path = tmp_path / "capture-manifest.json"
    scene = timeline.scenes[0]
    path.write_text(
        json.dumps(
            {
                "status": "passed",
                "scenes": {
                    scene.id: {
                        "status": "passed",
                        "matched_visual_terms": list(scene.visual_terms),
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    return path


def test_narration_gate_validates_code_demo_timing_and_claims(tmp_path: Path) -> None:
    timeline = qa_timeline()
    output = tmp_path / "narration-qa.json"

    report = validate_narration(
        timeline,
        qa_config(tmp_path),
        output,
        capture_manifest_path=capture_manifest(tmp_path, timeline),
    )

    assert report["status"] == "passed"
    assert report["gate_position"] == "before_tts_cache_and_provider"
    checks = cast(dict[str, bool], report["checks"])
    assert all(checks.values())
    assert json.loads(output.read_text(encoding="utf-8"))["status"] == "passed"


def test_narration_gate_fails_on_uncovered_or_invented_claim(tmp_path: Path) -> None:
    timeline = qa_timeline()
    bad_claim = NarrationClaim(
        statement=timeline.scenes[0].narration,
        evidence=(
            EvidenceReference(
                path="src/ragops/workflows/state_machine.py",
                contains="def feature_that_does_not_exist",
            ),
        ),
    )
    bad_scene = replace(timeline.scenes[0], claims=(bad_claim,))

    with pytest.raises(NarrationQualityError, match="NARRATION_QUALITY_GATE_FAILED"):
        validate_narration(
            replace(timeline, scenes=(bad_scene,)),
            qa_config(tmp_path),
            tmp_path / "failed-report.json",
            require_capture=False,
        )

    report = json.loads((tmp_path / "failed-report.json").read_text(encoding="utf-8"))
    assert report["status"] == "failed"
    assert report["checks"]["code_evidence_valid"] is False


def test_narration_gate_rejects_hype_and_scene_overrun(tmp_path: Path) -> None:
    timeline = qa_timeline()
    narration = (
        "Die revolutionäre Plattform ist perfekt und dieser absichtlich lange "
        "Sprechertext passt nicht in eine extrem kurze Szene."
    )
    scene = replace(
        timeline.scenes[0],
        planned_duration=3,
        narration=narration,
        claims=(
            NarrationClaim(
                statement=narration,
                evidence=timeline.scenes[0].claims[0].evidence,
            ),
        ),
    )

    with pytest.raises(NarrationQualityError):
        validate_narration(
            replace(timeline, scenes=(scene,)),
            qa_config(tmp_path),
            tmp_path / "timing-report.json",
            require_capture=False,
        )

    checks = json.loads((tmp_path / "timing-report.json").read_text(encoding="utf-8"))["checks"]
    assert checks["no_hype"] is False
    assert checks["timing_valid"] is False


def test_scene_duration_uses_complete_audio_and_pauses() -> None:
    timeline = qa_timeline()

    durations = scene_render_durations(timeline, {"intro": 6.25})

    assert durations == {"intro": 7.55}


def test_segment_command_delays_audio_instead_of_cutting_narration(tmp_path: Path) -> None:
    scene = qa_timeline().scenes[0]
    config = qa_config(tmp_path)

    command = build_segment_command(
        "ffmpeg",
        scene,
        tmp_path / "scene.png",
        tmp_path / "voice.wav",
        tmp_path / "scene.mp4",
        6.25,
        7.55,
        config,
    )
    filter_graph = command[command.index("-filter_complex") + 1]

    assert "atrim=0:6.250" in filter_graph
    assert "adelay=500:all=1" in filter_graph
    assert "atrim=0:7.550" in filter_graph
    assert command[command.index("-t") + 1] == "7.550"


def test_final_command_uses_soft_transitions_without_default_subtitles(
    tmp_path: Path,
) -> None:
    config = qa_config(tmp_path)
    segments = [tmp_path / "one.mp4", tmp_path / "two.mp4"]

    command = build_final_command(
        "ffmpeg",
        segments,
        [7.55, 8.0],
        None,
        tmp_path / "final.mp4",
        config,
    )
    filter_graph = command[command.index("-filter_complex") + 1]

    assert "xfade=transition=fade" in filter_graph
    assert "acrossfade" in filter_graph
    assert "subtitles=" not in filter_graph
    assert config.burn_subtitles is False
