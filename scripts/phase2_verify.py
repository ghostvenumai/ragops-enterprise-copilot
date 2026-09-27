"""Generate honest, bounded Phase 2 identity evidence."""

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


def write(name: str, payload: object) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / name).write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main() -> int:
    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    auth = run(
        "auth-tests",
        [
            sys.executable,
            "-m",
            "pytest",
            "tests/security/test_identity.py",
            "--junitxml",
            str(OUT / "auth-test-results.xml"),
        ],
    )
    authorization = run(
        "authorization-tests",
        [
            sys.executable,
            "-m",
            "pytest",
            "tests/security/test_product_foundation.py",
            "tests/security/test_mandantentrennung.py",
            "--junitxml",
            str(OUT / "authorization-security.xml"),
        ],
    )
    typecheck = run(
        "typecheck",
        [sys.executable, "-m", "mypy", "src/ragops/auth", "src/ragops/api", "src/ragops/config"],
    )
    lint = run("lint", [sys.executable, "-m", "ruff", "check", "src", "tests"])
    config_checks = {
        "development_identity_rejected_in_production": False,
        "oidc_missing_config_rejected": False,
        "unsupported_algorithm_rejected": False,
        "production_runtime_still_disabled": False,
    }
    from ragops.api.app import create_app
    from ragops.auth.dependencies import identity_provider_for
    from ragops.config.settings import Settings

    try:
        identity_provider_for(Settings(environment="production", identity_provider="development"))
    except RuntimeError:
        config_checks["development_identity_rejected_in_production"] = True
    try:
        identity_provider_for(Settings(environment="production", identity_provider="oidc"))
    except RuntimeError:
        config_checks["oidc_missing_config_rejected"] = True
    try:
        identity_provider_for(
            Settings(
                environment="development",
                identity_provider="oidc",
                oidc_issuer="https://id.example",
                oidc_audience="ragops-api",
                oidc_public_key="synthetic",
                oidc_algorithms=("HS256",),
            )
        )
    except RuntimeError:
        config_checks["unsupported_algorithm_rejected"] = True
    try:
        create_app(
            Settings(
                environment="production",
                identity_provider="oidc",
                oidc_issuer="https://id.example",
                oidc_audience="ragops-api",
                oidc_public_key="synthetic",
            )
        )
    except RuntimeError:
        config_checks["production_runtime_still_disabled"] = True
    config = {
        "name": "configuration-validation",
        "status": "PASSED" if all(config_checks.values()) else "FAILED",
        "checks": config_checks,
        "scope": "local deterministic configuration checks",
    }
    write("auth-test-results.json", auth)
    write("authorization-security.json", authorization)
    write("typecheck-results-phase2.json", typecheck)
    write("configuration-validation.json", config)
    write(
        "phase2-summary.json",
        {
            "phase": "PHASE-2",
            "version": "0.2.0.dev0",
            "run_id": run_id,
            "status": "PASSED"
            if all(
                item["status"] == "PASSED"
                for item in (auth, authorization, typecheck, lint, config)
            )
            else "BLOCKED",
            "checks": [auth, authorization, typecheck, lint, config],
            "not_executed": [
                {"name": "live-oidc-provider", "reason": "No external OIDC service configured"},
                {
                    "name": "production-postgresql",
                    "reason": "No external PostgreSQL service configured",
                },
            ],
            "tenant_leakage_expected": 0,
            "production_ready": False,
        },
    )
    return (
        0
        if all(
            item["status"] == "PASSED" for item in (auth, authorization, typecheck, lint, config)
        )
        else 1
    )


if __name__ == "__main__":
    raise SystemExit(main())
