"""Central quality gate runner.

The runner executes available local tools and records unavailable optional tools
as `not_executed`. It does not fabricate results.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path
from time import perf_counter

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
EVIDENCE_DIR = REPO_ROOT / "evidence"

SECRET_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9]{20,}"),
    re.compile(r"(?i)(api[_-]?key|secret|token)[ \t]*=[ \t]*['\"]?[A-Za-z0-9_./+-]{12,}"),
    re.compile(r"-----BEGIN (?:RSA |OPENSSH |EC )?PRIVATE KEY-----"),
]


def write_json(name: str, payload: Mapping[str, object]) -> None:
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    (EVIDENCE_DIR / name).write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def run_command(name: str, command: list[str], output_file: str | None = None) -> dict[str, object]:
    if shutil.which(command[0]) is None:
        return {"name": name, "status": "not_executed", "reason": f"{command[0]} not available"}
    started = perf_counter()
    completed = subprocess.run(  # noqa: S603 - command lists are fixed quality gates.
        command,
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    result: dict[str, object] = {
        "name": name,
        "status": "passed" if completed.returncode == 0 else "failed",
        "returncode": completed.returncode,
        "duration_ms": round((perf_counter() - started) * 1000, 3),
        "stdout_tail": completed.stdout[-4000:],
        "stderr_tail": completed.stderr[-4000:],
    }
    if output_file:
        (EVIDENCE_DIR / output_file).write_text(
            completed.stdout,
            encoding="utf-8",
        )
    return result


def iter_repo_files() -> list[Path]:
    excluded = {".git", ".venv", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"}
    files: list[Path] = []
    for path in REPO_ROOT.rglob("*"):
        if any(part in excluded for part in path.parts):
            continue
        if path.is_file():
            files.append(path)
    return files


def check_no_env_files() -> dict[str, object]:
    env_files = [
        str(path.relative_to(REPO_ROOT))
        for path in REPO_ROOT.rglob(".env*")
        if path.name != ".env.example" and ".git" not in path.parts
    ]
    return {
        "name": "env-file-check",
        "status": "passed" if not env_files else "failed",
        "unexpected_env_files": env_files,
    }


def secret_scan() -> dict[str, object]:
    findings: list[dict[str, str]] = []
    for path in iter_repo_files():
        if path.suffix.lower() in {".png", ".jpg", ".jpeg", ".gif", ".ico"}:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for pattern in SECRET_PATTERNS:
            for match in pattern.finditer(text):
                findings.append(
                    {
                        "path": str(path.relative_to(REPO_ROOT)),
                        "pattern": pattern.pattern,
                        "preview": match.group(0)[:20] + "...",
                    }
                )
    report: dict[str, object] = {
        "status": "passed" if not findings else "failed",
        "findings": findings,
    }
    write_json("secret-scan.json", report)
    return {"name": "secret-scan", **report}


def synthetic_data_scan() -> dict[str, object]:
    issues: list[str] = []
    for path in (REPO_ROOT / "data").rglob("*"):
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8").lower()
        except UnicodeDecodeError:
            continue
        if "synthetic" not in text and "synthetisch" not in text:
            issues.append(str(path.relative_to(REPO_ROOT)))
    return {
        "name": "synthetic-data-scan",
        "status": "passed" if not issues else "failed",
        "issues": issues,
    }


def docker_config_check() -> dict[str, object]:
    dockerfile = (
        (REPO_ROOT / "Dockerfile").read_text(encoding="utf-8")
        if (REPO_ROOT / "Dockerfile").exists()
        else ""
    )
    compose = (
        (REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8")
        if (REPO_ROOT / "docker-compose.yml").exists()
        else ""
    )
    checks = {
        "dockerfile_non_root_user": "USER appuser" in dockerfile,
        "compose_no_new_privileges": "no-new-privileges:true" in compose,
        "compose_drop_capabilities": "cap_drop:" in compose and "ALL" in compose,
        "compose_no_privileged_true": "privileged: true" not in compose,
        "compose_no_host_network": "network_mode: host" not in compose,
        "compose_healthchecks": "healthcheck:" in compose,
    }
    report: dict[str, object] = {
        "status": "passed" if all(checks.values()) else "failed",
        "checks": checks,
    }
    write_json("container-config-check.json", report)
    return {"name": "container-config-check", **report}


def run_pytest() -> dict[str, object]:
    coverage_xml = EVIDENCE_DIR / "coverage.xml"
    result = run_command(
        "pytest",
        [
            sys.executable,
            "-m",
            "pytest",
            "--junitxml",
            str(EVIDENCE_DIR / "test-results.xml"),
            "--cov=src/ragops",
            "--cov=loop",
            f"--cov-report=xml:{coverage_xml}",
        ],
    )
    if result.get("status") == "failed" and "unrecognized arguments" in str(
        result.get("stderr_tail", "")
    ):
        result = run_command(
            "pytest",
            [
                sys.executable,
                "-m",
                "pytest",
                "--junitxml",
                str(EVIDENCE_DIR / "test-results.xml"),
            ],
        )
        coverage_xml.write_text(
            '<coverage status="not_executed" reason="pytest-cov plugin not available"/>\n',
            encoding="utf-8",
        )
    elif not coverage_xml.exists():
        coverage_xml.write_text(
            '<coverage status="not_executed" reason="coverage report was not generated"/>\n',
            encoding="utf-8",
        )
    return result


def run_evaluation_gate() -> dict[str, object]:
    if not (REPO_ROOT / "data/evaluation/gold_questions.json").exists():
        return {"name": "evaluation", "status": "not_executed", "reason": "gold dataset missing"}
    return run_command("evaluation", [sys.executable, "scripts/run_evaluation.py"])


def run_external_tool_gates() -> list[dict[str, object]]:
    gates = [
        (
            "ruff",
            [sys.executable, "-m", "ruff", "check", "--output-format", "json", "."],
            "lint-results.json",
        ),
        (
            "mypy",
            [
                sys.executable,
                "-m",
                "mypy",
                "src",
                "loop",
                "scripts",
                "automation",
                "video",
                "apps",
            ],
            "typecheck-results.txt",
        ),
        (
            "bandit",
            [
                sys.executable,
                "-m",
                "bandit",
                "-q",
                "-lll",
                "-r",
                "src",
                "loop",
                "scripts",
                "automation",
                "video",
                "apps",
                "-f",
                "json",
            ],
            "bandit-report.json",
        ),
        (
            "pip-audit",
            [sys.executable, "-m", "pip_audit", "-r", "constraints.txt", "-f", "json"],
            "dependency-audit.json",
        ),
    ]
    results = []
    for name, command, output_file in gates:
        result = run_command(name, command, output_file=output_file)
        if result["status"] == "not_executed":
            write_json(output_file, result)
        results.append(result)
    sbom_result = run_command(
        "sbom",
        [
            sys.executable,
            "-m",
            "pip_audit",
            "-r",
            "constraints.txt",
            "-f",
            "cyclonedx-json",
            "-o",
            str(EVIDENCE_DIR / "software-bill-of-materials.json"),
        ],
    )
    results.append(sbom_result)
    return results


def build_loop_summary() -> None:
    last_result = {}
    path = REPO_ROOT / ".loop/last_result.json"
    if path.exists():
        last_result = json.loads(path.read_text(encoding="utf-8"))
    write_json(
        "loop-summary.json",
        {
            "status": "generated",
            "last_result_status": last_result.get("status", "not_run"),
            "state_path": ".loop/state.json",
            "history_path": ".loop/history.jsonl",
        },
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--only", choices=["lint", "typecheck", "security"], default=None)
    parser.add_argument("--loop", action="store_true")
    args = parser.parse_args(argv)
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)

    if args.only == "lint":
        results = [
            run_command(
                "ruff", [sys.executable, "-m", "ruff", "check", "--output-format", "json", "."]
            )
        ]
    elif args.only == "typecheck":
        results = [
            run_command(
                "mypy",
                [
                    sys.executable,
                    "-m",
                    "mypy",
                    "src",
                    "loop",
                    "scripts",
                    "automation",
                    "video",
                    "apps",
                ],
                "typecheck-results.txt",
            )
        ]
    elif args.only == "security":
        results = [
            secret_scan(),
            docker_config_check(),
            run_command(
                "bandit",
                [
                    sys.executable,
                    "-m",
                    "bandit",
                    "-q",
                    "-lll",
                    "-r",
                    "src",
                    "loop",
                    "scripts",
                    "automation",
                    "video",
                    "apps",
                    "-f",
                    "json",
                ],
                "bandit-report.json",
            ),
        ]
    else:
        results = [
            check_no_env_files(),
            secret_scan(),
            synthetic_data_scan(),
            docker_config_check(),
            run_pytest(),
            run_evaluation_gate(),
            run_command("review", [sys.executable, "scripts/review.py"]),
            *run_external_tool_gates(),
        ]
    summary = {
        "status": "passed"
        if all(result.get("status") == "passed" for result in results)
        else "failed",
        "loop_mode": args.loop,
        "results": results,
    }
    security_results = [
        result
        for result in results
        if result.get("name")
        in {"secret-scan", "synthetic-data-scan", "container-config-check", "bandit"}
    ]
    write_json(
        "security-report.json",
        {
            "status": "passed"
            if all(result.get("status") == "passed" for result in security_results)
            else "failed",
            "security_pass_rate": round(
                sum(int(result.get("status") == "passed") for result in security_results)
                / len(security_results),
                4,
            )
            if security_results
            else 0.0,
            "results": security_results,
        },
    )
    write_json("verify-summary.json", summary)
    build_loop_summary()
    if summary["status"] == "passed":
        from scripts.build_evidence_bundle import main as build_bundle

        build_bundle()
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0 if summary["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
