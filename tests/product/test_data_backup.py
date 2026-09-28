"""Encrypted full backup and fresh-target restore on migrated SQLite and in-memory Qdrant."""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

pytest.importorskip("qdrant_client", reason="install the vector extra")

from alembic import command
from alembic.config import Config
from qdrant_client import QdrantClient
from qdrant_client.http import models
from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine

from ragops.knowledge.service import KnowledgeManagementService, LocalDocumentBlobStore
from ragops.ops import data_backup as backup
from ragops.persistence.database import transaction
from ragops.persistence.models import Tenant

SECRET_TEXT = b"Synthetic confidential content for tenant"


def _engine(path: Path) -> Engine:
    engine = create_engine(f"sqlite:///{path}")

    @event.listens_for(engine, "connect")
    def enable_foreign_keys(connection: object, record: object) -> None:
        connection.execute("PRAGMA foreign_keys=ON")  # type: ignore[attr-defined]

    with engine.begin() as connection:
        cfg = Config("alembic.ini")
        cfg.attributes["connection"] = connection
        command.upgrade(cfg, "head")
    return engine


@dataclass
class Drill:
    root: Path
    source: backup.DataSources
    qdrant: QdrantClient
    key: bytes
    artifact: Path

    def target(self, name: str) -> backup.DataSources:
        return backup.DataSources(
            _engine(self.root / f"{name}.db"), self.qdrant, name, self.root / f"{name}-blobs"
        )

    def state(self, sources: backup.DataSources) -> dict[str, object]:
        with sources.engine.connect() as connection:
            return {
                "postgres": backup.export_postgres(connection),
                "qdrant": backup.export_qdrant(self.qdrant, sources.collection),
                "blobs": backup.export_blobs(sources.blob_root),
            }


@pytest.fixture
def drill(tmp_path: Path) -> Iterator[Drill]:
    engine = _engine(tmp_path / "source.db")
    qdrant = QdrantClient(":memory:")
    qdrant.create_collection(
        "source", vectors_config=models.VectorParams(size=4, distance=models.Distance.COSINE)
    )
    blobs = LocalDocumentBlobStore(tmp_path / "source-blobs")
    with transaction(engine) as session:
        session.add_all([Tenant(id="tenant-a", name="A"), Tenant(id="tenant-b", name="B – Ü 数据")])
        session.flush()
        for index, tenant in enumerate(("tenant-a", "tenant-b")):
            service = KnowledgeManagementService(session, tenant, "user", blobs)
            workspace = service.create_workspace("Workspace", description="")
            collection = service.create_collection(workspace.id, "Collection")
            upload = service.upload(
                workspace.id,
                collection.id,
                "Dokument ✓",
                "key",
                "doc.txt",
                "text/plain",
                SECRET_TEXT + tenant.encode(),
            )
            qdrant.upsert(
                "source",
                points=[
                    models.PointStruct(
                        id=str(upload.version.id),
                        vector=[0.1 * (index + 1), 0.2, 0.3, 0.4],
                        payload={
                            "tenant_id": tenant,
                            "document_version_id": str(upload.version.id),
                        },
                    )
                ],
            )
    source = backup.DataSources(engine, qdrant, "source", tmp_path / "source-blobs")
    key = os.urandom(32)
    artifact = backup.create_data_backup(source, tmp_path / "backups", key, app_version="test")
    yield Drill(tmp_path, source, qdrant, key, artifact)


def test_restore_reproduces_every_component_exactly(drill) -> None:
    target = drill.target("restored")
    backup.restore_data_backup(drill.artifact, drill.key, target)
    before, after = (
        backup.logical_digests(drill.state(drill.source)),
        backup.logical_digests(drill.state(target)),
    )
    assert before == after
    assert sorted(before["tenants"]) == ["tenant-a", "tenant-b"]
    assert backup.record_counts(drill.state(target))["qdrant.points"] == 2


def test_artifact_is_encrypted_and_key_is_external(drill) -> None:
    blob = b"".join(path.read_bytes() for path in drill.artifact.iterdir())
    assert SECRET_TEXT not in blob and b"tenant-a" not in blob
    assert drill.key not in blob and json.dumps(list(drill.key)).encode() not in blob
    manifest = json.loads((drill.artifact / "manifest.json").read_text())
    assert manifest["encryption"]["algorithm"] == "AES-256-GCM"
    assert sorted(item["name"] for item in manifest["components"]) == sorted(backup.COMPONENTS)
    assert oct(drill.artifact.stat().st_mode & 0o777) == "0o700"


