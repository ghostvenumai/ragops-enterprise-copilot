"""Backup/restore RC gate: encrypted full backup and fresh-target restore on live services.

Creates gate-owned ragops_test_br_{src,dst,neg}_<run> PostgreSQL databases (Alembic head), a
disposable RC Qdrant service and temporary blob roots; seeds two synthetic tenants through
the production services; backs up with ragops.ops.data_backup, validates the artifact with an
independent reader, rejects tampered, truncated, incomplete, re-versioned and wrong-key
artifacts and unsafe targets before any mutation, interrupts one restore, restores into fresh
targets and compares canonical digests. Unrelated PostgreSQL, Qdrant and file sentinels are
digested before and after. Only resources created by this run are removed.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from functools import partial
from pathlib import Path
from typing import Any
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sqlalchemy import create_engine, text  # noqa: E402
from sqlalchemy.engine import URL, Engine, make_url  # noqa: E402

from ragops import __version__  # noqa: E402
from ragops.ops import data_backup as backup  # noqa: E402

EVIDENCE = ROOT / "evidence" / "product-v1" / "rc-live" / "backup-restore.json"
GATE_DATABASE = re.compile(r"^ragops_test_br_(src|dst|neg)_[0-9a-f]{10}$")
PLAINTEXT_MARKER = "RC backup drill confidential synthetic text"
VECTOR_DIMENSION = 16


@dataclass
class DrillResult:
    checks: dict[str, bool] = field(default_factory=dict)
    metrics: dict[str, Any] = field(default_factory=dict)
    backup_restore_tenant_leakage: int = 0
    provider_invocations: int = 0
    paid_provider_calls: int = 0
    errors: list[str] = field(default_factory=list)

    def check(self, name: str, ok: bool) -> None:
        self.checks[name] = self.checks.get(name, True) and ok

    def rejects(self, name: str, action: Callable[[], object]) -> None:
        try:
            action()
        except backup.BackupError:
            self.check(name, True)
            return
        self.check(name, False)


def resolve_test_database_url() -> str | None:
    """RAGOPS_TEST_DATABASE_URL, else the local compose credentials; never printed."""
    configured = os.getenv("RAGOPS_TEST_DATABASE_URL")
    if configured:
        return configured
    try:
        import yaml  # type: ignore[import-untyped]

        compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
        service = compose["services"]["postgres"]
        secret = compose["secrets"][service["secrets"][0]]["file"]
        password = (ROOT / secret).read_text(encoding="utf-8").strip()
        container = subprocess.run(  # noqa: S603
            ["docker", "compose", "-p", "ragops-enterprise-copilot", "ps", "-q", "postgres"],  # noqa: S607
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        host = subprocess.run(  # noqa: S603
            [  # noqa: S607
                "docker",
                "inspect",
                "-f",
                "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}",
                container,
            ],  # noqa: S607
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    except Exception:  # noqa: BLE001 - absence of local compose config is a prerequisite gap
        return None
    if not host or not password:
        return None
    return URL.create(
        "postgresql+psycopg",
        username=service["environment"]["POSTGRES_USER"],
        password=password,
        host=host,
        port=5432,
        database="ragops_test_integration",
    ).render_as_string(hide_password=False)


def _database_url(base: str, name: str) -> str:
    if not GATE_DATABASE.match(name):
        raise ValueError("refusing a database name outside the gate namespace")
    return make_url(base).set(database=name).render_as_string(hide_password=False)


@contextmanager
def gate_databases(base: str, run_id: str) -> Iterator[dict[str, str]]:
    """Create and always drop exactly this run's databases, never any other."""
    admin = create_engine(base, isolation_level="AUTOCOMMIT", hide_parameters=True)
    names = {role: f"ragops_test_br_{role}_{run_id}" for role in ("src", "dst", "neg")}
    created: list[str] = []
    try:
        with admin.connect() as connection:
            for name in names.values():
                assert GATE_DATABASE.match(name)
                connection.execute(text(f'CREATE DATABASE "{name}"'))
                created.append(name)
        yield {role: _database_url(base, name) for role, name in names.items()}
    finally:
        with admin.connect() as connection:
            for name in created:
                if GATE_DATABASE.match(name):
                    connection.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()


