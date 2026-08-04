"""Remove only known generated local artifacts."""

from __future__ import annotations

from pathlib import Path

SAFE_FILES = [
    Path(".coverage"),
]

SAFE_DIRS = [
    Path(".pytest_cache"),
    Path(".ruff_cache"),
    Path(".mypy_cache"),
]


def main() -> int:
    for path in SAFE_FILES:
        if path.exists() and path.is_file():
            path.unlink()
    for path in SAFE_DIRS:
        if path.exists() and path.is_dir():
            for child in sorted(path.rglob("*"), reverse=True):
                if child.is_file():
                    child.unlink()
                elif child.is_dir():
                    child.rmdir()
            path.rmdir()
    print("Removed only allowlisted generated artifacts.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
