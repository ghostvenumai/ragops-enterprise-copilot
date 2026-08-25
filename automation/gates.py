"""Fixed application, security, demo, and video gates for the master loop."""

from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from video.config import REPO_ROOT, VideoConfig
from video.narration.generator import generate_narration
from video.narration.tts import (
    MissingCredentialError,
    generate_voice_assets,
    plan_voice_assets,
)
from video.pipeline import video_precheck
from video.qa.narration import validate_narration
from video.qa.validator import validate_video
from video.recording.capture import capture_scenes
from video.rendering.renderer import measure_audio_durations, render_video
from video.subtitles.generator import generate_subtitles
from video.timeline import Timeline, load_timeline
from video.tools import require_tool

from automation.retry import RetryPolicy
from automation.state import ErrorCategory, LoopState, Phase
from ragops.demo.controller import DemoController


@dataclass(frozen=True)
class GateResult:
    status: str
    detail: str
    category: ErrorCategory | None = None


class GateFailure(RuntimeError):
    pass


def _run_commands(
    phase: Phase, commands: list[list[str]], policy: RetryPolicy, config: VideoConfig
) -> None:
    output: list[str] = []
    for command in commands:
        completed = subprocess.run(  # noqa: S603 - only fixed command lists are passed.
            command,
            cwd=REPO_ROOT,
            text=True,
            capture_output=True,
            check=False,
            timeout=policy.command_timeout_seconds,
        )
        output.extend([completed.stdout, completed.stderr, f"returncode={completed.returncode}"])
        if completed.returncode:
            break
    path = config.logs_dir / f"master-{phase.value.lower()}.log"
    path.write_text("\n".join(output), encoding="utf-8")
    if completed.returncode:
        raise GateFailure(f"{phase.value} failed with return code {completed.returncode}")


def _timeline(config: VideoConfig) -> Timeline:
    return load_timeline(
        config.timeline_path,
        min_duration=config.min_duration_seconds,
        max_duration=config.max_duration_seconds,
    )


def _asset_maps(
    timeline: Timeline, config: VideoConfig, *, preview: bool
) -> tuple[dict[str, Path], dict[str, Path]]:
    images = {
        scene.id: config.tmp_dir / "screenshots" / f"{scene.order:03d}_{scene.id}.png"
        for scene in timeline.scenes
    }
    if preview:
        audio_dir = config.tmp_dir / "audio-preview"
        audio = {
            scene.id: audio_dir / f"{scene.order:03d}_{scene.id}.wav" for scene in timeline.scenes
        }
    else:
        voice_plan = plan_voice_assets(timeline, config)
        missing_voice = [item.scene_id for item in voice_plan.items if item.status != "HIT"]
        if missing_voice:
            raise GateFailure("validated TTS cache entries missing: " + ", ".join(missing_voice))
        audio = {item.scene_id: item.path for item in voice_plan.items}
    missing = [str(path) for path in (*images.values(), *audio.values()) if not path.exists()]
    if missing:
        raise GateFailure("required media assets missing: " + ", ".join(missing[:3]))
    return images, audio