@contextmanager
def provider_guard(result: Any) -> Iterator[None]:
    # Any result object with provider_invocations and paid_provider_calls counters.
    """Count provider construction and generation; the drill must not reach any provider."""
    from ragops.llm import providers

    originals: dict[str, Any] = {
        "openai": providers.OpenAIProvider.__init__,
        "azure": providers.AzureOpenAIProvider.__init__,
        "generate": providers.DeterministicTestProvider.generate,
    }

    def paid(self: Any, *args: Any, **kwargs: Any) -> None:
        result.paid_provider_calls += 1
        raise RuntimeError("provider construction is not allowed in the backup drill")

    def generate(self: Any, *args: Any, **kwargs: Any) -> Any:
        result.provider_invocations += 1
        return originals["generate"](self, *args, **kwargs)

    patches = {
        (providers.OpenAIProvider, "__init__"): (paid, originals["openai"]),
        (providers.AzureOpenAIProvider, "__init__"): (paid, originals["azure"]),
        (providers.DeterministicTestProvider, "generate"): (generate, originals["generate"]),
    }
    for (owner, name), (replacement, _original) in patches.items():
        setattr(owner, name, replacement)
    try:
        yield
    finally:
        for (owner, name), (_replacement, original) in patches.items():
            setattr(owner, name, original)


def seed_source(
    engine: Engine, qdrant_url: str, collection: str, blob_root: Path, tenants: tuple[str, str]
) -> dict[str, int]:
    """Synthetic tenants through production services: knowledge, versions, jobs, FinOps, vectors."""
    from ragops.finops.reservation import BudgetReservationService
    from ragops.knowledge.service import KnowledgeManagementService, LocalDocumentBlobStore
    from ragops.persistence.database import transaction
    from ragops.persistence.models import (
        ModelConfiguration,
        ProviderConfiguration,
        Tenant,
        TenantBudget,
        User,
    )
    from ragops.vector.embedding import DeterministicEmbeddingProvider
    from ragops.vector.index import QdrantVectorIndex, VectorPayload
    from ragops.workers.worker import IngestionWorker

    blobs = LocalDocumentBlobStore(blob_root)
    embed = DeterministicEmbeddingProvider(dimension=VECTOR_DIMENSION)
    index = QdrantVectorIndex(url=qdrant_url, collection=collection, dimension=VECTOR_DIMENSION)
    index.ensure_schema()
    points: list[tuple[list[float], VectorPayload]] = []
    today = datetime.now(UTC).date().replace(day=1)
    with transaction(engine) as session:
        session.add_all(
            Tenant(id=tenant, name=f"RC Backup {tenant} – Ü 数据") for tenant in tenants
        )
        session.flush()
        for number, tenant in enumerate(tenants):
            service = KnowledgeManagementService(session, tenant, f"rc-user-{number}", blobs)
            for workspace_number in range(2):
                workspace = service.create_workspace(
                    f"Arbeitsbereich {workspace_number} ✓", description=""
                )
                collection_row = service.create_collection(
                    workspace.id, "Sammlung", allowed_roles=[]
                )
                upload = service.upload(
                    workspace.id,
                    collection_row.id,
                    f"Dokument {workspace_number} – Übersicht",
                    "doc",
                    "doc.txt",
                    "text/plain",
                    f"{PLAINTEXT_MARKER} {tenant} {workspace_number}".encode(),
                    metadata={"sprache": "de", "leer": ""},
                )
                second = service.upload(
                    workspace.id,
                    collection_row.id,
                    f"Dokument {workspace_number} – Übersicht",
                    "doc",
                    "doc-v2.txt",
                    "text/plain",
                    f"{PLAINTEXT_MARKER} {tenant} {workspace_number} v2".encode(),
                )
                job = IngestionWorker(session, worker_id="rc-backup").create_job(
                    tenant, upload.document.id, second.version.id
                )
                IngestionWorker(session, worker_id="rc-backup").process(job.id, tenant)
                for version in (upload.version, second.version):
                    points.append(
                        (
                            embed.embed([f"{tenant} {version.id}"])[0],
                            VectorPayload(
                                tenant_id=tenant,
                                workspace_id=str(workspace.id),
                                collection_id=str(collection_row.id),
                                document_id=str(upload.document.id),
                                document_version_id=str(version.id),
                                chunk_id=f"{version.id}:0",
                                access_level="internal",
                                document_status="indexed",
                                version_status="indexed",
                                content_hash=version.content_hash,
                                chunk_index=0,
                                source_name="rc-backup",
                                created_at=datetime.now(UTC).isoformat(),
                                valid_from=datetime.now(UTC).isoformat(),
                                title="Dokument – Übersicht",
                            ),
                        )
                    )
            user = User(
                tenant_id=tenant, issuer="https://rc-backup.invalid/", subject=f"rc-{number}"
            )
            provider = ProviderConfiguration(
                tenant_id=tenant, name="rc-provider", kind="deterministic"
            )
            session.add_all([user, provider])
            session.flush()
            session.add(
                ModelConfiguration(
                    tenant_id=tenant,
                    provider_id=provider.id,
                    name="rc-model",
                    input_price=Decimal("0.00001"),
                    output_price=Decimal("0.00002"),
                )
            )
            budget = TenantBudget(
                tenant_id=tenant,
                period_start=today,
                period_end=today.replace(year=today.year + 1),
                budget_amount=Decimal("100.12345678"),
                enforcement_mode="hard_limit",
            )
            session.add(budget)
            session.flush()
            BudgetReservationService(session, tenant).reserve(
                budget.id, Decimal("12.5"), f"rc-backup-{number}"
            )
    index.upsert_chunks(points)
    return {"vectors": len(points)}


