from __future__ import annotations

import re
import tomllib
from datetime import UTC, datetime
from pathlib import Path

from scripts import qdrant_live_gate as gate

from ragops.vector.embedding import DeterministicEmbeddingProvider
from ragops.vector.index import AuthorizedVectorScope, build_authorized_vector_filter


def test_dynamic_port_parser_accepts_only_single_loopback_endpoint() -> None:
    assert gate.parse_loopback_port("127.0.0.1:6333\n") == 6333
    assert gate.parse_loopback_port("0.0.0.0:6333\n") is None
    assert gate.parse_loopback_port("[::]:6333\n") is None
    assert gate.parse_loopback_port("127.0.0.1:6333\n127.0.0.1:6334\n") is None
    assert gate.parse_loopback_port("127.0.0.1:70000\n") is None


def test_compose_targets_unique_project_exact_service_only(monkeypatch) -> None:
    monkeypatch.setattr(gate.shutil, "which", lambda name: "/usr/bin/docker")
    project = "ragops-enterprise-copilot-rc-qdrant-test123456"
    base = gate.compose_base(project)
    assert base is not None
    command = [*base, "up", "-d", "--no-deps", gate.COMPOSE_SERVICE]
    assert command[-1] == "rc-qdrant"
    assert "--remove-orphans" not in command
    assert "-p" in command and project in command
    compose = Path("docker-compose.rc.yml").read_text()
    service = compose.split("  rc-qdrant:", 1)[1].split("  rc-redis:", 1)[0]
    assert "image: qdrant/qdrant:v1.15.1" in service
    assert '"127.0.0.1::6333"' in service
    assert "read_only: true" in service
    assert "no-new-privileges:true" in service
    assert "cap_drop:" in service and "  - ALL" in service
    assert "mem_limit: 512m" in service


def test_compose_is_unavailable_without_docker_cli(monkeypatch) -> None:
    monkeypatch.setattr(gate.shutil, "which", lambda _name: None)
    assert gate.compose_base("project") is None


def test_missing_qdrant_client_is_blocked_before_bootstrap(tmp_path, monkeypatch) -> None:
    evidence_path = tmp_path / "qdrant.json"
    monkeypatch.setattr(gate, "EVIDENCE", evidence_path)
    monkeypatch.setattr(gate, "qdrant_client_available", lambda: False)
    monkeypatch.setattr(gate, "compose_base", lambda _project: (_ for _ in ()).throw(
        AssertionError("bootstrap must not run when dependency is missing")
    ))
    assert gate.main() == 2
    report = evidence_path.read_text()
    assert '"qdrant_client_available": false' in report
    assert "dependency is unavailable" in report


def test_available_client_proceeds_past_dependency_check(tmp_path, monkeypatch) -> None:
    evidence_path = tmp_path / "qdrant.json"
    monkeypatch.setattr(gate, "EVIDENCE", evidence_path)
    monkeypatch.setattr(gate, "qdrant_client_available", lambda: True)
    monkeypatch.setattr(gate, "compose_base", lambda _project: None)
    assert gate.main() == 2
    report = evidence_path.read_text()
    assert '"qdrant_client_available": true' in report
    assert "Docker Compose is unavailable" in report


def test_live_gate_has_no_deterministic_vector_fallback() -> None:
    source = Path("scripts/qdrant_live_gate.py").read_text()
    assert "DeterministicVectorIndex" not in source
    assert "QdrantVectorIndex" in source


def test_qdrant_dependency_metadata_is_pinned_and_consistent() -> None:
    metadata = tomllib.loads(Path("pyproject.toml").read_text())
    vector_extra = metadata["project"]["optional-dependencies"]["vector"]
    matches = [item for item in vector_extra if item.startswith("qdrant-client==")]
    assert matches == ["qdrant-client==1.15.1"]
    constraints = Path("constraints.txt").read_text()
    assert re.search(r"^qdrant-client==1\.15\.1$", constraints, re.MULTILINE)
    makefile = Path("Makefile").read_text()
    assert ".[dev,persistence,identity,vector]" in makefile


def test_qdrant_filter_has_server_side_tenant_lifecycle_access_and_validity() -> None:
    scope = AuthorizedVectorScope(
        "tenant-a",
        "viewer",
        workspace_ids=frozenset({"workspace-a"}),
        collection_ids=frozenset({"collection-a"}),
    )
    query = build_authorized_vector_filter(scope)
    must_keys = {item.get("key") for item in query["must"]}
    assert {
        "tenant_id",
        "workspace_id",
        "collection_id",
        "document_status",
        "version_status",
        "access_level",
        "valid_from",
    } <= must_keys
    assert any(
        "is_empty" in item and item["is_empty"]["key"] == "valid_to" for item in query["should"]
    )
    assert any(
        item.get("key") == "valid_to" and item.get("range", {}).get("gt")
        for item in query["should"]
    )


def test_live_fixture_covers_tenant_lifecycle_scope_and_expiry() -> None:
    provider = DeterministicEmbeddingProvider(dimension=32)
    points = gate._scenario_points(provider)
    payloads = [payload for _, payload in points]
    assert len(points) == 9
    assert any(item.tenant_id == "tenant-rc-b" for item in payloads)
    assert any(item.version_status == "superseded" for item in payloads)
    assert any(item.document_status == "inactive" for item in payloads)
    assert any(item.document_status == "deleted" for item in payloads)
    assert any(item.access_level == "restricted" for item in payloads)
    assert any(
        item.valid_to is not None and datetime.fromisoformat(item.valid_to) < datetime.now(UTC)
        for item in payloads
    )
    assert all(len(vector) == 32 for vector, _ in points)


def test_only_daemon_or_environment_network_absence_is_blocked() -> None:
    assert gate.is_docker_daemon_unavailable("Cannot connect to the Docker daemon")
    assert gate.is_docker_daemon_unavailable("failed to resolve registry host")
    assert not gate.is_docker_daemon_unavailable("invalid Qdrant image configuration")


def test_timeout_is_bounded() -> None:
    assert gate.bounded_timeout("0") == 1
    assert gate.bounded_timeout("999") == 120
    assert gate.bounded_timeout("oops") == gate.READY_TIMEOUT_SECONDS
