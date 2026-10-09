"""rag_query gate: classification, leakage counters, evidence contract, redaction, RC state."""

from __future__ import annotations

import json

import pytest
from scripts import rag_query_gate as gate
from scripts import rc_live_gate, security_gate

NONCE = "ENT114-A-" + "5" * 16
SECRET = "synthetic-" + "client-value"


def passing() -> gate.RagQueryResult:
    result = gate.RagQueryResult(checks=dict.fromkeys(gate.REQUIRED, True))
    result.qdrant_requests_observed = 4
    result.usage_records_written = 2
    return result


def test_complete_evidence_passes_and_states_the_contract() -> None:
    assert gate.classify(passing())[0] == "PASS"
    evidence = gate.build_evidence(passing())
    expected = {
        "gate": "rag_query",
        "real_api_upload_used": True,
        "worker_used": True,
        "real_chunks_written": True,
        "placeholder_vectors_remaining": 0,
        "productive_query_uses_qdrant": True,
        "productive_query_uses_vector_retriever": True,
        "productive_query_uses_context_builder": True,
        "productive_query_uses_model_router": True,
        "finops_enforced": True,
        "tenant_filter_enforced": True,
        "tenant_leakage": 0,
        "vector_tenant_leakage": 0,
        "citation_tenant_leakage": 0,
        "usage_tenant_leakage": 0,
        "budget_tenant_leakage": 0,
        "demo_retrieval_used": False,
        "unique_nonce_verified": True,
        "citations_verified": True,
        "usage_records_written": 2,
        "duplicate_usage_records": 0,
        "zero_result_abstained": True,
        "zero_result_provider_calls": 0,
        "zero_result_usage_records": 0,
        "negative_control_abstained": True,
        "accounting_failure_suppressed_answer": True,
        "hard_budget_limit_blocked_provider": True,
        "browser_vector_path_verified": True,
        "paid_provider_calls": 0,
        "cleanup_status": "PASS",
        "unrelated_data_unchanged": True,
    }
    assert {key: evidence[key] for key in expected} == expected
    assert evidence["qdrant_requests_observed"] == 4
    assert evidence["query_mode"] == "vector"


@pytest.mark.parametrize("check", sorted(gate.REQUIRED))
def test_a_failed_or_missing_check_fails(check) -> None:
    failed = passing()
    failed.checks[check] = False
    assert gate.classify(failed) == ("FAIL", f"rag_query invariant failed: {check}")
    missing = passing()
    del missing.checks[check]
    assert gate.classify(missing)[0] == "FAIL" and check in gate.classify(missing)[1]


@pytest.mark.parametrize(
    "counter",
    [
        "tenant_leakage",
        "vector_tenant_leakage",
        "citation_tenant_leakage",
        "usage_tenant_leakage",
        "budget_tenant_leakage",
        "duplicate_usage_records",
        "zero_result_provider_calls",
        "zero_result_usage_records",
        "placeholder_vectors_remaining",
        "paid_provider_calls",
    ],
)
def test_any_leakage_or_forbidden_activity_fails(counter) -> None:
    result = passing()
    setattr(result, counter, 1)
    status, reason = gate.classify(result)
    assert status == "FAIL" and counter in reason


def test_missing_qdrant_proof_fails() -> None:
    result = passing()
    result.qdrant_requests_observed = 0
    assert gate.classify(result) == ("FAIL", "no Qdrant request was observed for the query path")


def test_demo_fallback_fails() -> None:
    result = passing()
    result.demo_retrieval_used = True
    assert gate.classify(result) == ("FAIL", "demo retrieval was used in vector mode")


def test_missing_usage_records_fail() -> None:
    result = passing()
    result.usage_records_written = 1
    assert gate.classify(result)[0] == "FAIL"


def test_missing_nonce_proof_and_cleanup_failure_fail() -> None:
    for check in ("unique_nonce_verified", "gate_database_removed", "disposable_qdrant_removed"):
        result = passing()
        result.checks[check] = False
        assert gate.classify(result)[0] == "FAIL"
        assert gate.build_evidence(result)["cleanup_status" if "removed" in check else check] in {
            "FAIL",
            False,
        }


