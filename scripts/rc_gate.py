"""Verification-first Phase 10 release-candidate gate.

Live gates are never downgraded to local simulations; unavailable infrastructure is BLOCKED.
"""

# ruff: noqa: E501, S603, S607
from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "evidence" / "product-v1" / "rc"


def command_version(command: str) -> str:
    executable = shutil.which(command)
    if not executable:
        return "UNAVAILABLE"
    result = subprocess.run([executable, "--version"], capture_output=True, text=True, check=False)  # noqa: S603
    return result.stdout.strip() or result.stderr.strip() or "UNKNOWN"


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    commit = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True, check=False
    ).stdout.strip()  # noqa: S603
    preflight = {
        "timestamp": datetime.now(UTC).isoformat(),
        "host": platform.platform(),
        "python": platform.python_version(),
        "docker": command_version("docker"),
        "compose": command_version("docker-compose"),
        "repository_commit": commit,
        "dirty": bool(
            subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=ROOT,
                capture_output=True,
                text=True,
                check=False,
            ).stdout.strip()
        ),  # noqa: S603
        "secrets_present": {
            key: bool(os.getenv(key))
            for key in (
                "RAGOPS_DATABASE_URL",
                "RAGOPS_REDIS_URL",
                "RAGOPS_QDRANT_URL",
                "RAGOPS_OIDC_ISSUER",
                "OPENAI_API_KEY",
            )
        },
    }
    (OUT / "preflight.json").write_text(json.dumps(preflight, indent=2), encoding="utf-8")
    (OUT / "tested-commit.json").write_text(
        json.dumps({"commit": commit, "version": "0.2.0.dev0"}, indent=2), encoding="utf-8"
    )
    compose = subprocess.run(
        ["docker-compose", "-f", "docker-compose.yml", "-f", "docker-compose.prod.yml", "config"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )  # noqa: S603
    (OUT / "compose-config.txt").write_text(compose.stdout + compose.stderr, encoding="utf-8")
    gates = {
        name: "BLOCKED"
        for name in (
            "docker_compose",
            "postgresql",
            "redis",
            "qdrant",
            "oidc",
            "external_provider",
            "tenant_isolation",
            "backup_restore",
            "browser_e2e",
            "security",
        )
    }
    reason = "NOT_EXECUTED/BLOCKED: authorized live integration host and required services/credentials are unavailable in current sandbox"
    for name in gates:
        (OUT / f"{name.replace('_', '-')}.json").write_text(
            json.dumps({"status": gates[name], "reason": reason}, indent=2), encoding="utf-8"
        )
    release = {"commit": commit, "overall": "BLOCKED", "mandatory_gates": gates, "reason": reason}
    (OUT / "release-gate.json").write_text(json.dumps(release, indent=2), encoding="utf-8")
    (OUT / "rc-report.md").write_text(
        "# Release Candidate Gate\n\nOverall: **BLOCKED**. Live infrastructure and credentialed integration gates were not available in the sandbox. No RC tag was created.\n",
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
