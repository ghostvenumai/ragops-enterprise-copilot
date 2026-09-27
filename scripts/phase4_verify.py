"""Generate bounded, machine-readable Phase 4 async-ingestion evidence."""

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
        completed = subprocess.run(  # noqa: S603 - fixed repository quality commands
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
            "worker-tests",
            [
                sys.executable,
                "-m",
                "pytest",
                "tests/product/test_async_ingestion.py",
                "--junitxml",
                str(OUT / "worker-tests.xml"),
            ],
        ),
        run(
            "ingestion-lifecycle",
            [sys.executable, "-m", "pytest", "tests/product/test_knowledge_management.py"],
        ),
        run(
            "tenant-isolation",
            [
                sys.executable,
                "-m",
                "pytest",
                "tests/security/test_mandantentrennung.py",
                "tests/product/test_persistence.py",
            ],
        ),
        run(
            "security",
            [
                sys.executable,
                "-m",
                "pytest",
                "tests/security/test_product_foundation.py",
                "tests/security/test_identity.py",
            ],
        ),
        run(
            "typecheck",
            [
                sys.executable,
                "-m",
                "mypy",
                "src/ragops/workers",
                "src/ragops/knowledge",
                "src/ragops/persistence",
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
        "phase": "PHASE-4",
        "version": "0.2.0.dev0",
        "run_id": datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ"),
        "status": "PASSED" if all(item["status"] == "PASSED" for item in checks) else "BLOCKED",
        "checks": checks,
        "tenant_leakage_expected": 0,
        "production_ready": False,
        "not_executed": [
            {
                "name": "redis-integration",
                "reason": "Redis/socket access is unavailable in the sandbox",
            },
            {
                "name": "production-postgresql",
                "reason": "External PostgreSQL is unavailable in the sandbox",
            },
            {
                "name": "docker-worker-integration",
                "reason": "Docker daemon access is unavailable in the sandbox",
            },
        ],
    }
    write("phase4-summary.json", summary)
    write("ingestion-lifecycle.json", checks[1])
    write("tenant-isolation-phase4.json", checks[2])
    write("security-phase4.json", {"checks": checks[3], "tenant_leakage_expected": 0})
    write("typecheck-phase4.json", checks[4])
    (OUT / "migration-phase4.txt").write_text(
        json.dumps(checks[6], indent=2) + "\n", encoding="utf-8"
    )
    return 0 if summary["status"] == "PASSED" else 1


if __name__ == "__main__":
    raise SystemExit(main())
