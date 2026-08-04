"""Run the synthetic RAG evaluation suite."""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ragops.evaluation.runner import run_evaluation


def main() -> int:
    report = run_evaluation(Path("data/evaluation/gold_questions.json"), Path("evidence"))
    print(json.dumps({k: v for k, v in report.items() if k != "cases"}, indent=2, sort_keys=True))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
