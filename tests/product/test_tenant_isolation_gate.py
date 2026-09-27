"""Tenant-isolation gate contracts with local adapters; the live gate reuses the same scenarios."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest

pytest.importorskip("alembic", reason="Install declared persistence extra; product gate blocks")

from alembic import command
from alembic.config import Config
from scripts import rc_live_gate
from scripts import tenant_isolation_gate as gate
from sqlalchemy import create_engine, event, select
from sqlalchemy.engine import Engine

from ragops.knowledge.service import KnowledgeManagementService
from ragops.persistence.database import transaction
from ragops.persistence.models import Tenant, Workspace
from ragops.persistence.repository import TenantRepository
from ragops.vector.index import DeterministicVectorIndex, VectorHit
from ragops.workers.queue import InMemoryIngestionQueue

EXPECTED_CHECKS = {
    "repository_read_isolation",
    "repository_write_isolation",
    "repository_positive_control",
    "lifecycle_isolation",
    "lifecycle_positive_control",
    "ingestion_isolation",
    "ingestion_queue_delivery",
    "ingestion_positive_control",
    "vector_upsert",
    "vector_tenant_isolation",
    "vector_scope_isolation",
    "vector_positive_control",
    "finops_isolation",
    "finops_positive_control",
    "api_positive_control",
    "api_list_isolation",
    "api_direct_id_isolation",
    "admin_requires_role",
}


@pytest.fixture
def database(tmp_path: Path) -> Iterator[tuple[Engine, str]]:
    url = f"sqlite:///{tmp_path / 'tenant-gate.db'}"
    engine = create_engine(url)

    @event.listens_for(engine, "connect")
    def enable_foreign_keys(connection: object, record: object) -> None:
        connection.execute("PRAGMA foreign_keys=ON")  # type: ignore[attr-defined]

    with engine.begin() as connection:
        cfg = Config("alembic.ini")
        cfg.attributes["connection"] = connection
        command.upgrade(cfg, "head")
    yield engine, url
    engine.dispose()


def run(database: tuple[Engine, str], tmp_path: Path, index=None) -> gate.Probe:
    engine, url = database
    tenant_a, tenant_b = gate.rc_tenants("unit")
    return gate.run_scenarios(
        engine,
        url,
        InMemoryIngestionQueue(),
        index or DeterministicVectorIndex(dimension=gate.VECTOR_DIMENSION),
        tmp_path,
        tenant_a,
        tenant_b,
    )


def test_enforced_boundaries_pass_with_positive_controls(database, tmp_path) -> None:
    probe = run(database, tmp_path)
    assert set(probe.checks) == EXPECTED_CHECKS
    assert all(probe.checks.values()), probe.checks
    assert probe.unauthorized_access_count == 0
    assert (probe.vector_tenant_leakage, probe.vector_scope_leakage) == (0, 0)
    assert probe.cross_tenant_attempts >= 30
    assert gate.classify(probe, True, True) == (
        "PASS",
        "all implemented tenant boundaries denied cross-tenant access",
    )
    fields = gate.result_fields(probe)
    assert fields["tenant_leakage"] == 0
    assert fields["admin_cross_tenant_policy"] == gate.ADMIN_CROSS_TENANT_POLICY


def test_vector_filter_bypass_is_reported_as_fail(database, tmp_path) -> None:
    class UnfilteredIndex(DeterministicVectorIndex):
        def search(self, vector, scope, limit=10):
            return [VectorHit(key, 1.0, payload) for key, (_, payload) in self._records.items()]

    probe = run(database, tmp_path, UnfilteredIndex(dimension=gate.VECTOR_DIMENSION))
    assert probe.vector_tenant_leakage > 0
    assert gate.classify(probe, True, True)[0] == "FAIL"
    assert gate.result_fields(probe)["tenant_leakage"] > 0


def test_repository_filter_bypass_is_reported_as_fail(database, tmp_path, monkeypatch) -> None:
    def unscoped_get(self, model, record_id):
        return self.session.scalar(select(model).where(model.id == record_id))

    monkeypatch.setattr(TenantRepository, "get", unscoped_get)
    probe = run(database, tmp_path)
    assert probe.checks["repository_read_isolation"] is False
    assert probe.unauthorized_access_count > 0
    assert gate.classify(probe, True, True)[0] == "FAIL"


def test_list_filter_bypass_is_detected_in_service_and_api(database, tmp_path, monkeypatch) -> None:
    def unscoped_workspaces(self, limit=50, offset=0):
        return list(self.session.scalars(select(Workspace).limit(limit)))

    monkeypatch.setattr(KnowledgeManagementService, "list_workspaces", unscoped_workspaces)
    probe = run(database, tmp_path)
    assert probe.checks["repository_read_isolation"] is False
    assert probe.checks["api_list_isolation"] is False
    assert gate.classify(probe, True, True)[0] == "FAIL"


def test_cleanup_removes_only_gate_tenants(database, tmp_path) -> None:
    engine, _ = database
    with transaction(engine) as session:
        session.add(Tenant(id="tenant-foreign", name="Pre-existing host data"))
        session.flush()
        session.add(Workspace(tenant_id="tenant-foreign", name="Keep", slug="keep"))
    tenants = gate.rc_tenants("unit")
    before = gate.unrelated_row_counts(engine, tenants)
    run(database, tmp_path)
    assert gate.cleanup_database(engine, tenants) is True
    assert gate.unrelated_row_counts(engine, tenants) == before
    with transaction(engine) as session:
        assert session.scalars(select(Tenant.id)).all() == ["tenant-foreign"]


def test_unclean_cleanup_is_fail(database, tmp_path) -> None:
    probe = run(database, tmp_path)
    assert gate.classify(probe, False, True)[0] == "FAIL"
    assert gate.classify(probe, True, False)[0] == "FAIL"


def test_evidence_fields_contain_no_secrets_or_content(database, tmp_path) -> None:
    probe = run(database, tmp_path)
    serialized = json.dumps(gate.result_fields(probe))
    for forbidden in ("Bearer", "eyJ", "BEGIN", "sqlite:///", "Synthetic RC tenant-isolation"):
        assert forbidden not in serialized


@pytest.mark.parametrize(
    "url",
    [None, "postgresql+psycopg://rc:pw@127.0.0.1/ragops", "sqlite:///ragops_test_x.db"],
)
def test_missing_or_shared_database_is_blocked(monkeypatch, tmp_path, url) -> None:
    monkeypatch.setattr(gate, "EVIDENCE", tmp_path / "tenant-isolation.json")
    if url is None:
        monkeypatch.delenv("RAGOPS_TEST_DATABASE_URL", raising=False)
    else:
        monkeypatch.setenv("RAGOPS_TEST_DATABASE_URL", url)
    assert gate.main() == 2
    evidence = json.loads(gate.EVIDENCE.read_text())
    assert evidence["status"] == "BLOCKED"
    assert evidence["postgres_isolated_database"] is False


def test_unreachable_database_is_blocked_without_leaking_url(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(gate, "EVIDENCE", tmp_path / "tenant-isolation.json")
    monkeypatch.setenv(
        "RAGOPS_TEST_DATABASE_URL",
        "postgresql+psycopg://rcuser:TopSecretPw1@127.0.0.1:9/ragops_test_gate",
    )
    assert gate.main() == 2
    raw = gate.EVIDENCE.read_text()
    assert json.loads(raw)["status"] == "BLOCKED"
    assert "TopSecretPw1" not in raw and "postgresql+psycopg://" not in raw


def test_rc_runner_merges_detailed_tenant_evidence_and_keeps_prior_pass() -> None:
    assert "tenant_isolation" in rc_live_gate.DETAILED_EVIDENCE_GATES
    prior = {gate_name: "PASS" for gate_name in rc_live_gate.GATES[:7]}
    merged = rc_live_gate.merge_gate_statuses(
        prior, {"tenant_isolation"}, {"tenant_isolation": {"status": "PASS"}}
    )
    assert all(merged[name] == "PASS" for name in prior)
    assert merged["tenant_isolation"] == "PASS"
    assert merged["model_router"] == "BLOCKED"
    evidence = rc_live_gate.merge_detailed_gate_evidence(
        {"gate": "tenant_isolation", "status": "PASS", "tenant_leakage": 0},
        {"status": "FAIL", "detail": "fallback"},
        "abc123",
        "2026-09-27T00:00:00Z",
        "tenant_isolation",
    )
    assert (evidence["status"], evidence["tenant_leakage"]) == ("PASS", 0)
