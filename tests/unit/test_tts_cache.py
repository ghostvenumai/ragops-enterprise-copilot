from __future__ import annotations

import json
import wave
from dataclasses import asdict, replace
from pathlib import Path

import pytest
from video.config import VideoConfig
from video.narration.tts import (
    CacheOnlyMissError,
    MissingCredentialError,
    RetryableTTSProviderError,
    TTSRequest,
    TTSSafetyLimitError,
    generate_voice_assets,
    plan_voice_assets,
    redact_secrets,
)
from video.timeline import Scene, Timeline


def make_config(tmp_path: Path, **changes: object) -> VideoConfig:
    config = VideoConfig(
        timeline_path=tmp_path / "timeline.json",
        tmp_dir=tmp_path / "tmp",
        logs_dir=tmp_path / "logs",
        dist_dir=tmp_path / "dist",
        tts_cache_dir=tmp_path / "cache" / "tts",
    )
    return replace(config, **changes)


def make_timeline(count: int = 8, changed_scene: int | None = None) -> Timeline:
    scenes = []
    for index in range(1, count + 1):
        narration = f"Synthetischer deutscher Sprechertext fuer Szene {index}."
        if changed_scene == index:
            narration += " Dieser Satz wurde kontrolliert geaendert."
        scenes.append(
            Scene(
                id=f"scene-{index}",
                order=index,
                action="test",
                planned_duration=5,
                narration=narration,
                overlay=f"Szene {index}",
                capture="title:intro",
            )
        )
    return Timeline(title="TTS Cache Test", language="de", scenes=tuple(scenes))


class FakeTTSProvider:
    def __init__(self, transient_failures: int = 0) -> None:
        self.calls = 0
        self.requests: list[TTSRequest] = []
        self.transient_failures = transient_failures

    def synthesize(self, request: TTSRequest, output_path: Path) -> None:
        self.calls += 1
        self.requests.append(request)
        if self.calls <= self.transient_failures:
            raise RetryableTTSProviderError("synthetic transient failure")
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with wave.open(str(output_path), "wb") as audio:
            audio.setnchannels(1)
            audio.setsampwidth(2)
            audio.setframerate(8_000)
            audio.writeframes(b"\x00\x00" * 8_000)


