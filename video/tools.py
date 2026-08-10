"""Safe wrappers for fixed local media-tool invocations."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path


def require_tool(name: str) -> str:
    path = shutil.which(name)
    if not path:
        raise RuntimeError(f"required system tool is missing: {name}")
    return path


def run_checked(
    command: list[str], *, cwd: Path, timeout: float, log_path: Path | None = None
) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(  # noqa: S603 - callers provide fixed, structured commands.
        command,
        cwd=cwd,
        text=True,
        capture_output=True,
        check=False,
        timeout=timeout,
    )
    if log_path:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text(completed.stdout + completed.stderr, encoding="utf-8")
    if completed.returncode != 0:
        raise RuntimeError(
            f"command failed ({completed.returncode}): {Path(command[0]).name}: "
            f"{completed.stderr[-1000:]}"
        )
    return completed