def execute_phase(
    phase: Phase, state: LoopState, policy: RetryPolicy, config: VideoConfig
) -> GateResult:
    timeline = _timeline(config)
    python = sys.executable
    if phase is Phase.DISCOVER:
        required = ["pyproject.toml", "AGENTS.md", "apps/api/main.py", "tests", "data/synthetic"]
        missing = [item for item in required if not (REPO_ROOT / item).exists()]
        if missing:
            raise GateFailure("repository discovery missing: " + ", ".join(missing))
        return GateResult("passed", "Repository und Entry Points erkannt")
    if phase is Phase.PRECHECK:
        for tool in ("git", "ffmpeg", "ffprobe", "google-chrome"):
            require_tool(tool)
        if sys.version_info < (3, 12) or sys.prefix == sys.base_prefix:
            raise GateFailure("Python 3.12 project virtual environment is required")
        return GateResult("passed", "Python, Venv und Medienwerkzeuge verfügbar")
    if phase is Phase.PLAN:
        return GateResult("passed", f"Timeline validiert: {timeline.total_duration:.1f} Sekunden")
    if phase is Phase.IMPLEMENT:
        required = [
            "automation/run_loop.py",
            "video/pipeline.py",
            "video/script/timeline.json",
            "src/ragops/demo/controller.py",
        ]
        missing = [item for item in required if not (REPO_ROOT / item).exists()]
        if missing:
            raise GateFailure("implementation incomplete: " + ", ".join(missing))
        return GateResult("passed", "Anwendung, Demo und Video-Komponenten vorhanden")
    if phase is Phase.STATIC_CHECK:
        _run_commands(
            phase,
            [
                [python, "scripts/verify.py", "--only", "lint"],
                [python, "scripts/verify.py", "--only", "typecheck"],
            ],
            policy,
            config,
        )
        return GateResult("passed", "Ruff und MyPy bestanden")
    if phase is Phase.UNIT_TEST:
        _run_commands(phase, [[python, "-m", "pytest", "-q", "tests/unit"]], policy, config)
        return GateResult("passed", "Unit-Tests bestanden")
    if phase is Phase.INTEGRATION_TEST:
        _run_commands(
            phase,
            [[python, "-m", "pytest", "-q", "tests/integration", "tests/evaluation"]],
            policy,
            config,
        )
        return GateResult("passed", "Integrations- und Evaluationstests bestanden")
    if phase is Phase.SECURITY_CHECK:
        _run_commands(
            phase,
            [
                [python, "-m", "pytest", "-q", "tests/security"],
                [python, "scripts/verify.py", "--only", "security"],
            ],
            policy,
            config,
        )
        return GateResult("passed", "Security-Tests und Scans bestanden")
    if phase in {Phase.APPLICATION_QA, Phase.DEMO_RUN}:
        result = DemoController(
            REPO_ROOT / "data/synthetic", config.tmp_dir / phase.value.lower()
        ).run()
        state.artifacts[phase.value.lower()] = result.export_path
        return GateResult("passed", f"Demo mit {result.document_count} Dokumenten bestanden")
    if phase is Phase.DEMO_PRECHECK:
        video_precheck(config, timeline)
        validate_narration(
            timeline,
            config,
            config.tmp_dir / "narration-qa-precheck.json",
            require_capture=False,
        )
        state.artifacts["narration_qa_precheck"] = "video/tmp/narration-qa-precheck.json"
        voice = generate_voice_assets(timeline, config, dry_run=True)
        state.artifacts["tts_cache_report"] = "video/tmp/tts-cache-report.json"
        return GateResult(
            "passed",
            "Video-, Timeline- und TTS-Cache-Precheck bestanden: "
            f"{voice.stats.cache_hits} Hits, {voice.stats.cache_misses} Misses",
        )
    if phase is Phase.RECORD:
        assets = capture_scenes(timeline, config)
        state.artifacts["recording"] = str(config.tmp_dir / "screenshots")
        return GateResult("passed", f"{len(assets)} reale GUI-Szenen aufgenommen")
    if phase is Phase.GENERATE_NARRATION:
        path = generate_narration(timeline, REPO_ROOT / "video/script/narration.md")
        state.artifacts["narration"] = str(path.relative_to(REPO_ROOT))
        return GateResult("passed", "Deutscher Sprechertext erzeugt")
    if phase is Phase.VERIFY_NARRATION:
        report_path = config.dist_dir / "narration_qa_report.json"
        report = validate_narration(
            timeline,
            config,
            report_path,
            capture_manifest_path=config.tmp_dir / "capture-manifest.json",
            require_capture=True,
        )
        state.artifacts["narration_qa"] = str(report_path.relative_to(REPO_ROOT))
        return GateResult("passed", f"Narration-QA {report['status']} vor TTS")
    if phase is Phase.GENERATE_VOICE:
        try:
            voice = generate_voice_assets(timeline, config)
            state.artifacts["voice_mode"] = "openai"
            state.artifacts["tts_cache_report"] = "video/tmp/tts-cache-report.json"
            state.artifacts["tts_api_requests"] = str(voice.stats.api_requests)
            return GateResult(
                "passed",
                f"OpenAI Voiceover cache-first; API-Aufrufe: {voice.stats.api_requests}",
            )
        except MissingCredentialError as exc:
            state.artifacts["voice_mode"] = "blocked"
            state.artifacts["tts_cache_report"] = "video/tmp/tts-cache-report.json"
            if exc.stats:
                state.artifacts["tts_api_requests"] = str(exc.stats.api_requests)
            return GateResult(
                "blocked",
                "OPENAI_API_KEY fehlt fuer nicht gecachte Voice-Segmente; kein Ersatz-Audio",
                ErrorCategory.EXTERNAL_CREDENTIAL_MISSING,
            )
    if phase is Phase.GENERATE_SUBTITLES:
        speech_durations: dict[str, float] | None = None
        if state.artifacts.get("voice_mode") == "openai":
            _, audio = _asset_maps(timeline, config, preview=False)
            speech_durations = measure_audio_durations(timeline, audio, config, preview=False)
        path = generate_subtitles(
            timeline,
            config.tmp_dir / "subtitles.srt",
            speech_durations=speech_durations,
            transition_seconds=(config.transition_seconds if speech_durations is not None else 0.0),
        )
        state.artifacts["subtitles"] = str(path.relative_to(REPO_ROOT))
        return GateResult("passed", "Synchronisiertes SRT-Sidecar erzeugt")
    if phase is Phase.RENDER:
        if state.artifacts.get("voice_mode") == "blocked":
            return GateResult(
                "blocked",
                "Rendering wartet auf validierten TTS-Cache oder OPENAI_API_KEY",
                ErrorCategory.EXTERNAL_CREDENTIAL_MISSING,
            )
        preview = state.artifacts.get("voice_mode") != "openai"
        images, audio = _asset_maps(timeline, config, preview=preview)
        output = render_video(
            timeline,
            images,
            audio,
            config.tmp_dir / "subtitles.srt",
            config,
            preview=preview,
        )
        state.artifacts["video"] = str(output.relative_to(REPO_ROOT))
        state.artifacts["video_mode"] = "preview" if preview else "final"
        return GateResult("passed", f"H.264/AAC-Video gerendert: {output.name}")
    if phase is Phase.VIDEO_QA:
        if state.artifacts.get("voice_mode") == "blocked":
            return GateResult(
                "blocked",
                "Video-QA wartet auf ein mit Voiceover gerendertes Video",
                ErrorCategory.EXTERNAL_CREDENTIAL_MISSING,
            )
        path = REPO_ROOT / state.artifacts.get("video", "")
        report = validate_video(
            path, config, preview=state.artifacts.get("video_mode") == "preview"
        )
        state.artifacts["video_qa"] = "dist/video_qa_report.json"
        return GateResult("passed", f"Video-QA {report['status']}")
    if phase is Phase.FINAL_VERIFY:
        _run_commands(phase, [[python, "scripts/verify.py"]], policy, config)
        return GateResult("passed", "Vollständige Quality Gates bestanden")
    return GateResult("passed", "Alle erreichbaren Phasen abgeschlossen")
