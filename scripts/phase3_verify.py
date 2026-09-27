"""Generate bounded, machine-readable Phase 3 knowledge-management evidence."""

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
        completed = subprocess.run(  # noqa: S603 - fixed repository-local quality gates
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


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    checks = [
        run(
            "knowledge-management-tests",
            [
                sys.executable,
                "-m",
                "pytest",
                "tests/product/test_knowledge_management.py",
                "--junitxml",
                str(OUT / "knowledge-management-tests.xml"),
            ],
        ),
        run(
            "tenant-isolation-tests",
            [
                sys.executable,
                "-m",
                "pytest",
                "tests/security/test_mandantentrennung.py",
                "tests/product/test_persistence.py",
            ],
        ),
        run(
            "security-upload-tests",
            [sys.executable, "-m", "pytest", "tests/security/test_product_foundation.py"],
        ),
        run(
            "typecheck-phase3",
            [
                sys.executable,
                "-m",
                "mypy",
                "src/ragops/knowledge",
                "src/ragops/persistence",
                "src/ragops/api",
            ],
        ),
        run(
            "lint-phase3", [sys.executable, "-m", "ruff", "check", "src", "tests", "apps/dashboard"]
        ),
    ]
    migration = run(
        "migration-phase3",
        [sys.executable, "-m", "pytest", "tests/product/test_persistence.py", "-q"],
    )
    summary = {
        "phase": "PHASE-3",
        "version": "0.2.0.dev0",
        "run_id": datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ"),
        "status": "PASSED"
        if all(item["status"] == "PASSED" for item in [*checks, migration])
        else "BLOCKED",
        "checks": [*checks, migration],
        "tenant_leakage_expected": 0,
        "production_ready": False,
        "not_executed": [
            {
                "name": "production-postgresql",
                "reason": "External PostgreSQL is unavailable in the sandbox",
            },
            {
                "name": "live-docker-api-integration",
                "reason": "Docker/socket access is blocked by the sandbox",
            },
            {
                "name": "full-oidc-asgi-endpoint",
                "reason": "Sandbox TestClient event-loop/portal integration is blocked",
            },
        ],
    }
    (OUT / "phase3-summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    (OUT / "tenant-isolation-phase3.json").write_text(
        json.dumps(checks[1], indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    (OUT / "security-phase3.json").write_text(
        json.dumps({"upload": checks[2], "tenant_leakage_expected": 0}, indent=2, sort_keys=True)
        + "\n",
        encoding="utf-8",
    )
    (OUT / "typecheck-phase3.json").write_text(
        json.dumps(checks[3], indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    (OUT / "migration-phase3.txt").write_text(str(migration) + "\n", encoding="utf-8")
    return 0 if summary["status"] == "PASSED" else 1


if __name__ == "__main__":
    raise SystemExit(main())