def test_missing_prerequisites_are_blocked(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(gate, "EVIDENCE", tmp_path / "rag-query.json")

    def unavailable(*args, **kwargs):
        raise gate.PrerequisiteMissing("local Keycloak admin credentials are unavailable")

    monkeypatch.setattr(gate, "run", unavailable)
    assert gate.main() == 2
    evidence = json.loads((tmp_path / "rag-query.json").read_text())
    assert evidence["status"] == "BLOCKED" and evidence["gate"] == "rag_query"


def test_too_little_memory_blocks_before_any_browser_work(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(gate, "EVIDENCE", tmp_path / "rag-query.json")
    monkeypatch.setattr(gate, "available_memory_gib", lambda: 3.4)
    started: list[str] = []
    monkeypatch.setattr(gate, "run", lambda *a, **k: started.append("run"))
    assert gate.main() == 2 and started == []
    assert "3.5 GiB" in json.loads((tmp_path / "rag-query.json").read_text())["reason"]


@pytest.mark.parametrize(
    "leak",
    [
        NONCE,
        "Bearer abcdefghijklmnopqrstuvwxyz0123456789",
        "postgresql+psycopg://rc:SyntheticPw123@127.0.0.1:5432/ragops_test_rag",
        "-----BEGIN RSA " + "PRIVATE KEY-----",
    ],
    ids=["nonce", "bearer", "database-url", "private-key"],
)
def test_evidence_with_a_secret_or_nonce_is_replaced_by_a_failure(
    monkeypatch, tmp_path, leak
) -> None:
    monkeypatch.setattr(gate, "EVIDENCE", tmp_path / "rag-query.json")
    evidence = {"gate": "rag_query", "tested_commit": "abc", "detail": f"x {leak} y"}
    assert gate._finish(evidence, "PASS", "ok", 0, secrets=(NONCE,)) == 1
    raw = (tmp_path / "rag-query.json").read_text()
    written = json.loads(raw)
    assert written["status"] == "FAIL" and written["secret_scan_passed"] is False
    assert leak not in raw and "detail" not in written


def test_nonce_proof_is_a_digest_never_the_nonce() -> None:
    proof = gate.nonce_proof(NONCE)
    assert NONCE not in proof and len(proof) == 64
    assert proof == gate.nonce_proof(NONCE)


def test_rc_runner_has_sixteen_gates_with_rag_query_before_security() -> None:
    assert len(rc_live_gate.GATES) == 16
    assert rc_live_gate.GATES[-2:] == ("rag_query", "security")
    assert "rag_query" in rc_live_gate.DETAILED_EVIDENCE_GATES
    prior = {name: "PASS" for name in rc_live_gate.GATES if name != "rag_query"}
    merged = rc_live_gate.merge_gate_statuses(
        prior, {"rag_query"}, {"rag_query": {"status": "PASS"}}
    )
    assert all(value == "PASS" for value in merged.values()) and len(merged) == 16
    assert rc_live_gate.merge_gate_statuses(prior, set(), {})["rag_query"] == "BLOCKED"


def test_security_has_twenty_seven_controls_and_validates_rag_query_evidence() -> None:
    assert len(security_gate.CONTROLS) == 27
    live = [control for control in security_gate.CONTROLS if control.kind == "live"]
    assert len(live) == 10
    (control,) = [c for c in security_gate.CONTROLS if c.evidence_file == "rag-query.json"]
    required = dict(control.required)
    for key in (
        "tenant_leakage",
        "vector_tenant_leakage",
        "citation_tenant_leakage",
        "usage_tenant_leakage",
        "budget_tenant_leakage",
        "paid_provider_calls",
    ):
        assert required[key] == 0
    assert required["status"] == "PASS" and required["cleanup_status"] == "PASS"
    assert required["demo_retrieval_used"] is False
    assert required["unique_nonce_verified"] is True and required["citations_verified"] is True
    assert required["secret_scan_passed"] is True
    assert dict(control.minimums)["qdrant_requests_observed"] == 1


# --------------------------------------------------------------------------- failure cleanup


@pytest.mark.parametrize(
    ("stage", "function"),
    [
        ("ingest_a", "ingest"),  # after the upload
        ("query_a", "verify_answer"),  # after indexing, during the Qdrant-backed query
        ("negative_controls", "negative_controls"),  # after a Qdrant query
        ("accounting_failure", "accounting_failure"),  # after provider success
    ],
)
def test_a_failing_stage_still_cleans_up_and_fails(monkeypatch, tmp_path, stage, function) -> None:
    from contextlib import contextmanager
    from types import SimpleNamespace

    monkeypatch.setattr(gate, "EVIDENCE", tmp_path / "rag-query.json")
    monkeypatch.setattr(gate.e2e, "ARTIFACTS", tmp_path / "browser-e2e")
    monkeypatch.setattr(gate, "available_memory_gib", lambda: 8.0)
    exits: list[str] = []

    @contextmanager
    def fake_environment(result, evidence, **options):
        assert options == {
            "query_mode": "vector",
            "observe_qdrant": True,
            "idp_down_dashboard": False,
        }
        (tmp_path / "work" / "data").mkdir(parents=True, exist_ok=True)
        try:
            yield SimpleNamespace(work=tmp_path / "work", client_secret=SECRET, users={})
        finally:
            exits.append("cleaned")
            for name in gate.CLEANUP_CHECKS + gate.UNRELATED_CHECKS:
                result.check(name, True)

    class FakeStore:
        def __init__(self, env) -> None:
            pass

        def close(self) -> None:
            exits.append("store_closed")

    monkeypatch.setattr(gate.e2e, "environment", fake_environment)
    monkeypatch.setattr(gate, "Store", FakeStore)
    for name in (
        "provision_accounting",
        "zero_result",
        "ingest",
        "verify_answer",
        "negative_controls",
        "browser_journey",
        "accounting_failure",
        "hard_budget",
    ):
        monkeypatch.setattr(gate, name, lambda *args, **kwargs: None)

    def broken(*args, **kwargs):
        raise RuntimeError(f"injected failure in {stage}")

    monkeypatch.setattr(gate, function, broken)
    assert gate.main() == 1
    assert exits == ["store_closed", "cleaned"]
    evidence = json.loads((tmp_path / "rag-query.json").read_text())
    assert evidence["status"] == "FAIL" and evidence["cleanup_status"] == "PASS"
    assert evidence["errors"] and evidence["errors"][0].startswith(f"{stage}:")
    assert SECRET not in json.dumps(evidence)


def test_a_cleanup_failure_is_a_gate_failure() -> None:
    result = passing()
    result.checks["keycloak_gate_objects_removed"] = False
    assert gate.classify(result)[0] == "FAIL"
    assert gate.build_evidence(result)["cleanup_status"] == "FAIL"
    result = passing()
    result.checks["unrelated_postgres_data_unchanged"] = False
    assert gate.classify(result)[0] == "FAIL"
    assert gate.build_evidence(result)["unrelated_data_unchanged"] is False