def _tamper(artifact: Path, case: str, key: bytes) -> bytes:
    component = artifact / "postgres.bin"
    manifest_path = artifact / "manifest.json"
    if case == "one_byte":
        data = bytearray(component.read_bytes())
        data[len(data) // 2] ^= 0x01
        component.write_bytes(bytes(data))
    elif case == "truncated":
        component.write_bytes(component.read_bytes()[:-7])
    elif case == "missing_component":
        component.unlink()
    elif case == "extra_file":
        (artifact / "unexpected.bin").write_bytes(b"x")
    elif case == "manifest_modified":
        manifest = json.loads(manifest_path.read_text())
        manifest["record_counts"]["qdrant.points"] += 1
        manifest_path.write_text(json.dumps(manifest))
    elif case == "unsupported_version":
        manifest = json.loads(manifest_path.read_text())
        manifest["format_version"] = "99"
        manifest_path.write_text(json.dumps(backup.sign_manifest(manifest, key)))
    elif case == "wrong_key":
        return os.urandom(32)
    return key


@pytest.mark.parametrize(
    "case",
    [
        "one_byte",
        "truncated",
        "missing_component",
        "extra_file",
        "manifest_modified",
        "unsupported_version",
        "wrong_key",
    ],
)
def test_corrupt_or_unauthenticated_artifacts_fail_before_target_mutation(drill, case) -> None:
    key = _tamper(drill.artifact, case, drill.key)
    target = drill.target(f"target-{case}")
    with pytest.raises(backup.BackupError):
        backup.verify_data_backup(drill.artifact, key)
    with pytest.raises(backup.BackupError):
        backup.restore_data_backup(drill.artifact, key, target)
    with target.engine.connect() as connection:
        assert backup._target_is_empty(connection)
    assert target.collection not in backup._qdrant_names(drill.qdrant)
    assert not target.blob_root.exists()


def test_error_messages_never_contain_plaintext(drill) -> None:
    with pytest.raises(backup.BackupError) as raised:
        backup.verify_data_backup(drill.artifact, os.urandom(32))
    assert "tenant" not in str(raised.value) and "Synthetic" not in str(raised.value)


@pytest.mark.parametrize("database", ["postgres", "template1", "ragops"])
def test_protected_and_system_databases_are_rejected(drill, database) -> None:
    # Never connected: the name guard must reject before any connection is opened.
    unreachable = create_engine(f"postgresql+psycopg://guard:guard@127.0.0.1:9/{database}")
    target = backup.DataSources(unreachable, drill.qdrant, "t", drill.root / "b")
    with pytest.raises(backup.BackupError, match="protected"):
        backup.restore_data_backup(
            drill.artifact, drill.key, target, protected_databases={"ragops"}
        )


def test_non_empty_and_existing_targets_are_rejected(drill) -> None:
    with pytest.raises(backup.BackupError, match="not empty"):
        backup.restore_data_backup(
            drill.artifact,
            drill.key,
            backup.DataSources(drill.source.engine, drill.qdrant, "fresh", drill.root / "x"),
        )
    with pytest.raises(backup.BackupError, match="already exists"):
        backup.restore_data_backup(
            drill.artifact,
            drill.key,
            backup.DataSources(
                _engine(drill.root / "empty.db"), drill.qdrant, "source", drill.root / "y"
            ),
        )
    occupied = drill.root / "occupied"
    occupied.mkdir()
    (occupied / "keep.txt").write_text("unrelated")
    with pytest.raises(backup.BackupError, match="blob root"):
        backup.restore_data_backup(
            drill.artifact,
            drill.key,
            backup.DataSources(_engine(drill.root / "e2.db"), drill.qdrant, "fresh2", occupied),
        )
    assert (occupied / "keep.txt").read_text() == "unrelated"


def test_revision_mismatch_is_rejected(drill) -> None:
    target = drill.target("old-schema")
    with target.engine.begin() as connection:
        connection.exec_driver_sql("UPDATE alembic_version SET version_num = '0005_ai_finops'")
    with pytest.raises(backup.BackupError, match="migration revision"):
        backup.restore_data_backup(drill.artifact, drill.key, target)


def test_torn_cross_store_snapshot_fails_closed(drill) -> None:
    drill.qdrant.upsert(
        "source",
        points=[
            models.PointStruct(
                id="00000000-0000-0000-0000-000000000001",
                vector=[0.4, 0.3, 0.2, 0.1],
                payload={"tenant_id": "tenant-a", "document_version_id": "missing-version"},
            )
        ],
    )
    with pytest.raises(backup.BackupError, match="torn"):
        backup.create_data_backup(drill.source, drill.root / "torn", drill.key, app_version="t")


@pytest.mark.parametrize(
    "stage", ["verified", "blobs_staged", "qdrant_staged", "postgres_loaded", "activating"]
)
def test_interrupted_restore_never_activates_and_rerun_succeeds(drill, stage) -> None:
    target = drill.target(f"interrupted-{stage}")

    def fail(current: str) -> None:
        if current == stage:
            raise RuntimeError(f"injected failure at {stage}")

    with pytest.raises(RuntimeError):
        backup.restore_data_backup(drill.artifact, drill.key, target, hook=fail)
    with target.engine.connect() as connection:
        assert backup._target_is_empty(connection)
    names = backup._qdrant_names(drill.qdrant)
    assert not any(name.startswith(target.collection) for name in names)
    assert not target.blob_root.exists() or not any(target.blob_root.iterdir())
    backup.restore_data_backup(drill.artifact, drill.key, target)
    assert backup.logical_digests(drill.state(target)) == backup.logical_digests(
        drill.state(drill.source)
    )
    with pytest.raises(backup.BackupError, match="already exists"):
        backup.restore_data_backup(drill.artifact, drill.key, target)


def test_key_file_must_be_private_and_sized(tmp_path) -> None:
    key_file = tmp_path / "backup.key"
    key_file.write_bytes(os.urandom(32))
    key_file.chmod(0o644)
    with pytest.raises(backup.BackupError, match="private"):
        backup.load_backup_key(key_file)
    key_file.chmod(0o600)
    assert len(backup.load_backup_key(key_file)) == 32
    key_file.write_bytes(b"short")
    with pytest.raises(backup.BackupError):
        backup.load_backup_key(key_file)