def test_five_build_cost_control_scenarios(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    config = make_config(tmp_path)
    provider = FakeTTSProvider()
    original = make_timeline()

    build_1 = generate_voice_assets(original, config, provider)
    assert build_1.stats.api_requests == 8
    assert build_1.stats.cache_misses == 8

    build_2 = generate_voice_assets(original, config)
    assert build_2.stats.api_requests == 0
    assert build_2.stats.cache_hits == 8

    changed = make_timeline(changed_scene=4)
    build_3 = generate_voice_assets(changed, config, provider)
    assert build_3.stats.api_requests == 1
    assert build_3.stats.cache_hits == 7

    video_only_config = replace(config, width=1280, height=720, fps=24)
    build_4 = generate_voice_assets(changed, video_only_config)
    assert build_4.stats.api_requests == 0
    assert build_4.stats.cache_hits == 8

    build_5 = generate_voice_assets(changed, config)
    assert build_5.stats.api_requests == 0
    assert all(path.exists() for path in build_5.assets.values())
    assert provider.calls == 9


def test_duplicate_segment_hashes_require_one_api_call(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    provider = FakeTTSProvider()
    base = make_timeline(count=2)
    duplicate = Timeline(
        title=base.title,
        language=base.language,
        scenes=(
            base.scenes[0],
            replace(base.scenes[1], narration=base.scenes[0].narration),
        ),
    )

    first = generate_voice_assets(duplicate, config, provider)
    forced = generate_voice_assets(duplicate, config, provider, force_tts=True)

    assert first.stats.cache_misses == 2
    assert first.stats.api_calls_needed == 1
    assert first.stats.api_requests == 1
    assert first.assets["scene-1"] == first.assets["scene-2"]
    assert forced.stats.api_calls_needed == 1
    assert forced.stats.api_requests == 1
    assert provider.calls == 2


def test_corrupt_cache_regenerates_only_affected_segment(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    provider = FakeTTSProvider()
    timeline = make_timeline(count=3)
    first = generate_voice_assets(timeline, config, provider)

    first.assets["scene-2"].write_bytes(b"corrupt")
    second = generate_voice_assets(timeline, config, provider)

    assert second.stats.corrupt_entries == 1
    assert second.stats.api_requests == 1
    assert second.stats.cache_hits == 2
    assert provider.calls == 4


def test_changed_voice_and_text_create_targeted_cache_misses(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    provider = FakeTTSProvider()
    timeline = make_timeline(count=1)
    generate_voice_assets(timeline, config, provider)

    changed_voice = generate_voice_assets(
        timeline, replace(config, tts_voice="alloy"), provider
    )
    changed_text = generate_voice_assets(
        make_timeline(count=1, changed_scene=1), config, provider
    )

    assert changed_voice.stats.api_requests == 1
    assert changed_text.stats.api_requests == 1
    assert provider.calls == 3


def test_dry_run_and_cache_only_never_call_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    config = make_config(tmp_path)
    provider = FakeTTSProvider()

    dry_run = generate_voice_assets(make_timeline(count=2), config, provider, dry_run=True)
    assert dry_run.stats.status == "DRY_RUN"
    assert dry_run.stats.api_requests == 0
    assert provider.calls == 0

    with pytest.raises(CacheOnlyMissError) as caught:
        generate_voice_assets(make_timeline(count=2), config, provider, cache_only=True)
    assert caught.value.stats is not None
    assert caught.value.stats.api_requests == 0
    assert provider.calls == 0


def test_missing_key_requires_it_only_for_cache_misses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    config = make_config(tmp_path)
    timeline = make_timeline(count=1)

    with pytest.raises(MissingCredentialError) as caught:
        generate_voice_assets(timeline, config)
    assert caught.value.stats is not None
    assert caught.value.stats.status == "BLOCKED_EXTERNAL_CREDENTIAL"
    assert caught.value.stats.api_requests == 0

    provider = FakeTTSProvider()
    generate_voice_assets(timeline, config, provider)
    cached = generate_voice_assets(timeline, config)
    assert cached.stats.api_requests == 0
    assert cached.stats.cache_hits == 1


def test_api_call_safety_limit_blocks_before_provider_call(tmp_path: Path) -> None:
    config = make_config(tmp_path, tts_max_api_calls_per_build=2)
    provider = FakeTTSProvider()

    with pytest.raises(TTSSafetyLimitError, match="TTS_SAFETY_LIMIT_REACHED"):
        generate_voice_assets(make_timeline(count=3), config, provider)
    assert provider.calls == 0


def test_retries_are_bounded_and_success_is_cached(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("video.narration.tts.time.sleep", lambda _: None)
    config = make_config(tmp_path, tts_max_retries=2)
    provider = FakeTTSProvider(transient_failures=2)

    result = generate_voice_assets(make_timeline(count=1), config, provider)

    assert result.stats.api_requests == 3
    assert provider.calls == 3
    cached = generate_voice_assets(make_timeline(count=1), config)
    assert cached.stats.api_requests == 0


def test_force_tts_is_explicit_and_bypasses_valid_cache(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    provider = FakeTTSProvider()
    timeline = make_timeline(count=1)
    generate_voice_assets(timeline, config, provider)

    forced = generate_voice_assets(timeline, config, provider, force_tts=True)

    assert forced.stats.api_requests == 1
    assert forced.stats.cache_hits == 0
    assert provider.calls == 2


def test_manifest_contains_metadata_but_not_narration_or_secrets(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    provider = FakeTTSProvider()
    timeline = make_timeline(count=1)
    generate_voice_assets(timeline, config, provider)

    manifest_text = (config.tts_cache_dir / "manifest.json").read_text(encoding="utf-8")
    manifest = json.loads(manifest_text)

    assert manifest["cache_schema_version"] == 1
    assert len(manifest["entries"]) == 1
    assert timeline.scenes[0].narration not in manifest_text
    assert "OPENAI_API_KEY" not in manifest_text
    assert not list(config.tts_cache_dir.glob("*.partial-*"))


def test_cache_material_excludes_api_key_and_redacts_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    key = "sk-" + "synthetic"
    monkeypatch.setenv("OPENAI_API_KEY", key)
    plan = plan_voice_assets(make_timeline(count=1), make_config(tmp_path))
    item = plan.items[0]
    payload = {"cache_key": item.cache_key, "request": asdict(item.request)}

    assert key not in json.dumps(payload)
    message = f"Author" f"ization: Bearer {key}; provider rejected {key}"
    redacted = redact_secrets(message, key)
    assert key not in redacted
    assert redacted.count("[REDACTED]") == 2
