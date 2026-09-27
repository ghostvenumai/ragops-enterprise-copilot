"""Generate bounded, machine-readable Phase 5 vector evidence."""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "evidence/product-v1"
sys.path.insert(0, str(ROOT / "src"))


def run(name: str, command: list[str], timeout: int = 120) -> dict[str, object]:
    try:
        completed = subprocess.run(  # noqa: S603 - fixed local quality commands
            command, cwd=ROOT, capture_output=True, text=True, check=False, timeout=timeout
        )
    except subprocess.TimeoutExpired:
        return {"name": name, "status": "NOT_EXECUTED", "reason": f"timeout after {timeout}s"}
    return {
        "name": name,
        "status": "PASSED" if completed.returncode == 0 else "FAILED",
        "returncode": completed.returncode,
        "stdout_tail": completed.stdout[-4000:],
        "stderr_tail": completed.stderr[-4000:],
    }


def write(name: str, value: object) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / name).write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main() -> int:
    checks = [
        run(
            "vector-contract-tests",
            [
                sys.executable,
                "-m",
                "pytest",
                "tests/product/test_vector_index.py",
                "--junitxml",
                str(OUT / "vector-contract-tests.xml"),
            ],
        ),
        run(
            "worker-regression",
            [sys.executable, "-m", "pytest", "tests/product/test_async_ingestion.py"],
        ),
        run(
            "knowledge-regression",
            [sys.executable, "-m", "pytest", "tests/product/test_knowledge_management.py"],
        ),
        run(
            "tenant-security",
            [
                sys.executable,
                "-m",
                "pytest",
                "tests/security/test_identity.py",
                "tests/security/test_product_foundation.py",
                "tests/security/test_mandantentrennung.py",
            ],
        ),
        run(
            "rag-regression",
            [
                sys.executable,
                "-m",
                "pytest",
                "tests/integration/test_workflow.py",
                "tests/evaluation/test_evaluation_runner.py",
            ],
        ),
        run(
            "typecheck",
            [
                sys.executable,
                "-m",
                "mypy",
                "src/ragops/vector",
                "src/ragops/workers",
                "src/ragops/retrieval",
                "src/ragops/api",
                "src/ragops/config",
            ],
        ),
        run("lint", [sys.executable, "-m", "ruff", "check", "src", "tests", "apps", "scripts"]),
        run(
            "migration", [sys.executable, "-m", "pytest", "tests/product/test_persistence.py", "-q"]
        ),
    ]
    summary = {
        "phase": "PHASE-5",
        "version": "0.2.0.dev0",
        "run_id": datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ"),
        "status": "PASSED" if all(item["status"] == "PASSED" for item in checks) else "BLOCKED",
        "checks": checks,
        "tenant_leakage_expected": 0,
        "production_ready": False,
        "qdrant_integration": {
            "status": "NOT_EXECUTED",
            "reason": "Qdrant/socket access is unavailable in sandbox",
        },
    }
    write("phase5-summary.json", summary)
    write("vector-lifecycle.json", checks[0])
    write("vector-idempotency.json", checks[0])
    write("vector-tenant-isolation.json", checks[3])
    write("vector-security.json", {"checks": checks[3], "tenant_leakage_expected": 0})
    write("typecheck-phase5.json", checks[5])
    write("qdrant-integration.json", summary["qdrant_integration"])
    return 0 if summary["status"] == "PASSED" else 1


if __name__ == "__main__":
    raise SystemExit(main())
