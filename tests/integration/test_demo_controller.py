from __future__ import annotations

import json
from pathlib import Path

from ragops.demo.controller import DemoController


def test_demo_controller_runs_real_workflow_and_exports(tmp_path: Path) -> None:
    result = DemoController(Path("data/synthetic"), tmp_path).run()

    assert result.document_count >= 20
    assert result.customer_count >= 12
    assert result.contract_count >= 20
    assert result.ticket_count >= 30
    assert result.citation_ids
    assert "Acme Industrial AG" in result.answer
    assert result.prompt_injection_detected
    assert "blockiert" in result.blocked_answer

    exported = json.loads((tmp_path / "application-demo.json").read_text(encoding="utf-8"))
    assert exported["prompt_injection_detected"] is True
    assert exported["citation_ids"]
