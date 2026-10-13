"""The document_intelligence gate: registry, verdict logic, evidence contract (ENT-11.8)."""

from __future__ import annotations

import json

import pytest
from scripts import document_intelligence_gate as gate
from scripts import intelligence_fixtures as corpus
from scripts import rc_live_gate, security_gate


def passing() -> gate.DiGateResult:
    result = gate.DiGateResult(ocr_engine="tesseract")
    for name in gate.REQUIRED:
        result.check(name, True)
    return result


def test_gate_is_the_twentieth_release_gate_before_web_and_security() -> None:
    assert len(rc_live_gate.GATES) == 21
    assert rc_live_gate.GATES[-3:] == ("document_intelligence", "web_e2e", "security")
    assert "document_intelligence" in rc_live_gate.DETAILED_EVIDENCE_GATES


def test_live_14_requires_every_mandatory_check() -> None:
    (control,) = [c for c in security_gate.CONTROLS if c.id == "LIVE-14"]
    assert control.kind == "live" and control.evidence_file == "document-intelligence.json"
    required = dict(control.required)
    for name in (
        "document_classification_verified",
        "creditor_extraction_verified",
        "debtor_attribution_verified",
        "ambiguous_debtor_abstained",
        "amount_types_verified",
        "dates_and_references_verified",
        "source_page_verified",
        "provenance_verified",
        "duplicate_claims_not_double_counted",
        "same_amount_distinct_claims_not_merged",
        "review_workflow_verified",
        "concurrent_corrections_safe",
        "idempotent_reprocessing",
        "low_quality_ocr_not_treated_as_verified",
        "structured_search_verified",
        "tenant_isolation_verified",
        "access_control_verified",
        "injection_resistance_verified",
        "forged_source_rejected",
        "fabricated_amount_rejected",
        "person_identity_collision_rejected",
        "claim_group_poisoning_rejected",
    ):
        assert required[name] is True, name
    assert required["paid_provider_calls"] == 0 and required["cleanup_status"] == "PASS"
    assert required["wrong_debtor_attributions"] == 0 and required["false_claim_merges"] == 0
    assert required["cross_tenant_leakage"] == 0
    assert dict(control.minimums)["concurrent_reviewers"] == 10
    assert dict(control.maximums) == {"concurrent_applied": 1}


def test_evidence_provides_every_field_live_14_reads() -> None:
    (control,) = [c for c in security_gate.CONTROLS if c.id == "LIVE-14"]
    evidence = gate.build_evidence(passing(), {"corpus_digest": corpus.corpus_digest()})
    evidence.update({"status": "PASS", "secret_scan_passed": True})
    fields = {key for key, _ in control.required} | {key for key, _ in control.minimums}
    fields |= {key for key, _ in control.maximums}
    assert fields <= set(evidence)


def test_verdict_requires_every_check_and_zero_counters() -> None:
    assert gate.classify(passing())[0] == "PASS"
    missing = passing()
    del missing.checks["idempotent_reprocessing"]
    assert gate.classify(missing) == (
        "FAIL",
        "document_intelligence invariant not executed: idempotent_reprocessing",
    )
    failed = passing()
    failed.check("concurrent_corrections_safe", False)
    assert gate.classify(failed)[0] == "FAIL"
    for counter in gate.ZERO_COUNTERS:
        bad = passing()
        setattr(bad, counter, 1)
        assert gate.classify(bad) == ("FAIL", f"{counter} must be 0")
    scripted = passing()
    scripted.ocr_engine = "scripted"
    assert gate.classify(scripted)[0] == "FAIL"


@pytest.mark.parametrize(
    "name",
    ["wrong_debtor_attributions", "false_claim_merges"],
)
def test_high_severity_errors_are_counted_not_tolerated(name: str) -> None:
    assert name in gate.ZERO_COUNTERS


def test_gate_never_mocks_extraction_ocr_or_providers() -> None:
    source = (gate.ROOT / "scripts" / "document_intelligence_gate.py").read_text()
    for forbidden in (
        "ScriptedEngine",
        "ocr_helpers",
        "OpenAIProvider",
        "AzureOpenAI",
        "RAGOPS_ALLOW_PAID_INTEGRATION_TESTS",
        "persist_analysis",
        "analyze(",
    ):
        assert forbidden not in source, forbidden


def test_poison_and_low_quality_fixtures_are_meaningful() -> None:
    poison = " ".join(gate.POISON_PAGES[0])
    assert "VL-2026-0147" in poison and "Versandhaus" not in poison
    assert gate.LOW_QUALITY_SCAN["noise"] > 0 and gate.LOW_QUALITY_SCAN["blur"] > 0
    assert len(gate.TWO_PAGE) == 2


def test_evidence_contains_no_corpus_text() -> None:
    evidence = gate.build_evidence(passing(), {"corpus_digest": corpus.corpus_digest()})
    serialized = json.dumps(evidence)
    lines = corpus.all_text() + [
        line for page in gate.POISON_PAGES + gate.TWO_PAGE for line in page
    ]
    assert not [line for line in lines if line in serialized]


def test_listed_documents_are_mapped_by_id_not_title() -> None:
    run = gate.Run.__new__(gate.Run)
    run.docs = {"invoice_c1": gate.Uploaded("invoice_c1", document_id="d-1")}
    listed = [{"document_id": "d-1", "title": "invoice_c1.pdf"}, {"document_id": "x"}]
    assert run.keys_of(listed) == {"invoice_c1", "x"}
    assert run.keys_of(None) == set()


def test_throttled_call_waits_for_the_servers_lowercase_retry_after(monkeypatch) -> None:
    # Regression: uvicorn sends "retry-after"; a case-sensitive lookup fell back to 5 s, four
    # short waits ended inside the 60 s window and the snapshot read returned 429.
    answers = iter([(429, None, {"retry-after": "37"}), (200, {"ok": True}, {})])
    slept: list[float] = []
    monkeypatch.setattr(gate.e2e, "http_json", lambda *args, **kwargs: next(answers))
    monkeypatch.setattr(gate.time, "sleep", slept.append)
    api = gate.Api.__new__(gate.Api)
    api.env = type("Env", (), {"api_url": "http://api"})()
    api.result, api._tokens, api._lock = passing(), {"a-admin": "t"}, gate.threading.Lock()
    assert api.call("/x") == (200, {"ok": True})
    assert slept == [37.5] and api.result.throttled == 1
    assert gate.retry_after({"Retry-After": "9"}) == 9.0
    assert gate.retry_after({}) == 5.0 and gate.retry_after({"retry-after": "999"}) == 60.0
