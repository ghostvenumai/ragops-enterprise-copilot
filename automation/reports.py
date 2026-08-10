"""Human- and machine-readable master-loop reports."""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

from video.config import REPO_ROOT

from automation.state import LoopState, Phase


def portable_state(state: LoopState) -> dict[str, object]:
    payload: dict[str, object] = asdict(state)
    artifacts: dict[str, str] = {}
    for name, value in state.artifacts.items():
        candidate = Path(value)
        if candidate.is_absolute():
            try:
                value = str(candidate.relative_to(REPO_ROOT))
            except ValueError:
                value = "external-path-redacted"
        artifacts[name] = value
    payload["artifacts"] = artifacts
    return payload


def phase_status(state: LoopState, phase: Phase) -> str:
    if phase.value in state.blocked_phases:
        return "BLOCKED"
    if phase.value in state.completed_phases:
        return "PASS"
    if phase.value in state.failed_phases:
        return "FAIL"
    return "NOT_RUN"


def write_reports(
    state: LoopState, dist_dir: Path, evidence_dir: Path | None = None
) -> tuple[Path, Path]:
    dist_dir.mkdir(parents=True, exist_ok=True)
    evidence_dir = evidence_dir or REPO_ROOT / "evidence"
    evidence_dir.mkdir(parents=True, exist_ok=True)
    json_path = dist_dir / "master_loop_report.json"
    markdown_path = dist_dir / "build_report.md"
    json_text = json.dumps(portable_state(state), indent=2, sort_keys=True) + "\n"
    json_path.write_text(json_text, encoding="utf-8")
    rows = "\n".join(
        f"| {phase.value} | {phase_status(state, phase)} |"
        for phase in Phase
        if phase is not Phase.COMPLETE
    )
    blockers = (
        "\n".join(
            f"- `{item}`: {state.blocker_details.get(item, 'Keine Detaildiagnose')}"
            for item in state.blocked_phases
        )
        or "- Keine"
    )
    output = state.artifacts.get("video", "Nicht erzeugt")
    text = f"""# RAGOps Master-Loop Build Report

- Build ID: `{state.build_id}`
- Status: **{state.status}**
- Letzte erfolgreiche Aktion: `{state.last_successful_action or "keine"}`
- Letzte Fehlerkategorie: `{state.last_error_category or "keine"}`

| Phase | Ergebnis |
|---|---|
{rows}

## Finaler Output

`{output}`

## Externe Blocker

{blockers}

## Diagnose

{state.last_error or "Keine offene Diagnose."}
"""
    text = text.replace("`", chr(96))
    markdown_path.write_text(text, encoding="utf-8")
    (evidence_dir / "master-loop-report.json").write_text(json_text, encoding="utf-8")
    (evidence_dir / "master-loop-release-report.md").write_text(text, encoding="utf-8")
    return markdown_path, json_path


def print_summary(state: LoopState) -> None:
    print("=" * 58)
    print("RAGOPS ENTERPRISE COPILOT - AUTONOMOUS BUILD REPORT")
    print("=" * 58)
    labels = (
        ("APPLICATION", Phase.APPLICATION_QA),
        ("UNIT TESTS", Phase.UNIT_TEST),
        ("INTEGRATION TESTS", Phase.INTEGRATION_TEST),
        ("SECURITY CHECK", Phase.SECURITY_CHECK),
        ("DEMO", Phase.DEMO_RUN),
        ("RECORDING", Phase.RECORD),
        ("NARRATION", Phase.GENERATE_NARRATION),
        ("VOICEOVER", Phase.GENERATE_VOICE),
        ("SUBTITLES", Phase.GENERATE_SUBTITLES),
        ("RENDER", Phase.RENDER),
        ("VIDEO QA", Phase.VIDEO_QA),
    )
    for label, phase in labels:
        print(f"{label + ':':20}{phase_status(state, phase)}")
    print(f"\nFINAL STATUS:       {state.status}")
    print(f"VIDEO:              {state.artifacts.get('video', 'nicht erzeugt')}")
    print("REPORT:             dist/build_report.md")
    print("=" * 58)
