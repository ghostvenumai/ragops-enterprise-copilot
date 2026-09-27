"""Bootstrap an isolated Qdrant service and exercise the production vector contract."""
# ruff: noqa: S603, S607

from __future__ import annotations

import importlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = ROOT / "evidence/product-v1/rc-live/qdrant.json"
COMPOSE_PROJECT_PREFIX = "ragops-enterprise-copilot-rc-qdrant"
COMPOSE_SERVICE = "rc-qdrant"
READY_TIMEOUT_SECONDS = 45


def compose_base(project: str) -> list[str] | None:
    docker = shutil.which("docker")
    standalone = shutil.which("docker-compose")
    if docker:
        executable = [docker, "compose"]
    elif standalone:
        executable = [standalone]
    else:
        return None
    return [
        *executable,
        "-p",
        project,
        "-f",
        "docker-compose.yml",
        "-f",
        "docker-compose.rc.yml",
        "--profile",
        "rc",
    ]


def parse_loopback_port(output: str) -> int | None:
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    if len(lines) != 1:
        return None
    match = re.fullmatch(r"127\.0\.0\.1:(\d{1,5})", lines[0])
    if match is None:
        return None
    port = int(match.group(1))
    return port if 1 <= port <= 65535 else None


def bounded_timeout(raw: str | None) -> int:
    try:
        value = int(raw or str(READY_TIMEOUT_SECONDS))
    except ValueError:
        value = READY_TIMEOUT_SECONDS
    return min(max(value, 1), 120)


def safe_collection_name() -> str:
    return f"rc_{uuid4().hex}"


def qdrant_client_available() -> bool:
    """Return whether the official client is installed in this interpreter."""
    return importlib.util.find_spec("qdrant_client") is not None


def is_docker_daemon_unavailable(output: str) -> bool:
    lowered = output.lower()
    return any(
        marker in lowered
        for marker in (
            "cannot connect to the docker daemon",
            "is the docker daemon running",
            "error during connect",
            "daemon not running",
            "temporary failure in name resolution",
            "network is unreachable",
            "connection timed out",
            "i/o timeout",
            "failed to resolve",
            "permission denied while trying to connect to the docker daemon socket",
            "operation not permitted",
        )
    )


