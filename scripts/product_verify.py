"""Fail-closed enterprise gates. Never deploys, restores or deletes service data."""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "evidence/product-v1"
PYTHON = sys.executable
PENDING = {
    "auth-test-results.json": "OIDC/JWT not implemented; production blocked",
    "tenant-isolation.json": "Persistent Qdrant tenant isolation is not implemented",
    "worker-flow.json": "Redis queue, workers and crash recovery are not implemented",
    "backup-validation.json": "Coordinated backup/restore is not implemented",
    "container-validation.json": "Production Compose topology is not implemented",
    "finops-validation.json": "Usage enforcement and approved model routing not implemented",
    "retention-validation.json": "Retention and vector deletion not implemented",
    "adversarial-review.json": "Separate enterprise adversarial review has not been executed",
}


def write(name: str, payload: dict[str, Any]) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / name
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def execute(name: str, command: list[str], *, timeout: int = 60) -> dict[str, Any]:
    print(f"product-verify: {name}", flush=True)
    try:
        result = subprocess.run(  # noqa: S603 - caller passes fixed command lists only.
            command, cwd=ROOT, capture_output=True, text=True, check=False, timeout=timeout
        )
    except (FileNotFoundError, PermissionError) as exc:
        return {"name": name, "status": "NOT_EXECUTED", "reason": type(exc).__name__}
    except subprocess.TimeoutExpired as exc:
        log = ""
        for output in (exc.stdout, exc.stderr):
            if output:
                log += output.decode(errors="replace") if isinstance(output, bytes) else output
        (OUT / f"{name}.log").write_text(log, encoding="utf-8")
        return {"name": name, "status": "FAILED", "reason": f"Timed out after {timeout}s"}
    (OUT / f"{name}.log").write_text(result.stdout + result.stderr, encoding="utf-8")
    return {
        "name": name,
        "status": "PASSED" if result.returncode == 0 else "FAILED",
        "returncode": result.returncode,
        "log": f"{name}.log",
    }


