"""Execute bounded source/behavior checks; never claim a full enterprise review."""

from __future__ import annotations

import ast
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
EVIDENCE_DIR = REPO_ROOT / "evidence"
sys.path.insert(0, str(REPO_ROOT))


def check_fixed_loop_command() -> bool:
    from loop.controller import Task, build_codex_command

    command = build_codex_command(
        Task(task_id="review", title="review", description="", priority=0)
    )
    return (
        command[:6]
        == ["codex", "exec", "--sandbox", "workspace-write", "--ask-for-approval", "never"]
        and len(command) == 7
    )


def check_no_shell_execution() -> bool:
    tree = ast.parse((REPO_ROOT / "loop/controller.py").read_text(encoding="utf-8"))
    calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)]
    for call in calls:
        if isinstance(call.func, ast.Attribute) and call.func.attr in {"run", "Popen", "call"}:
            for keyword in call.keywords:
                if keyword.arg == "shell" and not (
                    isinstance(keyword.value, ast.Constant) and keyword.value.value is False
                ):
                    return False
    return True


def check_admin_boundary() -> bool:
    from ragops.auth.rbac import AuthorizationError, require_tenant
    from ragops.storage.models import QueryUser

    try:
        require_tenant(QueryUser("synthetic-review", "tenant-alpha", "admin"), "tenant-beta")
    except AuthorizationError:
        return True
    return False


def main() -> int:
    EVIDENCE_DIR.mkdir(exist_ok=True)
    checks = {
        "fixed_loop_arguments": check_fixed_loop_command(),
        "no_shell_keyword_in_loop": check_no_shell_execution(),
        "admin_tenant_boundary": check_admin_boundary(),
    }
    report = {
        "status": "passed" if all(checks.values()) else "failed",
        "scope": "bounded executable regression checks, not independent security review",
        "checks": checks,
        "production_readiness": "blocked",
        "production_findings": "docs/PRODUCTIZATION_AUDIT.md",
        "limitations": [
            "JWT, migrations, persistence, worker recovery and restore remain unverified",
            "Does not replace separate adversarial enterprise review",
        ],
    }
    (EVIDENCE_DIR / "review-results.json").write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    lines = ["# Bounded automated review", "", f"Result: {report['status']}", ""]
    lines.extend(f"- {name}: {'passed' if passed else 'failed'}" for name, passed in checks.items())
    lines.extend(
        [
            "",
            "Production readiness remains blocked. See docs/PRODUCTIZATION_AUDIT.md.",
            "These checks do not constitute an independent security or architecture review.",
            "",
        ]
    )
    (EVIDENCE_DIR / "architecture-review.md").write_text("\n".join(lines), encoding="utf-8")
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