def main() -> int:
    EVIDENCE.parent.mkdir(parents=True, exist_ok=True)
    project = f"{COMPOSE_PROJECT_PREFIX}-{uuid4().hex[:10]}"
    collection = safe_collection_name()
    evidence: dict[str, Any] = {
        "gate": "qdrant",
        "status": "BLOCKED",
        "reason": "Qdrant live scenario has not run",
        "tested_commit": _commit(),
        "timestamp": datetime.now(UTC).isoformat(),
        "qdrant_client_available": qdrant_client_available(),
        "compose_available": False,
        "compose_project": project,
        "compose_service": COMPOSE_SERVICE,
        "bootstrap_status": "NOT_RUN",
        "readiness_status": "NOT_RUN",
        "published_host": None,
        "published_port": None,
        "collection_created": False,
        "schema_status": "NOT_RUN",
        "integration_status": "NOT_EXECUTED",
        "scenario_status": "NOT_RUN",
        "cleanup_status": "NOT_RUN",
        "tenant_leakage": None,
        "checks": {},
        "gate_exit_code": 2,
    }
    evidence["compose_available"] = bool(shutil.which("docker") or shutil.which("docker-compose"))
    if not evidence["qdrant_client_available"]:
        return _finish(evidence, "BLOCKED", "qdrant-client dependency is unavailable", 2)
    base = compose_base(project)
    if base is None:
        return _finish(evidence, "BLOCKED", "Docker Compose is unavailable", 2)
    up = [*base, "up", "-d", "--no-deps", COMPOSE_SERVICE]
    started = _run(up)
    if started.returncode != 0:
        combined = started.stdout + started.stderr
        if is_docker_daemon_unavailable(combined):
            evidence["bootstrap_status"] = "BLOCKED"
            return _finish(
                evidence,
                "BLOCKED",
                "Docker daemon or image-registry network is unavailable",
                2,
            )
        evidence["bootstrap_status"] = "FAIL"
        return _finish(evidence, "FAIL", "isolated RC Qdrant service failed to start", 1)
    evidence["bootstrap_status"] = "PASS"

    client: Any = None
    index: Any = None
    integration_ok = False
    integration_reason = "Qdrant scenario failed"
    try:
        port_result = _run([*base, "port", COMPOSE_SERVICE, "6333"])
        port = parse_loopback_port(port_result.stdout) if port_result.returncode == 0 else None
        if port is None:
            evidence["readiness_status"] = "FAIL"
            integration_reason = "RC Qdrant did not publish exactly one loopback REST endpoint"
        else:
            evidence["published_host"] = "127.0.0.1"
            evidence["published_port"] = port
            qdrant_client = importlib.import_module("qdrant_client")

            endpoint = f"http://127.0.0.1:{port}"
            client = qdrant_client.QdrantClient(url=endpoint, timeout=2)
            deadline = time.monotonic() + bounded_timeout(
                os.getenv("RAGOPS_RC_QDRANT_READY_TIMEOUT_SECONDS")
            )
            while time.monotonic() < deadline:
                try:
                    client.get_collections()
                    evidence["readiness_status"] = "PASS"
                    break
                except Exception:  # noqa: BLE001 - live readiness polling
                    time.sleep(0.5)
            if evidence["readiness_status"] != "PASS":
                evidence["readiness_status"] = "FAIL"
                integration_reason = "RC Qdrant REST endpoint did not become ready"
            else:
                from ragops.vector.embedding import DeterministicEmbeddingProvider
                from ragops.vector.index import (
                    AuthorizedVectorScope,
                    QdrantVectorIndex,
                    deterministic_vector_id,
                )

                provider = DeterministicEmbeddingProvider(dimension=32)
                index = QdrantVectorIndex(
                    url=endpoint,
                    collection=collection,
                    dimension=provider.dimension,
                )
                index.ensure_schema()
                evidence["collection_created"] = True
                evidence["schema_status"] = "PASS"
                points = _scenario_points(provider)
                ids = [deterministic_vector_id(payload) for _, payload in points]
                first = index.upsert_chunks(points[:4])
                after_partial = client.count(collection, exact=True).count
                complete = index.upsert_chunks(points)
                repeated = index.upsert_chunks(points)
                final_count = client.count(collection, exact=True).count
                scope = AuthorizedVectorScope(
                    tenant_id="tenant-rc-a",
                    role="viewer",
                    workspace_ids=frozenset({"workspace-rc-a"}),
                    collection_ids=frozenset({"collection-rc-a"}),
                )
                query = provider.embed(["synthetic current contract"])[0]
                hits = index.search(query, scope, limit=100)
                returned_ids = {hit.vector_id for hit in hits}
                expected_current = ids[1]
                checks = {
                    "partial_write_count": first == 4 and after_partial == 4,
                    "full_write_count": complete == len(points),
                    "idempotent_upsert_count": repeated == len(points)
                    and final_count == len(points),
                    "deterministic_ids_unique": len(set(ids)) == len(ids),
                    "current_active_version_only": returned_ids == {expected_current},
                    "tenant_isolation": not any(
                        hit.payload.tenant_id != "tenant-rc-a" for hit in hits
                    ),
                    "workspace_and_collection_scope": all(
                        hit.payload.workspace_id == "workspace-rc-a"
                        and hit.payload.collection_id == "collection-rc-a"
                        for hit in hits
                    ),
                    "access_level_filter": all(
                        hit.payload.access_level in {"public", "internal"} for hit in hits
                    ),
                    "stale_inactive_deleted_excluded": all(
                        hit.payload.document_status == "indexed"
                        and hit.payload.version_status == "indexed"
                        for hit in hits
                    ),
                }
                # Probe the cross-tenant sentinel specifically: the vector exists,
                # but its authoritative tenant differs from the authenticated scope.
                attack_query = provider.embed(["cross tenant sentinel"])[0]
                attack_hits = index.search(attack_query, scope, limit=100)
                checks["cross_tenant_vector_attack_returns_zero"] = not any(
                    hit.payload.document_id == "doc-tenant-b" for hit in attack_hits
                )
                checks["expired_version_excluded"] = all(
                    hit.payload.valid_to is None
                    or datetime.fromisoformat(hit.payload.valid_to) > datetime.now(UTC)
                    for hit in hits
                )
                # Deletion is tenant-scoped and checked against persisted count.
                before_delete = client.count(collection, exact=True).count
                index.delete_version_vectors("tenant-rc-a", "version-rc-v1")
                after_delete = client.count(collection, exact=True).count
                checks["version_delete_removes_only_target"] = (
                    before_delete - after_delete == 1
                    and not client.retrieve(collection, ids=[ids[0]])
                )
                evidence["checks"] = checks
                evidence["vector_count_after_partial_write"] = after_partial
                evidence["vector_count_after_idempotent_replay"] = final_count
                evidence["returned_result_count"] = len(hits)
                evidence["tenant_leakage"] = (
                    0
                    if checks["tenant_isolation"]
                    and checks["cross_tenant_vector_attack_returns_zero"]
                    else 1
                )
                integration_ok = all(checks.values())
                evidence["integration_status"] = "PASS" if integration_ok else "FAIL"
                integration_reason = (
                    "all live Qdrant contract checks passed"
                    if integration_ok
                    else "one or more live Qdrant contract checks failed"
                )
                evidence["scenario_status"] = "PASS" if integration_ok else "FAIL"
    except Exception as exc:  # noqa: BLE001 - evidence must not contain raw service detail
        evidence["scenario_status"] = "FAIL"
        evidence["integration_status"] = "FAIL"
        integration_reason = f"Qdrant live scenario raised {type(exc).__name__}"
    finally:
        if client is not None:
            try:
                if client.collection_exists(collection):
                    client.delete_collection(collection)
                evidence["cleanup_status"] = (
                    "PASS" if not client.collection_exists(collection) else "FAIL"
                )
            except Exception:  # noqa: BLE001 - cleanup outcome is separately recorded
                evidence["cleanup_status"] = "FAIL"
            client.close()
        else:
            evidence["cleanup_status"] = "NOT_REQUIRED"
        stopped = _run([*base, "rm", "-sf", COMPOSE_SERVICE])
        evidence["service_cleanup_status"] = "PASS" if stopped.returncode == 0 else "FAIL"

    if evidence["cleanup_status"] == "FAIL" or evidence.get("service_cleanup_status") == "FAIL":
        integration_ok = False
        integration_reason = "Qdrant scenario completed but isolated resource cleanup failed"
    evidence["scenario_status"] = evidence.get("scenario_status", "NOT_RUN")
    return _finish(
        evidence,
        "PASS" if integration_ok else "FAIL",
        integration_reason,
        0 if integration_ok else 1,
    )


