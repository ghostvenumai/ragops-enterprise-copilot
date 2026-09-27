from __future__ import annotations

import hashlib
import json
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path


class BackupError(ValueError):
    pass


def create_backup(
    source: Path, destination: Path, *, app_version: str, schema_version: str
) -> Path:
    if not source.exists() or not source.is_dir():
        raise BackupError("backup source is unavailable")
    destination.mkdir(parents=True, exist_ok=True)
    temp = Path(tempfile.mkdtemp(prefix=".backup-", dir=destination))
    try:
        artifacts: list[str] = []
        for path in sorted(source.rglob("*")):
            if path.is_file():
                relative = path.relative_to(source)
                target = temp / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(path.read_bytes())
                artifacts.append(str(relative))
        checksums = {
            name: hashlib.sha256((temp / name).read_bytes()).hexdigest() for name in artifacts
        }
        (temp / "checksums.sha256").write_text(
            "".join(f"{digest}  {name}\n" for name, digest in checksums.items()), encoding="utf-8"
        )
        (temp / "manifest.json").write_text(
            json.dumps(
                {
                    "format_version": "1",
                    "created_at": datetime.now(UTC).isoformat(),
                    "app_version": app_version,
                    "schema_version": schema_version,
                    "artifacts": artifacts,
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        final = destination / datetime.now(UTC).strftime("backup-%Y%m%dT%H%M%SZ")
        os.replace(temp, final)
        return final
    except Exception:
        for path in sorted(temp.rglob("*"), reverse=True):
            if path.is_file():
                path.unlink()
            elif path.is_dir():
                path.rmdir()
        temp.rmdir()
        raise


def verify_backup(path: Path) -> dict[str, object]:
    manifest_path = path / "manifest.json"
    checksums_path = path / "checksums.sha256"
    if not manifest_path.is_file() or not checksums_path.is_file():
        raise BackupError("backup manifest or checksums missing")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("format_version") != "1":
        raise BackupError("unsupported backup format")
    verified = 0
    for line in checksums_path.read_text(encoding="utf-8").splitlines():
        digest, name = line.split("  ", 1)
        target = path / name
        if not target.is_file() or hashlib.sha256(target.read_bytes()).hexdigest() != digest:
            raise BackupError("backup checksum mismatch")
        verified += 1
    return {
        "status": "verified",
        "artifacts": verified,
        "format_version": manifest["format_version"],
    }
