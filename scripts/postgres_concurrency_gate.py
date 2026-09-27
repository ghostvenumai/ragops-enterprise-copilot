"""Explicitly report the deferred PostgreSQL FinOps reservation gate."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy.engine import make_url

MIGRATION_HEAD = "0006_finops_reservations"


def validate_isolated_database(database_url: str) -> tuple[bool, str, str]:
    """Allow only psycopg URLs targeting an explicitly isolated test database."""
    if not database_url:
        return False, "PostgreSQL integration URL unavailable in current sandbox", "unknown"
    try:
        url = make_url(database_url)
    except Exception:  # noqa: BLE001
        return False, "invalid PostgreSQL URL", "unknown"
    database = url.database or ""
    if url.drivername != "postgresql+psycopg":
        return False, "driver must be postgresql+psycopg", database or "unknown"
    if not database.startswith("ragops_test_"):
        return False, "database name must start with ragops_test_", database
    return True, "", database


def run_alembic(database_url: str, *arguments: str) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment["RAGOPS_DATABASE_URL"] = database_url
    return subprocess.run(  # noqa: S603
        [".venv/bin/alembic", *arguments],
        capture_output=True,
        text=True,
        check=False,
        env=environment,
    )


def migration_revision(output: str) -> str | None:
    for line in output.splitlines():
        if MIGRATION_HEAD in line:
            return MIGRATION_HEAD
    return None


def safe_detail(output: str, database_url: str) -> str:
    try:
        redacted = make_url(database_url).render_as_string(hide_password=True)
    except Exception:  # noqa: BLE001
        redacted = database_url
    return output.replace(database_url, redacted)[-1000:]


def main() -> int:
    database_url = os.getenv("RAGOPS_TEST_CONCURRENCY_DATABASE_URL", "")
    if not database_url and os.getenv("RAGOPS_ALLOW_CONCURRENCY_DB_FALLBACK") == "1":
        database_url = os.getenv("RAGOPS_TEST_DATABASE_URL", "")
    commit = subprocess.run(
        ["git", "rev-parse", "HEAD"],  # noqa: S607
        capture_output=True,
        text=True,
        check=False,
    ).stdout.strip()  # noqa: S603, S607
    valid_database, validation_reason, database_name = validate_isolated_database(database_url)
    evidence: dict[str, Any] = {
        "gate": "postgresql_concurrency",
        "tested_commit": commit or "unknown",
        "timestamp": datetime.now(UTC).isoformat(),
        "database_backend": "postgresql" if valid_database else "unknown",
        "database_name": database_name,
        "test_command": ".venv/bin/pytest tests/product/test_postgres_finops_concurrency.py -q -s",
    }
    if not valid_database:
        evidence.update(
            {
                "status": "FAIL" if database_url else "NOT_EXECUTED",
                "migration_preparation": "NOT_RUN",
                "race_test": "NOT_RUN",
                "reason": validation_reason
                or "PostgreSQL integration URL unavailable in current sandbox",
                "exit_code": None,
                "scenario": None,
            }
        )
        _write(evidence)
        return 2
    migration = run_alembic(database_url, "upgrade", "head")
    if migration.returncode != 0:
        evidence.update(
            {
                "status": "FAIL",
                "migration_preparation": "FAIL",
                "race_test": "NOT_RUN",
                "reason": "Alembic migration failed",
                "exit_code": migration.returncode,
                "detail": safe_detail(migration.stderr or migration.stdout, database_url),
                "scenario": None,
            }
        )
        _write(evidence)
        return 1
    current = run_alembic(database_url, "current")
    revision = migration_revision(current.stdout + current.stderr)
    if current.returncode != 0 or revision != MIGRATION_HEAD:
        evidence.update(
            {
                "status": "FAIL",
                "migration_preparation": "FAIL",
                "race_test": "NOT_RUN",
                "alembic_revision": revision,
                "reason": "Alembic head revision was not verified",
                "exit_code": current.returncode,
                "scenario": None,
            }
        )
        _write(evidence)
        return 1
    result = subprocess.run(  # noqa: S603
        [".venv/bin/pytest", "tests/product/test_postgres_finops_concurrency.py", "-q", "-s"],
        capture_output=True,
        text=True,
        check=False,
    )  # noqa: S603
    combined = result.stdout + result.stderr
    evidence.update(
        {
            "status": "PASS" if result.returncode == 0 else "FAIL",
            "migration_preparation": "PASS",
            "alembic_revision": revision,
            "race_test": "PASS" if result.returncode == 0 else "FAIL",
            "exit_code": result.returncode,
            "detail": combined[-1000:],
        }
    )
    marker = "CONCURRENCY_RESULT "
    for line in combined.splitlines():
        if line.startswith(marker):
            try:
                evidence["scenario"] = json.loads(line[len(marker) :])
            except json.JSONDecodeError:
                pass
            break
    _write(evidence)
    return 0 if result.returncode == 0 else 1


def _write(evidence: dict[str, Any]) -> None:
    path = Path("evidence/product-v1/rc-live/postgresql-concurrency.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(evidence, indent=2), encoding="utf-8")


if __name__ == "__main__":
    sys.exit(main())
