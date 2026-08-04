from __future__ import annotations

from pathlib import Path

from ragops.evaluation.runner import run_evaluation


def test_evaluation_runner_writes_measured_report(tmp_path: Path) -> None:
    report = run_evaluation(Path("data/evaluation/gold_questions.json"), tmp_path)

    assert report["case_count"] == 28
    assert "retrieval_hit_rate" in report
    assert (tmp_path / "rag-evaluation.json").exists()
    assert report["tenant_leakage_count"] == 0
