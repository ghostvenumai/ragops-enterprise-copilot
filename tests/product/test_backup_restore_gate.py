"""Backup/restore gate classification, blocking, name guards, evidence secrets and RC state."""

from __future__ import annotations

import json

import pytest
from scripts import backup_restore_gate as gate
from scripts import rc_live_gate

BASE = "postgresql+psycopg://rc:TopSecretPw1@127.0.0.1:9/ragops_test_integration"


def passing() -> gate.DrillResult:
    result = gate.DrillResult(checks=dict.fromkeys(gate.REQUIRED, True))
    result.metrics["partial_target_activated"] = False
    return result


def test_all_required_checks_pass() -> None:
    assert gate.classify(passing())[0] == "PASS"
    evidence = gate.build_evidence(passing())
    required_truths = {
        "manifest_verified": True,
        "checksums_verified": True,
        "encryption_enabled": True,
        "key_external_to_artifact": True,
        "wrong_key_rejected": True,
        "tamper_rejected": True,
        "truncation_rejected": True,
        "fresh_target_verified": True,
        "logical_digest_match": True,
        "backup_restore_tenant_leakage": 0,
        "partial_target_activated": False,
        "source_unchanged": True,
        "unrelated_postgres_data_unchanged": True,
        "paid_provider_calls": 0,
        "secret_scan_passed": True,
        "cleanup_status": "PASS",
    }
    assert {key: evidence[key] for key in required_truths} == required_truths


@pytest.mark.parametrize(
    "check", ["wrong_key_rejected", "interrupted_restore_safe", "logical_digest_match"]
)
def test_any_failed_or_missing_check_fails(check) -> None:
    failed = passing()
    failed.checks[check] = False
    assert gate.classify(failed)[0] == "FAIL"
    missing = passing()
    del missing.checks[check]
    assert check in gate.classify(missing)[1]


def test_tenant_leakage_and_paid_provider_fail() -> None:
    leaking = passing()
    leaking.backup_restore_tenant_leakage = 1
    assert gate.classify(leaking)[1] == "restored data crossed tenant boundaries"
    paid = passing()
    paid.paid_provider_calls = 1
    assert gate.classify(paid)[0] == "FAIL"


def test_gate_only_touches_its_own_database_names() -> None:
    assert gate._database_url(BASE, "ragops_test_br_dst_0123456789").endswith(
        "/ragops_test_br_dst_0123456789"
    )
    for name in ("ragops", "postgres", "ragops_test_integration", "ragops_test_br_dst_x; DROP"):
        with pytest.raises(ValueError):
            gate._database_url(BASE, name)


def test_missing_credentials_are_blocked(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(gate, "EVIDENCE", tmp_path / "backup-restore.json")
    monkeypatch.setattr(gate, "resolve_test_database_url", lambda: None)
    assert gate.main() == 2
    assert json.loads(gate.EVIDENCE.read_text())["status"] == "BLOCKED"


def test_unreachable_database_is_blocked_without_leaking_url(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(gate, "EVIDENCE", tmp_path / "backup-restore.json")
    monkeypatch.setattr(gate, "resolve_test_database_url", lambda: BASE)
    assert gate.main() == 2
    raw = gate.EVIDENCE.read_text()
    assert json.loads(raw)["status"] == "BLOCKED"
    assert "TopSecretPw1" not in raw and "postgresql+psycopg://" not in raw


def test_evidence_with_a_secret_is_replaced_by_a_failure(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(gate, "EVIDENCE", tmp_path / "backup-restore.json")
    evidence = {"gate": "backup_restore", "tested_commit": "abc", "detail": f"connect {BASE}"}
    assert gate._finish(evidence, "PASS", "ok", 0, secrets=("TopSecretPw1",)) == 1
    raw = gate.EVIDENCE.read_text()
    assert json.loads(raw)["status"] == "FAIL" and "TopSecretPw1" not in raw


def test_rc_runner_merges_backup_evidence_and_keeps_prior_pass() -> None:
    assert "backup_restore" in rc_live_gate.DETAILED_EVIDENCE_GATES
    prior = {name: "PASS" for name in rc_live_gate.GATES[:11]}
    merged = rc_live_gate.merge_gate_statuses(
        prior, {"backup_restore"}, {"backup_restore": {"status": "PASS"}}
    )
    assert all(merged[name] == "PASS" for name in prior)
    assert (merged["backup_restore"], merged["readiness_failure_recovery"]) == ("PASS", "BLOCKED")
