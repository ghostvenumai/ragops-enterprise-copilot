"""Encrypted, verified full backup and fresh-target restore of RAGOps durable state.

Protected components are the Alembic-managed PostgreSQL schema (logical export inside one
REPEATABLE READ snapshot), the Qdrant vector collection (configuration and points) and the
document blob store. Redis is excluded: it only carries queue messages whose job state is
persisted in PostgreSQL and ephemeral rate-limit counters.

Each component is serialized canonically and encrypted with AES-256-GCM under a key held
outside the artifact; the associated data binds every ciphertext to its backup id and
component name. The manifest is authenticated with HMAC-SHA256, using HKDF subkeys that are
separate from the encryption key. Restore verifies and decrypts everything before touching a
target, refuses non-empty or protected targets, stages Qdrant and blobs, commits PostgreSQL
last and removes staged state if any step fails, so a failed restore never activates.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import stat
import tempfile
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from hmac import compare_digest
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes, hmac
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from sqlalchemy import JSON, func, select, text
from sqlalchemy.engine import Connection, Engine

from ragops.persistence.models import Base, DocumentVersion

FORMAT_VERSION = "2"
SUPPORTED_FORMATS = frozenset({FORMAT_VERSION})
COMPONENTS = ("postgres", "qdrant", "blobs")
ENCRYPTION_ALGORITHM = "AES-256-GCM"
SYSTEM_DATABASES = frozenset({"postgres", "template0", "template1"})
EXCLUDED_COMPONENTS = {
    "redis": "queue messages are reconstructible from persisted ingestion jobs; rate-limit "
    "counters are ephemeral",
    "configuration": "non-secret configuration is versioned in the repository; runtime "
    "secrets are never backed up",
}
RestoreHook = Callable[[str], None]


class BackupError(ValueError):
    """The artifact or target is unsafe; nothing was activated."""


@dataclass(frozen=True)
class DataSources:
    engine: Engine
    qdrant: Any
    collection: str
    blob_root: Path


def load_backup_key(path: Path) -> bytes:
    """Read a 256-bit key from a file that is not readable by group or others."""
    if not path.is_file() or path.stat().st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise BackupError("backup key file must exist and be private (0600)")
    raw = path.read_bytes()
    if len(raw) == 32:
        # A raw key is binary data: any byte, including whitespace, is key material.
        return raw
    # Only the textual base64 format is normalized, by its own parser.
    try:
        key = base64.b64decode(raw.strip(), validate=True)
    except ValueError:  # binascii.Error is a ValueError subclass
        raise BackupError("backup key must be 32 raw or base64-encoded bytes") from None
    if len(key) != 32:
        raise BackupError("backup key must be 32 bytes")
    return key


def _subkey(key: bytes, purpose: str) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=purpose.encode()).derive(key)


def key_id(key: bytes) -> str:
    return hashlib.sha256(_subkey(key, "ragops-backup-key-id-v2")).hexdigest()[:16]


def _aad(backup_id: str, component: str) -> bytes:
    return f"ragops-backup:v{FORMAT_VERSION}:{backup_id}:{component}".encode()


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def _sign(manifest: dict[str, Any], key: bytes) -> str:
    signer = hmac.HMAC(_subkey(key, "ragops-backup-manifest-v2"), hashes.SHA256())
    signer.update(_canonical({k: v for k, v in manifest.items() if k != "signature"}))
    return signer.finalize().hex()


def sign_manifest(manifest: dict[str, Any], key: bytes) -> dict[str, Any]:
    return {**manifest, "signature": _sign(manifest, key)}


def _encode(value: Any, *, json_column: bool = False) -> Any:
    if value is None:
        return None
    if json_column:
        return {"$json": value}
    if isinstance(value, bool | int | str | float):
        return value
    if isinstance(value, Decimal):
        return {"$decimal": str(value)}
    if isinstance(value, datetime):
        return {"$datetime": value.isoformat()}
    if isinstance(value, date):
        return {"$date": value.isoformat()}
    if isinstance(value, UUID):
        return {"$uuid": str(value)}
    if isinstance(value, bytes | bytearray | memoryview):
        return {"$bytes": base64.b64encode(bytes(value)).decode()}
    raise BackupError(f"unsupported column value type {type(value).__name__}")


def _decode(value: Any) -> Any:
    if not isinstance(value, dict):
        return value
    marker, raw = next(iter(value.items()))
    decoders: dict[str, Callable[[Any], Any]] = {
        "$json": lambda v: v,
        "$decimal": Decimal,
        "$datetime": datetime.fromisoformat,
        "$date": date.fromisoformat,
        "$uuid": UUID,
        "$bytes": base64.b64decode,
    }
    return decoders[marker](raw)


def export_postgres(connection: Connection) -> dict[str, list[dict[str, Any]]]:
    """Every ORM table, rows ordered by primary key, values in a typed canonical form."""
    tables: dict[str, list[dict[str, Any]]] = {}
    for table in Base.metadata.sorted_tables:
        ordering = list(table.primary_key.columns) or list(table.columns)
        rows = connection.execute(select(table).order_by(*ordering)).mappings()
        tables[table.name] = [
            {
                column.name: _encode(row[column.name], json_column=isinstance(column.type, JSON))
                for column in table.columns
            }
            for row in rows
        ]
    return tables


def export_qdrant(client: Any, collection: str) -> dict[str, Any]:
    info = client.get_collection(collection)
    vectors = info.config.params.vectors
    points: list[dict[str, Any]] = []
    offset = None
    while True:
        batch, offset = client.scroll(
            collection, limit=256, offset=offset, with_payload=True, with_vectors=True
        )
        points.extend(
            {"id": str(point.id), "vector": list(point.vector), "payload": point.payload or {}}
            for point in batch
        )
        if offset is None:
            break
    return {
        "vector_size": int(vectors.size),
        "distance": str(vectors.distance.value),
        "payload_indexes": {
            name: str(schema.data_type.value)
            for name, schema in sorted(info.payload_schema.items())
        },
        "points": sorted(points, key=lambda point: point["id"]),
    }


def export_blobs(root: Path) -> list[dict[str, Any]]:
    files = sorted(path for path in root.rglob("*") if path.is_file()) if root.is_dir() else []
    return [
        {
            "path": path.relative_to(root).as_posix(),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "data": base64.b64encode(path.read_bytes()).decode(),
        }
        for path in files
    ]


def logical_digests(state: dict[str, Any]) -> dict[str, Any]:
    """Whole-component and per-tenant SHA-256 digests over the canonical exports."""
    tables: dict[str, list[dict[str, Any]]] = state["postgres"]
    tenants: dict[str, list[Any]] = {}
    for name, rows in tables.items():
        for row in rows:
            owner = row.get("tenant_id") if name != "tenants" else row.get("id")
            if isinstance(owner, str):
                tenants.setdefault(owner, []).append([name, row])
    for point in state["qdrant"]["points"]:
        owner = point["payload"].get("tenant_id")
        if isinstance(owner, str):
            tenants.setdefault(owner, []).append(["qdrant", point])
    return {
        "postgres": {
            name: hashlib.sha256(_canonical(rows)).hexdigest() for name, rows in tables.items()
        },
        "qdrant": hashlib.sha256(_canonical(state["qdrant"])).hexdigest(),
        "blobs": hashlib.sha256(_canonical(state["blobs"])).hexdigest(),
        "tenants": {
            tenant: hashlib.sha256(_canonical(sorted(items, key=_canonical))).hexdigest()
            for tenant, items in sorted(tenants.items())
        },
    }


def record_counts(state: dict[str, Any]) -> dict[str, int]:
    return {
        **{f"postgres.{name}": len(rows) for name, rows in state["postgres"].items()},
        "qdrant.points": len(state["qdrant"]["points"]),
        "blobs.files": len(state["blobs"]),
    }


def _migration_revision(connection: Connection) -> str | None:
    try:
        return connection.execute(text("SELECT version_num FROM alembic_version")).scalar()
    except Exception:  # noqa: BLE001 - an unmigrated target has no revision
        return None


def _check_barrier(tables: dict[str, list[dict[str, Any]]], qdrant: dict[str, Any]) -> None:
    """Every vector must reference a document version inside the PostgreSQL snapshot."""
    versions = {
        (row["tenant_id"], str(_decode(row["id"])))
        for row in tables.get(DocumentVersion.__tablename__, [])
    }
    for point in qdrant["points"]:
        version = point["payload"].get("document_version_id")
        if version and (point["payload"].get("tenant_id"), version) not in versions:
            raise BackupError("cross-store snapshot is torn: vector without document version")


def create_data_backup(
    sources: DataSources, destination: Path, key: bytes, *, app_version: str
) -> Path:
    """Write one encrypted, signed backup set; the directory appears only when complete."""
    if len(key) != 32:
        raise BackupError("backup key must be 32 bytes")
    backup_id = str(uuid4())
    snapshot_at = datetime.now(UTC)
    with sources.engine.connect() as connection:
        if connection.dialect.name == "postgresql":
            connection.execution_options(isolation_level="REPEATABLE READ")
        with connection.begin():
            watermark = (
                connection.execute(text("SELECT pg_current_snapshot()::text")).scalar()
                if connection.dialect.name == "postgresql"
                else None
            )
            revision = _migration_revision(connection)
            tables = export_postgres(connection)
    qdrant = export_qdrant(sources.qdrant, sources.collection)
    blobs = export_blobs(sources.blob_root)
    _check_barrier(tables, qdrant)
    state = {"postgres": tables, "qdrant": qdrant, "blobs": blobs}
    destination.mkdir(parents=True, exist_ok=True, mode=0o700)
    staging = Path(tempfile.mkdtemp(prefix=".backup-", dir=destination))
    try:
        cipher = AESGCM(_subkey(key, "ragops-backup-components-v2"))
        components = []
        for name in COMPONENTS:
            nonce = os.urandom(12)
            payload = nonce + cipher.encrypt(nonce, _canonical(state[name]), _aad(backup_id, name))
            path = staging / f"{name}.bin"
            path.write_bytes(payload)
            path.chmod(0o600)
            components.append(
                {
                    "name": name,
                    "file": path.name,
                    "bytes": len(payload),
                    "sha256": hashlib.sha256(payload).hexdigest(),
                }
            )
        manifest = sign_manifest(
            {
                "format_version": FORMAT_VERSION,
                "backup_id": backup_id,
                "created_at": datetime.now(UTC).isoformat(),
                "app_version": app_version,
                "migration_revision": revision,
                "components": components,
                "record_counts": record_counts(state),
                "tenant_count": len(logical_digests(state)["tenants"]),
                "excluded_components": EXCLUDED_COMPONENTS,
                "consistency": {
                    "strategy": "postgres REPEATABLE READ snapshot, then qdrant and blob export, "
                    "then a vector-to-document-version barrier check",
                    "postgres_snapshot": watermark,
                    "snapshot_at": snapshot_at.isoformat(),
                    "completed_at": datetime.now(UTC).isoformat(),
                    "residual_risk": "writes after snapshot_at are not captured",
                },
                "encryption": {
                    "algorithm": ENCRYPTION_ALGORITHM,
                    "key_id": key_id(key),
                    "associated_data": "ragops-backup:v<format>:<backup_id>:<component>",
                    "manifest_mac": "HMAC-SHA256 (HKDF subkey)",
                },
            },
            key,
        )
        (staging / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        (staging / "manifest.json").chmod(0o600)
        final = destination / f"backup-{backup_id}"
        os.replace(staging, final)
        final.chmod(0o700)
        return final
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise


@dataclass(frozen=True)
class VerifiedBackup:
    manifest: dict[str, Any]
    state: dict[str, Any]


def verify_data_backup(path: Path, key: bytes) -> VerifiedBackup:
    """Validate-only: signature, version, component set, sizes, hashes, decryption, counts."""
    manifest_path = path / "manifest.json"
    if not manifest_path.is_file():
        raise BackupError("backup manifest missing")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise BackupError("backup manifest is malformed") from None
    signature = manifest.get("signature")
    if not isinstance(signature, str) or not compare_digest(_sign(manifest, key), signature):
        raise BackupError("manifest authentication failed")
    if manifest.get("format_version") not in SUPPORTED_FORMATS:
        raise BackupError("unsupported backup format version")
    components = {item["name"]: item for item in manifest.get("components", [])}
    if sorted(components) != sorted(COMPONENTS) or len(manifest["components"]) != len(COMPONENTS):
        raise BackupError("backup components are missing or duplicated")
    expected_files = {item["file"] for item in components.values()} | {"manifest.json"}
    if {entry.name for entry in path.iterdir()} != expected_files:
        raise BackupError("backup contains missing or unexpected files")
    cipher = AESGCM(_subkey(key, "ragops-backup-components-v2"))
    state: dict[str, Any] = {}
    for name, item in components.items():
        payload = (path / item["file"]).read_bytes()
        if len(payload) != item["bytes"] or hashlib.sha256(payload).hexdigest() != item["sha256"]:
            raise BackupError(f"backup component {name} is truncated or modified")
        try:
            plaintext = cipher.decrypt(
                payload[:12], payload[12:], _aad(manifest["backup_id"], name)
            )
        except InvalidTag:
            raise BackupError(f"backup component {name} failed authentication") from None
        state[name] = json.loads(plaintext)
    if record_counts(state) != manifest["record_counts"]:
        raise BackupError("backup record counts do not match the manifest")
    _check_barrier(state["postgres"], state["qdrant"])
    return VerifiedBackup(manifest, state)


def _target_is_empty(connection: Connection) -> bool:
    return all(
        not connection.execute(select(func.count()).select_from(table)).scalar()
        for table in Base.metadata.sorted_tables
    )


def _qdrant_names(client: Any) -> set[str]:
    names = {item.name for item in client.get_collections().collections}
    names |= {alias.alias_name for alias in client.get_aliases().aliases}
    return names


def restore_data_backup(
    path: Path,
    key: bytes,
    target: DataSources,
    *,
    protected_databases: Iterable[str] = (),
    hook: RestoreHook | None = None,
) -> dict[str, Any]:
    """Restore a verified backup into empty targets; activate only if every step succeeds."""
    from qdrant_client.http import models

    verified = verify_data_backup(path, key)
    manifest, state = verified.manifest, verified.state
    database = target.engine.url.database or ""
    if database in SYSTEM_DATABASES or database in set(protected_databases):
        raise BackupError("restore target database is protected")
    if target.collection in _qdrant_names(target.qdrant):
        raise BackupError("restore target Qdrant collection or alias already exists")
    if target.blob_root.exists() and any(target.blob_root.iterdir()):
        raise BackupError("restore target blob root is not empty")
    with target.engine.connect() as connection:
        if _migration_revision(connection) != manifest["migration_revision"]:
            raise BackupError("restore target migration revision does not match the backup")
        if not _target_is_empty(connection):
            raise BackupError("restore target database is not empty")
    notify = hook or (lambda _stage: None)
    notify("verified")
    staging_collection = f"{target.collection}-staging-{manifest['backup_id'][:8]}"
    staging_blobs = (
        target.blob_root.parent / f".{target.blob_root.name}.staging-{manifest['backup_id'][:8]}"
    )
    connection = target.engine.connect()
    transaction = None
    blobs_activated = alias_created = False
    try:
        staging_blobs.mkdir(parents=True, mode=0o700)
        for item in state["blobs"]:
            relative = Path(item["path"])
            if relative.is_absolute() or ".." in relative.parts:
                raise BackupError("backup blob path escapes the restore root")
            data = base64.b64decode(item["data"])
            if hashlib.sha256(data).hexdigest() != item["sha256"]:
                raise BackupError("backup blob hash mismatch")
            destination = staging_blobs / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(data)
        notify("blobs_staged")
        qdrant = state["qdrant"]
        target.qdrant.create_collection(
            staging_collection,
            vectors_config=models.VectorParams(
                size=qdrant["vector_size"], distance=models.Distance(qdrant["distance"])
            ),
        )
        for field, schema in qdrant["payload_indexes"].items():
            target.qdrant.create_payload_index(
                staging_collection, field, models.PayloadSchemaType(schema)
            )
        points = [
            models.PointStruct(id=point["id"], vector=point["vector"], payload=point["payload"])
            for point in qdrant["points"]
        ]
        for start in range(0, len(points), 256):
            target.qdrant.upsert(staging_collection, points=points[start : start + 256], wait=True)
        notify("qdrant_staged")
        transaction = connection.begin()
        for table in Base.metadata.sorted_tables:
            rows = [
                {column: _decode(value) for column, value in row.items()}
                for row in state["postgres"].get(table.name, [])
            ]
            if rows:
                connection.execute(table.insert(), rows)
        notify("postgres_loaded")
        # Activation: blobs, then the Qdrant alias, then the PostgreSQL commit as the last
        # step; any failure before the commit is compensated below.
        if target.blob_root.exists():
            _move_into(staging_blobs, target.blob_root)
        else:
            os.replace(staging_blobs, target.blob_root)
        blobs_activated = True
        target.qdrant.update_collection_aliases(
            change_aliases_operations=[
                models.CreateAliasOperation(
                    create_alias=models.CreateAlias(
                        collection_name=staging_collection, alias_name=target.collection
                    )
                )
            ]
        )
        alias_created = True
        notify("activating")
        transaction.commit()
        return {
            "backup_id": manifest["backup_id"],
            "collection": staging_collection,
            "record_counts": manifest["record_counts"],
        }
    except Exception:
        if transaction is not None and transaction.is_active:
            transaction.rollback()
        if alias_created:
            target.qdrant.update_collection_aliases(
                change_aliases_operations=[
                    models.DeleteAliasOperation(
                        delete_alias=models.DeleteAlias(alias_name=target.collection)
                    )
                ]
            )
        if blobs_activated:
            # The target blob root was verified empty, so its contents are ours alone.
            for item in list(target.blob_root.iterdir()):
                if item.is_dir():
                    shutil.rmtree(item)
                else:
                    item.unlink()
        if target.qdrant.collection_exists(staging_collection):
            target.qdrant.delete_collection(staging_collection)
        shutil.rmtree(staging_blobs, ignore_errors=True)
        raise
    finally:
        connection.close()


def _move_into(staging: Path, root: Path) -> None:
    for item in staging.iterdir():
        os.replace(item, root / item.name)
    staging.rmdir()
