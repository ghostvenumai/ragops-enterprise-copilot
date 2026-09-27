from pathlib import Path

import pytest

from ragops.ops.backup import BackupError, create_backup, verify_backup
from ragops.ops.rate_limit import DeterministicRateLimiter


def test_backup_manifest_and_checksum(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "data.txt").write_text("safe", encoding="utf-8")
    backup = create_backup(
        source, tmp_path / "backups", app_version="0.2.0.dev0", schema_version="v1"
    )
    assert verify_backup(backup)["status"] == "verified"
    (backup / "data.txt").write_text("tampered", encoding="utf-8")
    with pytest.raises(BackupError):
        verify_backup(backup)


def test_rate_limit_is_tenant_and_user_aware() -> None:
    limiter = DeterministicRateLimiter(limit=1, window_seconds=60)
    assert limiter.check("tenant-a", "user-a", "rag").allowed
    assert not limiter.check("tenant-a", "user-a", "rag").allowed
    assert limiter.check("tenant-b", "user-a", "rag").allowed
