"""Fail-closed narration quality gate executed before any TTS request."""

from __future__ import annotations

import json
import re
import unicodedata
from pathlib import Path
from typing import Any

from video.config import REPO_ROOT, VideoConfig
from video.timeline import Timeline

SENTENCE_BOUNDARY = re.compile(r"(?<=[.!?])\s+")
GERMAN_MARKERS = {
    "als",
    "aus",
    "das",
    "der",
    "die",
    "durch",
    "eine",
    "einen",
    "für",
    "ist",
    "mit",
    "nicht",
    "nur",
    "und",
    "werden",
    "wird",
    "zur",
}
HYPE_TERMS = {
    "bahnbrechend",
    "fehlerfrei",
    "perfekt",
    "revolutionär",
    "unschlagbar",
    "weltbeste",
    "weltweit führend",
    "world-leading",
}
UNIMPLEMENTED_TECHNOLOGIES = {
    "kafka",
    "kubernetes",
    "langchain",
    "langgraph",
    "pinecone",
    "weaviate",
}
MAX_EVIDENCE_FILE_BYTES = 2_000_000
ESTIMATED_CHARACTERS_PER_SECOND = 12.0
ESTIMATED_WORDS_PER_SECOND = 1.4
TIMING_TOLERANCE_SECONDS = 0.25


class NarrationQualityError(RuntimeError):
    """Raised when narration is not eligible for cache lookup or TTS."""


def _normalize(text: str) -> str:
    return " ".join(unicodedata.normalize("NFC", text).split())


def estimate_speech_duration(text: str, speaking_speed: float = 1.0) -> float:
    normalized = _normalize(text)
    character_estimate = len(normalized) / ESTIMATED_CHARACTERS_PER_SECOND
    word_estimate = len(normalized.split()) / ESTIMATED_WORDS_PER_SECOND
    return round(max(character_estimate, word_estimate) / speaking_speed, 3)


def _sentences(text: str) -> tuple[str, ...]:
    return tuple(
        normalized
        for item in SENTENCE_BOUNDARY.split(_normalize(text))
        if (normalized := _normalize(item))
    )


def _atomic_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _load_capture_manifest(path: Path | None) -> dict[str, Any]:
    if path is None or not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise NarrationQualityError("capture manifest is unreadable") from exc
    if not isinstance(payload, dict):
        raise NarrationQualityError("capture manifest must be a JSON object")
    return payload


def _evidence_path(relative_path: str) -> Path:
    candidate = (REPO_ROOT / relative_path).resolve()
    if not candidate.is_relative_to(REPO_ROOT) or not candidate.is_file() or candidate.is_symlink():
        raise NarrationQualityError(f"unsafe or missing evidence path: {relative_path}")
    if candidate.stat().st_size > MAX_EVIDENCE_FILE_BYTES:
        raise NarrationQualityError(f"evidence file exceeds size limit: {relative_path}")
    return candidate


