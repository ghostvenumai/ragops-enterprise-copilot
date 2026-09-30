"""A raw 32-byte backup key is binary data and must be loaded byte-exactly."""

from __future__ import annotations

import base64
import os

import pytest

pytest.importorskip("cryptography", reason="Install the persistence extra")

from ragops.ops import data_backup as backup


@pytest.mark.parametrize(
    "key",
    [
        b" " + bytes(range(1, 32)),  # leading space
        bytes(range(1, 32)) + b"\n",  # trailing newline
        b"\t" + bytes(range(100, 130)) + b"\r",  # whitespace on both ends
        b"\x0b" * 32,  # every byte is whitespace
    ],
)
def test_raw_keys_with_whitespace_boundary_bytes_are_preserved(tmp_path, key) -> None:
    assert len(key) == 32
    path = tmp_path / "backup.key"
    path.write_bytes(key)
    path.chmod(0o600)
    assert backup.load_backup_key(path) == key


def test_textual_base64_key_is_still_parsed_by_its_own_parser(tmp_path) -> None:
    key = os.urandom(32)
    path = tmp_path / "backup.key"
    path.write_text(base64.b64encode(key).decode() + "\n", encoding="ascii")
    path.chmod(0o600)
    assert backup.load_backup_key(path) == key


def test_raw_key_with_a_trailing_newline_is_rejected_not_truncated(tmp_path) -> None:
    path = tmp_path / "backup.key"
    path.write_bytes(os.urandom(32) + b"\n")
    path.chmod(0o600)
    with pytest.raises(backup.BackupError):
        backup.load_backup_key(path)