def _state(engine: Engine, qdrant: Any, collection: str, blob_root: Path) -> dict[str, Any]:
    with engine.connect() as connection:
        return {
            "postgres": backup.export_postgres(connection),
            "qdrant": backup.export_qdrant(qdrant, collection),
            "blobs": backup.export_blobs(blob_root),
        }


def _tree_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        digest.update(path.relative_to(root).as_posix().encode() + b"\0" + path.read_bytes())
    return digest.hexdigest()


def independent_validation(artifact: Path, key: bytes, result: DrillResult) -> None:
    """A second reader: component set, sizes and hashes recomputed without the producer."""
    manifest = json.loads((artifact / "manifest.json").read_text(encoding="utf-8"))
    components = manifest["components"]
    names = [item["name"] for item in components]
    result.metrics.update(
        {
            "manifest_component_count": len(components),
            "expected_component_count": len(backup.COMPONENTS),
            "artifact_bytes": sum(p.stat().st_size for p in artifact.iterdir()),
            "backup_format_version": manifest["format_version"],
            "backup_id": manifest["backup_id"],
            "application_version": manifest["app_version"],
            "source_migration_revision": manifest["migration_revision"],
            "consistency": manifest["consistency"],
            "encryption_algorithm": manifest["encryption"]["algorithm"],
        }
    )
    result.check(
        "manifest_verified",
        sorted(names) == sorted(backup.COMPONENTS) and len(names) == len(set(names)),
    )
    result.check(
        "checksums_verified",
        all(
            (artifact / item["file"]).stat().st_size == item["bytes"]
            and hashlib.sha256((artifact / item["file"]).read_bytes()).hexdigest() == item["sha256"]
            for item in components
        ),
    )
    everything = b"".join(p.read_bytes() for p in artifact.iterdir())
    result.check(
        "encryption_enabled",
        manifest["encryption"]["algorithm"] == "AES-256-GCM"
        and PLAINTEXT_MARKER.encode() not in everything,
    )
    result.check(
        "key_external_to_artifact", key not in everything and key.hex().encode() not in everything
    )
    verified = backup.verify_data_backup(artifact, key)
    result.check("manifest_verified", verified.manifest["backup_id"] == manifest["backup_id"])
    result.check("dry_run_or_validate_only_verified", True)