def _scenario_points(provider: Any) -> list[tuple[list[float], Any]]:
    from typing import cast

    from ragops.storage.models import AccessLevel
    from ragops.vector.index import VectorPayload

    now = datetime.now(UTC)
    fixtures = [
        (
            "tenant-rc-a",
            "workspace-rc-a",
            "collection-rc-a",
            "doc-current",
            "version-rc-v1",
            "indexed",
            "superseded",
            "internal",
            "rc stale v1",
        ),
        (
            "tenant-rc-a",
            "workspace-rc-a",
            "collection-rc-a",
            "doc-current",
            "version-rc-v2",
            "indexed",
            "indexed",
            "internal",
            "rc synthetic current contract",
        ),
        (
            "tenant-rc-b",
            "workspace-rc-a",
            "collection-rc-a",
            "doc-tenant-b",
            "version-tenant-b",
            "indexed",
            "indexed",
            "internal",
            "cross tenant sentinel",
        ),
        (
            "tenant-rc-a",
            "workspace-other",
            "collection-rc-a",
            "doc-other-workspace",
            "version-other-workspace",
            "indexed",
            "indexed",
            "internal",
            "rc synthetic current contract",
        ),
        (
            "tenant-rc-a",
            "workspace-rc-a",
            "collection-other",
            "doc-other-collection",
            "version-other-collection",
            "indexed",
            "indexed",
            "internal",
            "rc synthetic current contract",
        ),
        (
            "tenant-rc-a",
            "workspace-rc-a",
            "collection-rc-a",
            "doc-inactive",
            "version-inactive",
            "inactive",
            "indexed",
            "internal",
            "rc synthetic current contract",
        ),
        (
            "tenant-rc-a",
            "workspace-rc-a",
            "collection-rc-a",
            "doc-deleted",
            "version-deleted",
            "deleted",
            "indexed",
            "internal",
            "rc synthetic current contract",
        ),
        (
            "tenant-rc-a",
            "workspace-rc-a",
            "collection-rc-a",
            "doc-restricted",
            "version-restricted",
            "indexed",
            "indexed",
            "restricted",
            "rc synthetic current contract",
        ),
        (
            "tenant-rc-a",
            "workspace-rc-a",
            "collection-rc-a",
            "doc-expired",
            "version-expired",
            "indexed",
            "indexed",
            "internal",
            "rc synthetic current contract",
        ),
    ]
    texts = [item[-1] for item in fixtures]
    vectors = provider.embed(texts)
    points: list[tuple[list[float], VectorPayload]] = []
    for index, (fixture, vector) in enumerate(zip(fixtures, vectors, strict=True)):
        (
            tenant,
            workspace,
            collection,
            document,
            version,
            doc_status,
            version_status,
            access,
            _text,
        ) = fixture
        payload = VectorPayload(
            tenant_id=tenant,
            workspace_id=workspace,
            collection_id=collection,
            document_id=document,
            document_version_id=version,
            chunk_id=f"{version}:0",
            access_level=cast(AccessLevel, access),
            document_status=doc_status,
            version_status=version_status,
            content_hash=f"synthetic-hash-{index}",
            chunk_index=0,
            source_name="synthetic-rc-fixture.pdf",
            created_at=now.isoformat(),
            valid_from=now.isoformat(),
            valid_to=(now - timedelta(days=1)).isoformat()
            if version == "version-expired"
            else None,
            title="Synthetic RC Qdrant fixture",
            page_number=1,
        )
        points.append((vector, payload))
    return points


def _run(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, cwd=ROOT, capture_output=True, text=True, check=False)  # noqa: S603


def _finish(evidence: dict[str, Any], status: str, reason: str, exit_code: int) -> int:
    evidence.update(
        {
            "status": status,
            "reason": reason,
            "gate_exit_code": exit_code,
            "timestamp": datetime.now(UTC).isoformat(),
        }
    )
    EVIDENCE.write_text(json.dumps(evidence, indent=2), encoding="utf-8")
    return exit_code


def _commit() -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True, check=False
        )  # noqa: S603
    except OSError:
        return "unknown"
    return result.stdout.strip() or "unknown"


if __name__ == "__main__":
    sys.exit(main())