def validate_narration(
    timeline: Timeline,
    config: VideoConfig,
    output_path: Path,
    *,
    capture_manifest_path: Path | None = None,
    require_capture: bool = True,
) -> dict[str, object]:
    """Validate every narration sentence before cache lookup or provider use."""
    errors: list[str] = []
    scene_reports: list[dict[str, object]] = []
    capture_manifest = _load_capture_manifest(capture_manifest_path)
    capture_scenes = capture_manifest.get("scenes", {})
    if not isinstance(capture_scenes, dict):
        capture_scenes = {}

    claims_complete = True
    sentences_covered = True
    code_evidence_valid = True
    demo_alignment_valid = True
    timing_valid = True
    language_valid = timeline.language == config.language == "de"
    no_hype = True
    technical_terms_valid = True
    total_estimated = 0.0
    claim_count = 0
    evidence_count = 0

    for scene in timeline.scenes:
        scene_errors: list[str] = []
        sentences = _sentences(scene.narration)
        claim_statements = {_normalize(claim.statement) for claim in scene.claims}
        claim_count += len(scene.claims)

        if not scene.claims:
            claims_complete = False
            scene_errors.append("keine maschinenlesbaren Claims")
        uncovered = [sentence for sentence in sentences if sentence not in claim_statements]
        extra_claims = [
            statement for statement in claim_statements if statement not in set(sentences)
        ]
        if uncovered or extra_claims:
            sentences_covered = False
            if uncovered:
                scene_errors.append(f"{len(uncovered)} Narrationssatz/-sätze ohne Claim")
            if extra_claims:
                scene_errors.append(f"{len(extra_claims)} Claim(s) nicht in Narration")

        for claim in scene.claims:
            if not claim.evidence:
                code_evidence_valid = False
                scene_errors.append("Claim ohne Code-Beleg")
            for reference in claim.evidence:
                evidence_count += 1
                try:
                    evidence_path = _evidence_path(reference.path)
                    evidence_text = evidence_path.read_text(encoding="utf-8")
                except (NarrationQualityError, OSError, UnicodeError) as exc:
                    code_evidence_valid = False
                    scene_errors.append(str(exc))
                    continue
                if reference.contains not in evidence_text:
                    code_evidence_valid = False
                    scene_errors.append(f"Belegtext fehlt in {reference.path}")

        if len(scene.visual_terms) < 2:
            demo_alignment_valid = False
            scene_errors.append("weniger als zwei erwartete sichtbare Begriffe")
        if require_capture:
            captured = capture_scenes.get(scene.id, {})
            if not isinstance(captured, dict) or captured.get("status") != "passed":
                demo_alignment_valid = False
                scene_errors.append("reale Demo-Sichtprüfung fehlt")
            else:
                matched = captured.get("matched_visual_terms", [])
                normalized_matched = {
                    _normalize(str(item)).casefold() for item in matched if isinstance(item, str)
                }
                expected = {_normalize(item).casefold() for item in scene.visual_terms}
                if normalized_matched != expected:
                    demo_alignment_valid = False
                    scene_errors.append(
                        "sichtbare Demo-Begriffe stimmen nicht mit Timeline überein"
                    )

        estimated_speech = estimate_speech_duration(scene.narration, config.tts_speed)
        estimated_required = round(scene.pause_before + estimated_speech + scene.pause_after, 3)
        total_estimated += estimated_required
        if estimated_required > scene.planned_duration + TIMING_TOLERANCE_SECONDS:
            timing_valid = False
            scene_errors.append(
                f"geschätzte Dauer {estimated_required:.2f}s überschreitet "
                f"Szenenbudget {scene.planned_duration:.2f}s"
            )

        words = {word.casefold().strip(".,:;!?()[]") for word in scene.narration.split()}
        if not words.intersection(GERMAN_MARKERS):
            language_valid = False
            scene_errors.append("deutsche Sprachmerkmale fehlen")
        lowered = scene.narration.casefold()
        hype_hits = sorted(term for term in HYPE_TERMS if term in lowered)
        if hype_hits:
            no_hype = False
            scene_errors.append("übertriebene Begriffe: " + ", ".join(hype_hits))
        unsupported = sorted(
            technology for technology in UNIMPLEMENTED_TECHNOLOGIES if technology in lowered
        )
        if unsupported:
            technical_terms_valid = False
            scene_errors.append("nicht belegte Technologie: " + ", ".join(unsupported))

        errors.extend(f"{scene.id}: {error}" for error in scene_errors)
        scene_reports.append(
            {
                "scene_id": scene.id,
                "status": "passed" if not scene_errors else "failed",
                "sentences": len(sentences),
                "claims": len(scene.claims),
                "evidence_references": sum(len(claim.evidence) for claim in scene.claims),
                "visual_terms": len(scene.visual_terms),
                "estimated_speech_seconds": estimated_speech,
                "estimated_required_seconds": estimated_required,
                "planned_seconds": scene.planned_duration,
                "errors": scene_errors,
            }
        )

    checks = {
        "claims_complete": claims_complete,
        "all_sentences_covered": sentences_covered,
        "code_evidence_valid": code_evidence_valid,
        "demo_alignment_valid": demo_alignment_valid,
        "timing_valid": timing_valid,
        "german_language_valid": language_valid,
        "no_hype": no_hype,
        "technical_terms_valid": technical_terms_valid,
        "target_total_duration_valid": (
            config.min_duration_seconds <= timeline.total_duration <= config.max_duration_seconds
        ),
    }
    status = "passed" if all(checks.values()) and not errors else "failed"
    report: dict[str, object] = {
        "status": status,
        "gate_position": "before_tts_cache_and_provider",
        "capture_validation": "actual" if require_capture else "planned",
        "checks": checks,
        "metrics": {
            "scenes": len(timeline.scenes),
            "claims": claim_count,
            "evidence_references": evidence_count,
            "estimated_total_seconds": round(total_estimated, 3),
            "planned_total_seconds": timeline.total_duration,
        },
        "scenes": scene_reports,
        "errors": errors,
    }
    _atomic_json(output_path, report)
    for label, passed in checks.items():
        print(f"[NARRATION QA] {label:.<34} {'PASS' if passed else 'FAIL'}")
    print(f"NARRATION QA: {status.upper()}")
    if status != "passed":
        raise NarrationQualityError("NARRATION_QUALITY_GATE_FAILED: " + "; ".join(errors[:8]))
    return report
