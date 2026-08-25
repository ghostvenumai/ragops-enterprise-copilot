from __future__ import annotations

import json
import socket
import wave
from pathlib import Path

import pytest
from video.config import VideoConfig
from video.narration.tts import MissingCredentialError, generate_voice_assets
from video.qa.validator import validate_probe_payload
from video.recording.capture import _resolve_free_port, _validated_local_url
from video.subtitles.generator import generate_subtitles, srt_timestamp
from video.timeline import Scene, Timeline, load_timeline


def config_for(tmp_path: Path) -> VideoConfig:
    return VideoConfig(
        timeline_path=tmp_path / "timeline.json",
        tmp_dir=tmp_path / "tmp",
        logs_dir=tmp_path / "logs",
        dist_dir=tmp_path / "dist",
        tts_cache_dir=tmp_path / "cache",
    )


def short_timeline() -> Timeline:
    return Timeline(
        title="Test",
        language="de",
        scenes=(
            Scene(
                id="intro",
                order=1,
                action="Start",
                planned_duration=5,
                narration="Dies ist ein reproduzierbarer deutscher Testtext.",
                overlay="Test",
                capture="title:intro",
            ),
        ),
    )


class RecordingVoiceProvider:
    def __init__(self) -> None:
        self.calls = 0

    def synthesize(self, request: object, output_path: Path) -> None:
        self.calls += 1
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with wave.open(str(output_path), "wb") as audio:
            audio.setnchannels(1)
            audio.setsampwidth(2)
            audio.setframerate(8_000)
            audio.writeframes(b"\x00\x00" * 8_000)


def test_recording_url_is_restricted_to_local_http() -> None:
    assert _validated_local_url("http://127.0.0.1:8765/ready").endswith("/ready")
    with pytest.raises(ValueError, match="local HTTP"):
        _validated_local_url("https://example.com/ready")
    with pytest.raises(ValueError, match="local HTTP"):
        _validated_local_url("file:///tmp/report")


def test_resolve_free_port_keeps_preferred_port_when_available() -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        free_port = probe.getsockname()[1]

    assert _resolve_free_port(free_port, set()) == free_port


def test_resolve_free_port_falls_back_when_preferred_port_is_occupied() -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as blocker:
        blocker.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        blocker.bind(("127.0.0.1", 0))
        blocker.listen(1)
        occupied_port = blocker.getsockname()[1]

        resolved = _resolve_free_port(occupied_port, set())

        assert resolved != occupied_port


def test_resolve_free_port_avoids_already_taken_ports() -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        free_port = probe.getsockname()[1]

    assert _resolve_free_port(free_port, {free_port}) != free_port


def test_repository_timeline_is_valid_and_two_to_three_minutes() -> None:
    timeline_path = Path(__file__).resolve().parents[2] / "video/script/timeline.json"

    timeline = load_timeline(timeline_path)

    assert timeline.language == "de"
    assert 120 <= timeline.total_duration <= 180
    assert timeline.total_duration == 179


def test_timeline_rejects_unallowlisted_capture(tmp_path: Path) -> None:
    payload = {
        "title": "Test",
        "language": "de",
        "scenes": [
            {
                "id": "intro",
                "order": 1,
                "action": "Start",
                "planned_duration": 5,
                "narration": "Ein ausreichend langer Testtext.",
                "overlay": "Test",
                "capture": "shell:arbitrary",
            }
        ],
    }
    path = tmp_path / "timeline.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="unsupported capture mode"):
        load_timeline(path, min_duration=1, max_duration=10)


def test_subtitles_are_generated_from_the_same_timeline(tmp_path: Path) -> None:
    output = generate_subtitles(short_timeline(), tmp_path / "subtitles.srt")
    text = output.read_text(encoding="utf-8")

    assert "Dies ist ein reproduzierbarer" in text
    assert "00:00:00,500 -->" in text
    assert srt_timestamp(65.432) == "00:01:05,432"


def test_voice_assets_are_cached_by_content(tmp_path: Path) -> None:
    config = config_for(tmp_path)
    provider = RecordingVoiceProvider()

    first = generate_voice_assets(short_timeline(), config, provider)
    second = generate_voice_assets(short_timeline(), config, provider)

    assert first.assets == second.assets
    assert provider.calls == 1
    assert first.assets["intro"].exists()
    assert second.stats.api_requests == 0


def test_voice_generation_without_key_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    with pytest.raises(MissingCredentialError):
        generate_voice_assets(short_timeline(), config_for(tmp_path))


def test_probe_validation_accepts_required_video_contract(tmp_path: Path) -> None:
    payload = {
        "streams": [
            {
                "codec_type": "video",
                "codec_name": "h264",
                "width": 1920,
                "height": 1080,
                "r_frame_rate": "30/1",
            },
            {"codec_type": "audio", "codec_name": "aac"},
        ],
        "format": {"duration": "150.0", "size": "1000000"},
    }

    errors, warnings, metrics = validate_probe_payload(payload, config_for(tmp_path), preview=True)

    assert errors == []
    assert warnings == ["preview uses intentional silence; final AI voice is not present"]
    assert metrics["duration_seconds"] == 150.0
