"""Operator CLI for encrypted RAGOps backups: backup, verify (validate-only) and restore.

Connection URLs and the key come from the environment, never from arguments, so they do not
reach shell history: RAGOPS_DATABASE_URL, RAGOPS_QDRANT_URL, RAGOPS_QDRANT_COLLECTION,
RAGOPS_DATA_DIR (blob root = <data dir>/document-blobs) and RAGOPS_BACKUP_KEY_FILE. Restore
reads its target from RAGOPS_RESTORE_DATABASE_URL, refuses the primary database and requires
--confirm-target with the target database name. Output is a non-secret JSON summary.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.engine import make_url  # noqa: E402

from ragops import __version__  # noqa: E402
from ragops.ops import data_backup  # noqa: E402


def _required(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise SystemExit(f"{name} is required")
    return value


def _sources(database_env: str, collection: str, blob_root: Path) -> data_backup.DataSources:
    from qdrant_client import QdrantClient

    engine = create_engine(_required(database_env), hide_parameters=True)
    return data_backup.DataSources(
        engine, QdrantClient(url=_required("RAGOPS_QDRANT_URL")), collection, blob_root
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("backup")
    create.add_argument("--destination", type=Path, required=True)
    verify = commands.add_parser("verify")
    verify.add_argument("--artifact", type=Path, required=True)
    restore = commands.add_parser("restore")
    restore.add_argument("--artifact", type=Path, required=True)
    restore.add_argument("--target-collection", required=True)
    restore.add_argument("--target-blob-root", type=Path, required=True)
    restore.add_argument("--confirm-target", required=True)
    args = parser.parse_args(argv)

    key = data_backup.load_backup_key(Path(_required("RAGOPS_BACKUP_KEY_FILE")))
    blob_root = Path(os.getenv("RAGOPS_DATA_DIR", "data/synthetic")) / "document-blobs"
    collection = os.getenv("RAGOPS_QDRANT_COLLECTION", "ragops_vectors")
    try:
        if args.command == "backup":
            artifact = data_backup.create_data_backup(
                _sources("RAGOPS_DATABASE_URL", collection, blob_root),
                args.destination,
                key,
                app_version=__version__,
            )
            summary: dict[str, object] = {"status": "created", "artifact": artifact.name}
        elif args.command == "verify":
            verified = data_backup.verify_data_backup(args.artifact, key)
            summary = {"status": "verified", "record_counts": verified.manifest["record_counts"]}
        else:
            target = _sources(
                "RAGOPS_RESTORE_DATABASE_URL", args.target_collection, args.target_blob_root
            )
            if target.engine.url.database != args.confirm_target:
                raise data_backup.BackupError("--confirm-target does not name the target database")
            primary = make_url(_required("RAGOPS_DATABASE_URL")).database or ""
            report = data_backup.restore_data_backup(
                args.artifact, key, target, protected_databases={primary}
            )
            summary = {"status": "restored", "record_counts": report["record_counts"]}
    except data_backup.BackupError as exc:
        print(json.dumps({"status": "failed", "reason": str(exc)}))
        return 1
    print(json.dumps(summary))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