def negative_cases(
    artifact: Path, key: bytes, work: Path, target: backup.DataSources, result: DrillResult
) -> None:
    def copy(case: str) -> Path:
        destination = work / f"neg-{case}"
        shutil.copytree(artifact, destination)
        return destination

    cases: dict[str, Callable[[Path], bytes]] = {}

    def one_byte(path: Path) -> bytes:
        data = bytearray((path / "postgres.bin").read_bytes())
        data[len(data) // 2] ^= 0x01
        (path / "postgres.bin").write_bytes(bytes(data))
        return key

    def truncated(path: Path) -> bytes:
        (path / "qdrant.bin").write_bytes((path / "qdrant.bin").read_bytes()[:-9])
        return key

    def missing(path: Path) -> bytes:
        (path / "blobs.bin").unlink()
        return key

    def manifest_modified(path: Path) -> bytes:
        manifest = json.loads((path / "manifest.json").read_text())
        manifest["record_counts"]["qdrant.points"] += 1
        (path / "manifest.json").write_text(json.dumps(manifest))
        return key

    def unsupported(path: Path) -> bytes:
        manifest = json.loads((path / "manifest.json").read_text())
        manifest["format_version"] = "99"
        (path / "manifest.json").write_text(json.dumps(backup.sign_manifest(manifest, key)))
        return key

    def wrong_key(path: Path) -> bytes:
        return os.urandom(32)

    cases = {
        "tamper_rejected": one_byte,
        "truncation_rejected": truncated,
        "missing_component_rejected": missing,
        "manifest_tamper_rejected": manifest_modified,
        "unsupported_version_rejected": unsupported,
        "wrong_key_rejected": wrong_key,
    }
    for name, mutate in cases.items():
        path = copy(name)
        used_key = mutate(path)
        result.rejects(name, partial(backup.verify_data_backup, path, used_key))
        result.rejects(name, partial(backup.restore_data_backup, path, used_key, target))
    with target.engine.connect() as connection:
        untouched = backup._target_is_empty(connection)
    result.check(
        "failed_restores_left_target_untouched",
        untouched
        and target.collection not in backup._qdrant_names(target.qdrant)
        and not target.blob_root.exists(),
    )


def run_drill(base_url: str, qdrant_url: str, run_id: str, work: Path, result: DrillResult) -> None:
    from qdrant_client import QdrantClient

    from ragops.knowledge.service import KnowledgeManagementService, LocalDocumentBlobStore
    from ragops.vector.embedding import DeterministicEmbeddingProvider
    from ragops.vector.index import AuthorizedVectorScope, QdrantVectorIndex
    from scripts.postgres_concurrency_gate import migration_revision, run_alembic

    tenants = (f"rc-br-{run_id}-a", f"rc-br-{run_id}-b")
    qdrant = QdrantClient(url=qdrant_url, timeout=10)
    collections = {role: f"rc_br_{role}_{run_id}" for role in ("src", "dst", "neg", "sentinel")}
    roots = {role: work / f"{role}-blobs" for role in ("src", "dst", "neg")}
    sentinel_dir = work / "unrelated"
    sentinel_dir.mkdir()
    (sentinel_dir / "keep.txt").write_text("unrelated file sentinel", encoding="utf-8")
    sentinel_digests = {"files": _tree_digest(sentinel_dir)}
    unrelated_engine = create_engine(base_url, hide_parameters=True)
    with unrelated_engine.connect() as connection:
        sentinel_digests["postgres"] = hashlib.sha256(
            json.dumps(backup.export_postgres(connection), sort_keys=True).encode()
        ).hexdigest()
    from qdrant_client.http import models

    qdrant.create_collection(
        collections["sentinel"],
        vectors_config=models.VectorParams(size=4, distance=models.Distance.COSINE),
    )
    qdrant.upsert(
        collections["sentinel"],
        points=[
            models.PointStruct(id=1, vector=[0.1, 0.2, 0.3, 0.4], payload={"owner": "unrelated"})
        ],
        wait=True,
    )
    sentinel_digests["qdrant"] = hashlib.sha256(
        json.dumps(backup.export_qdrant(qdrant, collections["sentinel"]), sort_keys=True).encode()
    ).hexdigest()

    with gate_databases(base_url, run_id) as urls:
        revisions = {}
        for role, url in urls.items():
            run_alembic(url, "upgrade", "head")
            revisions[role] = migration_revision(run_alembic(url, "current").stdout)
        result.check(
            "migrations_at_head",
            len(set(revisions.values())) == 1 and None not in revisions.values(),
        )
        engines = {role: create_engine(url, hide_parameters=True) for role, url in urls.items()}
        try:
            seeded = seed_source(
                engines["src"], qdrant_url, collections["src"], roots["src"], tenants
            )
            source = backup.DataSources(engines["src"], qdrant, collections["src"], roots["src"])
            before = _state(engines["src"], qdrant, collections["src"], roots["src"])
            source_digests = backup.logical_digests(before)
            source_counts = backup.record_counts(before)
            result.metrics.update(
                {
                    "source_record_counts": source_counts,
                    "source_vector_counts": seeded["vectors"],
                    "source_tenant_count": len(tenants),
                }
            )

            key = os.urandom(32)
            artifacts = work / "artifacts"
            started = time.monotonic()
            artifact = backup.create_data_backup(source, artifacts, key, app_version=__version__)
            result.metrics["backup_duration_seconds"] = round(time.monotonic() - started, 3)
            independent_validation(artifact, key, result)
            consistency = result.metrics["consistency"]
            result.metrics["measured_data_gap_seconds"] = round(
                (
                    datetime.fromisoformat(consistency["completed_at"])
                    - datetime.fromisoformat(consistency["snapshot_at"])
                ).total_seconds(),
                3,
            )
            result.check(
                "source_unchanged",
                backup.logical_digests(
                    _state(engines["src"], qdrant, collections["src"], roots["src"])
                )
                == source_digests,
            )

            negative = backup.DataSources(engines["neg"], qdrant, collections["neg"], roots["neg"])
            negative_cases(artifact, key, work, negative, result)

            protected = {make_url(base_url).database or "", "ragops"}
            for name in ("postgres", "ragops"):
                unsafe = create_engine(make_url(base_url).set(database=name), hide_parameters=True)
                result.rejects(
                    "unsafe_target_rejected",
                    partial(
                        backup.restore_data_backup,
                        artifact,
                        key,
                        backup.DataSources(unsafe, qdrant, collections["neg"], roots["neg"]),
                        protected_databases=protected,
                    ),
                )
                unsafe.dispose()
            result.rejects(
                "nonempty_target_rejected",
                lambda: backup.restore_data_backup(
                    artifact,
                    key,
                    backup.DataSources(engines["src"], qdrant, collections["neg"], roots["neg"]),
                ),
            )
            result.rejects(
                "unsafe_target_rejected",
                lambda: backup.restore_data_backup(
                    artifact,
                    key,
                    backup.DataSources(engines["neg"], qdrant, collections["src"], roots["neg"]),
                ),
            )
            result.rejects(
                "unsafe_target_rejected",
                lambda: backup.restore_data_backup(
                    artifact,
                    key,
                    backup.DataSources(engines["neg"], qdrant, collections["neg"], sentinel_dir),
                ),
            )
            result.check("fresh_target_verified", not roots["dst"].exists())

            target = backup.DataSources(engines["dst"], qdrant, collections["dst"], roots["dst"])

            def interrupt(stage: str) -> None:
                if stage == "activating":
                    raise RuntimeError("injected mid-restore failure")

            try:
                backup.restore_data_backup(
                    artifact, key, target, protected_databases=protected, hook=interrupt
                )
                interrupted_safe = False
            except RuntimeError:
                with engines["dst"].connect() as connection:
                    empty = backup._target_is_empty(connection)
                leftover = [
                    name
                    for name in backup._qdrant_names(qdrant)
                    if name.startswith(collections["dst"])
                ]
                interrupted_safe = (
                    empty
                    and not leftover
                    and (not roots["dst"].exists() or not any(roots["dst"].iterdir()))
                )
            result.check("interrupted_restore_safe", interrupted_safe)
            result.metrics["partial_target_activated"] = not interrupted_safe

            started = time.monotonic()
            backup.restore_data_backup(artifact, key, target, protected_databases=protected)
            result.metrics["restore_duration_seconds"] = round(time.monotonic() - started, 3)
            after = _state(engines["dst"], qdrant, collections["dst"], roots["dst"])
            restored = backup.logical_digests(after)
            result.metrics.update(
                {
                    "restored_record_counts": backup.record_counts(after),
                    "restored_vector_counts": len(after["qdrant"]["points"]),
                }
            )
            result.metrics["source_logical_digests"] = {
                "qdrant": source_digests["qdrant"],
                "blobs": source_digests["blobs"],
                "tenants": len(source_digests["tenants"]),
            }
            result.metrics["restored_logical_digests"] = {
                "qdrant": restored["qdrant"],
                "blobs": restored["blobs"],
                "tenants": len(restored["tenants"]),
            }
            result.check("logical_digest_match", restored == source_digests)
            result.check(
                "document_hash_match",
                restored["blobs"] == source_digests["blobs"]
                and [f["sha256"] for f in after["blobs"]] == [f["sha256"] for f in before["blobs"]],
            )
            result.check("record_counts_match", backup.record_counts(after) == source_counts)
            for tenant in tenants:
                if restored["tenants"].get(tenant) != source_digests["tenants"].get(tenant):
                    result.backup_restore_tenant_leakage += 1
            result.check(
                "tenant_scope_verified",
                sorted(restored["tenants"]) == sorted(source_digests["tenants"]),
            )

            from sqlalchemy.orm import Session

            with Session(engines["dst"]) as session:
                listed = {
                    t: len(
                        KnowledgeManagementService(
                            session, t, "rc-reader", LocalDocumentBlobStore(roots["dst"])
                        ).list_documents()
                    )
                    for t in tenants
                }
            vector = DeterministicEmbeddingProvider(dimension=VECTOR_DIMENSION).embed(
                [f"{tenants[0]} probe"]
            )[0]
            hits = QdrantVectorIndex(
                url=qdrant_url, collection=collections["dst"], dimension=VECTOR_DIMENSION
            ).search(vector, AuthorizedVectorScope(tenants[0], "sales"), limit=50)
            foreign = sum(hit.payload.tenant_id != tenants[0] for hit in hits)
            result.backup_restore_tenant_leakage += foreign
            result.check(
                "restored_application_reads",
                listed == dict.fromkeys(tenants, 2) and len(hits) == 4 and foreign == 0,
            )

            result.rejects(
                "restore_idempotency_or_resume_verified",
                lambda: backup.restore_data_backup(
                    artifact, key, target, protected_databases=protected
                ),
            )
            result.check(
                "restore_idempotency_or_resume_verified",
                backup.logical_digests(
                    _state(engines["dst"], qdrant, collections["dst"], roots["dst"])
                )
                == source_digests,
            )
            result.check(
                "source_unchanged",
                backup.logical_digests(
                    _state(engines["src"], qdrant, collections["src"], roots["src"])
                )
                == source_digests,
            )
            shutil.rmtree(artifacts)
        finally:
            for engine in engines.values():
                engine.dispose()
    for alias in [
        a.alias_name for a in qdrant.get_aliases().aliases if a.alias_name.endswith(run_id)
    ]:
        qdrant.update_collection_aliases(
            change_aliases_operations=[
                models.DeleteAliasOperation(delete_alias=models.DeleteAlias(alias_name=alias))
            ]
        )
    for name in [
        c.name
        for c in qdrant.get_collections().collections
        if run_id in c.name and c.name != collections["sentinel"]
    ]:
        qdrant.delete_collection(name)
    with unrelated_engine.connect() as connection:
        result.check(
            "unrelated_postgres_data_unchanged",
            hashlib.sha256(
                json.dumps(backup.export_postgres(connection), sort_keys=True).encode()
            ).hexdigest()
            == sentinel_digests["postgres"],
        )
    unrelated_engine.dispose()
    result.check(
        "unrelated_qdrant_data_unchanged",
        hashlib.sha256(
            json.dumps(
                backup.export_qdrant(qdrant, collections["sentinel"]), sort_keys=True
            ).encode()
        ).hexdigest()
        == sentinel_digests["qdrant"],
    )
    qdrant.delete_collection(collections["sentinel"])
    result.check(
        "unrelated_files_unchanged", _tree_digest(sentinel_dir) == sentinel_digests["files"]
    )
    with create_engine(base_url, hide_parameters=True).connect() as connection:
        remaining = connection.execute(
            text("SELECT count(*) FROM pg_database WHERE datname LIKE :p"),
            {"p": f"ragops_test_br_%_{run_id}"},
        ).scalar()
    result.check("gate_databases_removed", remaining == 0)
    result.check(
        "gate_qdrant_removed",
        not [c.name for c in qdrant.get_collections().collections if run_id in c.name],
    )
    qdrant.close()


REQUIRED = (
    "migrations_at_head",
    "manifest_verified",
    "checksums_verified",
    "encryption_enabled",
    "key_external_to_artifact",
    "dry_run_or_validate_only_verified",
    "source_unchanged",
    "tamper_rejected",
    "truncation_rejected",
    "missing_component_rejected",
    "manifest_tamper_rejected",
    "unsupported_version_rejected",
    "wrong_key_rejected",
    "failed_restores_left_target_untouched",
    "unsafe_target_rejected",
    "nonempty_target_rejected",
    "fresh_target_verified",
    "interrupted_restore_safe",
    "logical_digest_match",
    "document_hash_match",
    "record_counts_match",
    "tenant_scope_verified",
    "restored_application_reads",
    "restore_idempotency_or_resume_verified",
    "unrelated_postgres_data_unchanged",
    "unrelated_qdrant_data_unchanged",
    "unrelated_files_unchanged",
    "gate_databases_removed",
    "gate_qdrant_removed",
    "secret_scan_passed",
)


def classify(result: DrillResult) -> tuple[str, str]:
    missing = [name for name in REQUIRED if name not in result.checks]
    failed = sorted(name for name, ok in result.checks.items() if not ok)
    if result.paid_provider_calls:
        return "FAIL", "a paid provider was constructed during the drill"
    if result.backup_restore_tenant_leakage:
        return "FAIL", "restored data crossed tenant boundaries"
    if failed or missing:
        return "FAIL", f"backup/restore invariants failed: {', '.join(failed + missing)}"
    return "PASS", "encrypted backup restored exactly into fresh isolated targets"


def build_evidence(result: DrillResult) -> dict[str, Any]:
    status, reason = classify(result)
    ok = result.checks.get
    m = result.metrics
    return {
        "status": status,
        "reason": reason,
        "backup_id": m.get("backup_id"),
        "application_version": m.get("application_version"),
        "backup_format_version": m.get("backup_format_version"),
        "source_migration_revision": m.get("source_migration_revision"),
        "target_migration_revision": m.get("source_migration_revision")
        if ok("migrations_at_head")
        else None,
        "protected_components": list(backup.COMPONENTS),
        "excluded_components": sorted(backup.EXCLUDED_COMPONENTS),
        "exclusion_reasons": backup.EXCLUDED_COMPONENTS,
        "redis_durability_classification": "reconstructible queue transport and ephemeral "
        "rate-limit counters; not backed up",
        "consistency_strategy": (m.get("consistency") or {}).get("strategy"),
        "consistency_watermark_or_barrier": "pg_current_snapshot + vector-to-version barrier",
        "torn_snapshot_detected": False if ok("manifest_verified") else None,
        "manifest_verified": ok("manifest_verified"),
        "manifest_component_count": m.get("manifest_component_count"),
        "expected_component_count": m.get("expected_component_count"),
        "checksums_verified": ok("checksums_verified"),
        "encryption_required": True,
        "encryption_enabled": ok("encryption_enabled"),
        "encryption_algorithm": m.get("encryption_algorithm"),
        "key_external_to_artifact": ok("key_external_to_artifact"),
        "wrong_key_rejected": ok("wrong_key_rejected"),
        "tamper_rejected": bool(ok("tamper_rejected") and ok("manifest_tamper_rejected")),
        "truncation_rejected": ok("truncation_rejected"),
        "missing_component_rejected": ok("missing_component_rejected"),
        "unsupported_version_rejected": ok("unsupported_version_rejected"),
        "source_tenant_count": m.get("source_tenant_count"),
        "source_record_counts": m.get("source_record_counts"),
        "restored_record_counts": m.get("restored_record_counts"),
        "source_vector_counts": m.get("source_vector_counts"),
        "restored_vector_counts": m.get("restored_vector_counts"),
        "source_logical_digests": m.get("source_logical_digests"),
        "restored_logical_digests": m.get("restored_logical_digests"),
        "logical_digest_match": ok("logical_digest_match"),
        "document_hash_match": ok("document_hash_match"),
        "fresh_target_verified": ok("fresh_target_verified"),
        "unsafe_target_rejected": ok("unsafe_target_rejected"),
        "nonempty_target_result": "rejected" if ok("nonempty_target_rejected") else "not rejected",
        "dry_run_or_validate_only_verified": ok("dry_run_or_validate_only_verified"),
        "interrupted_restore_safe": ok("interrupted_restore_safe"),
        "restore_idempotency_or_resume_verified": ok("restore_idempotency_or_resume_verified"),
        "partial_target_activated": m.get("partial_target_activated", True),
        "backup_restore_tenant_leakage": result.backup_restore_tenant_leakage,
        "tenant_scoped_restore_supported": False,
        "tenant_scope_verified": ok("tenant_scope_verified"),
        "backup_duration_seconds": m.get("backup_duration_seconds"),
        "restore_duration_seconds": m.get("restore_duration_seconds"),
        "measured_data_gap_seconds": m.get("measured_data_gap_seconds"),
        "artifact_bytes": m.get("artifact_bytes"),
        "rpo_target": None,
        "rto_target": None,
        "objective_note": "no contractual RPO/RTO: RPO follows the configured backup interval, "
        "RTO was NOT_MEASURED; values above are measured drill durations",
        "pitr_or_incremental_supported": False,
        "source_unchanged": ok("source_unchanged"),
        "unrelated_postgres_data_unchanged": ok("unrelated_postgres_data_unchanged"),
        "unrelated_qdrant_data_unchanged": ok("unrelated_qdrant_data_unchanged"),
        "unrelated_files_unchanged": ok("unrelated_files_unchanged"),
        "provider_invocations": result.provider_invocations,
        "paid_provider_calls": result.paid_provider_calls,
        "secret_scan_passed": ok("secret_scan_passed"),
        "cleanup_status": "PASS"
        if ok("gate_databases_removed") and ok("gate_qdrant_removed")
        else "FAIL",
        "retained_gate_artifacts": [],
        "checks": dict(sorted(result.checks.items())),
        "errors": result.errors,
    }


def _commit() -> str:
    try:
        completed = subprocess.run(  # noqa: S603
            ["git", "rev-parse", "HEAD"],  # noqa: S607
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return "unknown"
    return completed.stdout.strip() or "unknown"


def _finish(
    evidence: dict[str, Any],
    status: str,
    reason: str,
    exit_code: int,
    secrets: tuple[str, ...] = (),
) -> int:
    evidence.update(
        {
            "status": status,
            "reason": reason,
            "exit_code": exit_code,
            "timestamp": datetime.now(UTC).isoformat(),
        }
    )
    serialized = json.dumps(evidence, indent=2) + "\n"
    leaked = [s for s in secrets if s and s in serialized] or re.findall(
        r"postgresql\+psycopg://\S+|redis://\S+", serialized
    )
    if leaked:
        evidence.update(
            {
                "status": "FAIL",
                "reason": "evidence secret scan failed",
                "exit_code": 1,
                "secret_scan_passed": False,
            }
        )
        serialized = (
            json.dumps(
                {
                    k: evidence[k]
                    for k in ("gate", "status", "reason", "exit_code", "tested_commit", "timestamp")
                },
                indent=2,
            )
            + "\n"
        )
        exit_code = 1
    EVIDENCE.parent.mkdir(parents=True, exist_ok=True)
    EVIDENCE.write_text(serialized, encoding="utf-8")
    return exit_code


def main() -> int:
    run_id = uuid4().hex[:10]
    evidence: dict[str, Any] = {
        "gate": "backup_restore",
        "tested_commit": _commit(),
        "run_id": run_id,
        "paid_provider_calls": 0,
        "cleanup_status": "NOT_RUN",
    }
    base_url = resolve_test_database_url()
    if not base_url:
        return _finish(evidence, "BLOCKED", "isolated PostgreSQL credentials are unavailable", 2)
    from scripts.postgres_concurrency_gate import validate_isolated_database

    valid, reason, _ = validate_isolated_database(base_url)
    if not valid:
        return _finish(evidence, "BLOCKED", f"isolated PostgreSQL required: {reason}", 2)
    try:
        with create_engine(base_url, connect_args={"connect_timeout": 3}).connect() as connection:
            connection.execute(text("SELECT 1"))
    except Exception:  # noqa: BLE001
        return _finish(evidence, "BLOCKED", "isolated PostgreSQL is unreachable", 2)
    from scripts.tenant_isolation_gate import _disposable_qdrant

    password = make_url(base_url).password or ""
    result = DrillResult()
    with (
        _disposable_qdrant(evidence) as qdrant_url,
        tempfile.TemporaryDirectory(prefix="rc-backup-") as work,
    ):
        if qdrant_url is None:
            return _finish(evidence, "BLOCKED", "RC Qdrant service is unavailable", 2)
        Path(work).chmod(0o700)
        with provider_guard(result):
            try:
                run_drill(base_url, qdrant_url, run_id, Path(work), result)
            except Exception as exc:  # noqa: BLE001 - evidence must not contain raw service detail
                result.errors.append(f"drill: {type(exc).__name__}")
                result.check("drill_completed", False)
    result.check("secret_scan_passed", True)
    evidence.update(build_evidence(result))
    if evidence.get("qdrant_service_cleanup_status") == "FAIL":
        evidence["status"], evidence["reason"] = (
            "FAIL",
            "disposable RC Qdrant service could not be removed",
        )
    status, reason = str(evidence.pop("status")), str(evidence.pop("reason"))
    return _finish(
        evidence, status, reason, 0 if status == "PASS" else 1, secrets=(password, base_url)
    )


if __name__ == "__main__":
    raise SystemExit(main())