def missing_modules(names: tuple[str, ...]) -> list[str]:
    return [name for name in names if importlib.util.find_spec(name) is None]


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    temp = ROOT / ".loop/tmp/product-verification"
    temp.mkdir(parents=True, exist_ok=True)
    os.environ["TMPDIR"] = str(temp)
    os.environ["XDG_CACHE_HOME"] = str(temp / "cache")
    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    (temp / run_id).mkdir()
    write(
        "release-gate.json",
        {
            "status": "BLOCKED",
            "run_id": run_id,
            "reason": "Verification in progress",
            "production_enabled": False,
        },
    )
    for name in (
        "test-results.xml",
        "coverage.xml",
        "api-tests.xml",
        "migration-tests.xml",
        "postgres-tests.xml",
    ):
        (OUT / name).unlink(missing_ok=True)
    results = []
    # Every existing test is included: API tests are isolated because their event
    # loop cannot initialize in a sandbox denying socketpair. Timeouts fail the gate.
    results.append(
        execute(
            "local-tests",
            [
                PYTHON,
                "-m",
                "pytest",
                "tests/unit",
                "tests/security",
                "tests/evaluation",
                "tests/integration/test_workflow.py",
                "tests/integration/test_demo_controller.py",
                "--basetemp",
                str(temp / run_id / "local"),
                "--junitxml",
                str(OUT / "test-results.xml"),
                "--cov",
                "--cov-branch",
                f"--cov-report=xml:{OUT / 'coverage.xml'}",
            ],
        )
    )
    results.append(
        execute(
            "api-tests",
            [
                PYTHON,
                "-m",
                "pytest",
                "tests/integration/test_api.py",
                "--basetemp",
                str(temp / run_id / "api"),
                "-o",
                "faulthandler_timeout=10",
                "--junitxml",
                str(OUT / "api-tests.xml"),
            ],
            timeout=30,
        )
    )
    results.append(execute("lint", [PYTHON, "-m", "ruff", "check", "."]))
    results.append(
        execute(
            "typecheck",
            [
                PYTHON,
                "-m",
                "mypy",
                "src",
                "loop",
                "scripts",
                "automation",
                "video",
                "apps",
            ],
        )
    )
    results.append(execute("security", [PYTHON, "scripts/verify.py", "--only", "security"]))
    shutil.copyfile(ROOT / "evidence/security-report.json", OUT / "security-report.json")
    # Uses actual deterministic fixture workflow, explicitly not a live-model evaluation.
    from ragops.evaluation.runner import run_evaluation

    evaluation = run_evaluation(ROOT / "data/evaluation/gold_questions.json", OUT)
    results.append(
        {
            "name": "rag-evaluation",
            "status": str(evaluation["status"]).upper(),
            "scope": "deterministic synthetic fixtures",
        }
    )
    write(
        "performance-summary.json",
        {
            "status": "NOT_EXECUTED",
            "reason": "No production load/restart test environment",
            "fixture_evaluation": "rag-evaluation.json",
        },
    )
    missing = missing_modules(("sqlalchemy", "alembic", "psycopg"))
    if missing:
        migration = {
            "name": "migrations",
            "status": "NOT_EXECUTED",
            "reason": "Missing declared persistence dependencies: " + ", ".join(missing),
        }
    else:
        migration = execute(
            "migrations",
            [
                PYTHON,
                "-m",
                "pytest",
                "tests/product/test_persistence.py",
                "--basetemp",
                str(temp / run_id / "migrations"),
                "--junitxml",
                str(OUT / "migration-tests.xml"),
            ],
        )
    results.append(migration)
    (OUT / "migration-test.txt").write_text(json.dumps(migration) + "\n", encoding="utf-8")
    if missing or not os.getenv("RAGOPS_TEST_DATABASE_URL"):
        postgres = {
            "name": "postgres",
            "status": "NOT_EXECUTED",
            "reason": "Persistence dependencies or explicit disposable test DB unavailable",
        }
    else:
        postgres = execute(
            "postgres",
            [
                PYTHON,
                "-m",
                "pytest",
                "tests/product/test_postgres.py",
                "--junitxml",
                str(OUT / "postgres-tests.xml"),
            ],
        )
    results.append(postgres)
    write("postgres-validation.json", postgres)
    audit = execute(
        "dependency-audit",
        [
            PYTHON,
            "-m",
            "pip_audit",
            "--cache-dir",
            str(temp / "audit-cache"),
            "-r",
            "constraints.txt",
            "--progress-spinner",
            "off",
            "-f",
            "json",
            "-o",
            str(OUT / "dependency-audit.json"),
        ],
        timeout=30,
    )
    results.append(audit)
    if audit["status"] != "PASSED":
        write("dependency-audit.json", audit)
    for name, reason in PENDING.items():
        entry = {"name": name.removesuffix(".json"), "status": "NOT_EXECUTED", "reason": reason}
        write(name, entry)
        results.append(entry)
    from ragops import __version__

    report = {
        "version": __version__,
        "run_id": run_id,
        "production_enabled": False,
        "status": "PASSED" if all(item["status"] == "PASSED" for item in results) else "BLOCKED",
        "results": results,
    }
    write("release-gate.json", report)
    lines = [
        f"# Production readiness — {__version__}",
        "",
        f"Run: {run_id}",
        "",
        f"Release gate: **{report['status']}**. Production startup is disabled.",
        "",
        "| Gate | Result |",
        "|---|---|",
    ]
    lines.extend(f"| {item['name']} | {item['status']} |" for item in results)
    lines.extend(
        [
            "",
            "See release-gate.json for reasons and individual logs for real results.",
            "This foundation is not a release candidate or a v1.0 certification.",
            "",
        ]
    )
    (OUT / "production-readiness.md").write_text("\n".join(lines), encoding="utf-8")
    print(json.dumps(report, indent=2))
    return 0 if report["status"] == "PASSED" else 1


if __name__ == "__main__":
    raise SystemExit(main())
