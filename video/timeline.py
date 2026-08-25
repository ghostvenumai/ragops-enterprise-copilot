"""Timeline parsing and validation for narration, capture, and rendering."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SCENE_ID = re.compile(r"^[a-z][a-z0-9_-]{1,40}$")
ALLOWED_CAPTURES = {
    "title:intro",
    "app:overview",
    "app:knowledge",
    "app:answer",
    "app:blocked",
    "app:monitoring",
    "app:governance",
    "report:automation",
    "title:outro",
}


@dataclass(frozen=True)
class EvidenceReference:
    path: str
    contains: str

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> EvidenceReference:
        try:
            return cls(path=str(payload["path"]), contains=str(payload["contains"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"invalid narration evidence reference: {payload!r}") from exc


@dataclass(frozen=True)
class NarrationClaim:
    statement: str
    evidence: tuple[EvidenceReference, ...]

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> NarrationClaim:
        try:
            raw_evidence = payload["evidence"]
            if not isinstance(raw_evidence, list):
                raise TypeError("evidence must be a list")
            return cls(
                statement=str(payload["statement"]),
                evidence=tuple(
                    EvidenceReference.from_dict(item)
                    for item in raw_evidence
                    if isinstance(item, dict)
                ),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"invalid narration claim: {payload!r}") from exc


@dataclass(frozen=True)
class Scene:
    id: str
    order: int
    action: str
    planned_duration: float
    narration: str
    overlay: str
    capture: str
    pause_before: float = 0.5
    pause_after: float = 0.8
    claims: tuple[NarrationClaim, ...] = ()
    visual_terms: tuple[str, ...] = ()

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> Scene:
        try:
            return cls(
                id=str(payload["id"]),
                order=int(payload["order"]),
                action=str(payload["action"]),
                planned_duration=float(payload["planned_duration"]),
                narration=str(payload["narration"]),
                overlay=str(payload["overlay"]),
                capture=str(payload["capture"]),
                pause_before=float(payload.get("pause_before", 0.5)),
                pause_after=float(payload.get("pause_after", 0.8)),
                claims=tuple(
                    NarrationClaim.from_dict(item)
                    for item in payload.get("claims", [])
                    if isinstance(item, dict)
                ),
                visual_terms=tuple(str(item) for item in payload.get("visual_terms", [])),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"invalid timeline scene: {payload!r}") from exc

    @property
    def narration_duration(self) -> float:
        return self.planned_duration - self.pause_before - self.pause_after


@dataclass(frozen=True)
class Timeline:
    title: str
    language: str
    scenes: tuple[Scene, ...]

    @property
    def total_duration(self) -> float:
        return round(sum(scene.planned_duration for scene in self.scenes), 3)


def load_timeline(path: Path, *, min_duration: float = 120, max_duration: float = 180) -> Timeline:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or not isinstance(raw.get("scenes"), list):
        raise ValueError("timeline root must contain a scenes list")
    scenes = tuple(Scene.from_dict(item) for item in raw["scenes"] if isinstance(item, dict))
    timeline = Timeline(
        title=str(raw.get("title", "")),
        language=str(raw.get("language", "")),
        scenes=tuple(sorted(scenes, key=lambda scene: scene.order)),
    )
    validate_timeline(timeline, min_duration=min_duration, max_duration=max_duration)
    return timeline


def validate_timeline(
    timeline: Timeline, *, min_duration: float = 120, max_duration: float = 180
) -> None:
    if not timeline.title or timeline.language not in {"de", "en"}:
        raise ValueError("timeline requires a title and supported language")
    if not timeline.scenes:
        raise ValueError("timeline must contain scenes")
    expected_orders = list(range(1, len(timeline.scenes) + 1))
    if [scene.order for scene in timeline.scenes] != expected_orders:
        raise ValueError("scene order must be consecutive and start at one")
    if len({scene.id for scene in timeline.scenes}) != len(timeline.scenes):
        raise ValueError("scene ids must be unique")
    for scene in timeline.scenes:
        if not SCENE_ID.fullmatch(scene.id):
            raise ValueError(f"invalid scene id: {scene.id}")
        if not scene.action or not scene.narration or not scene.overlay:
            raise ValueError(f"scene {scene.id} has empty required fields")
        if scene.capture not in ALLOWED_CAPTURES:
            raise ValueError(f"scene {scene.id} has unsupported capture mode")
        if any(not claim.statement or not claim.evidence for claim in scene.claims):
            raise ValueError(f"scene {scene.id} has incomplete narration claims")
        if any(
            not reference.path or not reference.contains
            for claim in scene.claims
            for reference in claim.evidence
        ):
            raise ValueError(f"scene {scene.id} has incomplete evidence references")
        if any(not term.strip() for term in scene.visual_terms):
            raise ValueError(f"scene {scene.id} has empty visual terms")
        if not 3 <= scene.planned_duration <= 40 or scene.narration_duration < 1:
            raise ValueError(f"scene {scene.id} has invalid timing")
    if not min_duration <= timeline.total_duration <= max_duration:
        raise ValueError(
            f"timeline duration {timeline.total_duration}s outside {min_duration}-{max_duration}s"
        )
