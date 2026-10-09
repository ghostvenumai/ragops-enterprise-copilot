"""Browser e2e gate: classification, blocking, secret scans, provider watchdog, RC state."""

from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

import pytest
from scripts import browser_e2e_gate as gate
from scripts import browser_e2e_services as services
from scripts import rc_live_gate


def passing() -> gate.GateResult:
    return gate.GateResult(checks=dict.fromkeys(gate.REQUIRED, True))


def test_all_required_checks_pass_and_report_the_contract() -> None:
    assert gate.classify(passing())[0] == "PASS"
    evidence = gate.build_evidence(passing(), {"browser_version": "153"})
    assert evidence["browser_query_uses_qdrant"] is False
    # The journeys use the demo corpus on purpose; the mode is explicit, never a fallback.
    assert evidence["query_mode"] == "demo"
    import inspect

    assert inspect.signature(gate.environment).parameters["query_mode"].default == "demo"
    assert "query_mode=" not in inspect.getsource(gate.main)
    source = Path(gate.__file__).read_text(encoding="utf-8")
    assert source.count('"RAGOPS_QUERY_MODE": query_mode') == 1
    assert evidence["headless"] is True and evidence["browser_engine"] == "chromium"
    for key in (
        "real_browser_login_verified",
        "backend_rbac_denied",
        "responsive_desktop",
        "responsive_mobile",
        "keyboard_flow_verified",
    ):
        assert evidence[key] is True
    assert evidence["browser_e2e_tenant_leakage"] == 0 and evidence["paid_provider_calls"] == 0
    assert evidence["sensitive_artifacts_detected"] is False
    assert evidence["cleanup_status"] == "PASS"


@pytest.mark.parametrize(
    "check",
    ["real_browser_login_verified", "backend_rbac_denied", "no_severe_console_errors"],
)
def test_a_failed_or_missing_check_fails(check) -> None:
    failed = passing()
    failed.checks[check] = False
    assert gate.classify(failed)[0] == "FAIL"
    missing = passing()
    del missing.checks[check]
    assert check in gate.classify(missing)[1]


def test_leakage_and_paid_calls_fail() -> None:
    leaking = passing()
    leaking.tenant_leakage = 1
    assert gate.classify(leaking)[0] == "FAIL"
    paid = passing()
    paid.paid_provider_calls = 1
    assert gate.classify(paid)[0] == "FAIL"


def test_check_never_turns_a_failure_back_into_success() -> None:
    result = gate.GateResult()
    result.check("x", False)
    result.check("x", True)
    assert result.checks["x"] is False


def test_gate_database_names_are_restricted() -> None:
    assert gate.GATE_DATABASE.match("ragops_test_e2e_0123456789")
    for name in ("ragops", "ragops_test_integration", "ragops_test_e2e_x; DROP"):
        assert not gate.GATE_DATABASE.match(name)


def test_missing_keycloak_credentials_are_blocked(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(gate, "EVIDENCE", tmp_path / "browser-e2e.json")
    monkeypatch.setattr(gate, "ARTIFACTS", tmp_path / "artifacts")
    monkeypatch.setattr(gate, "keycloak_admin_credentials", lambda: None)
    assert gate.main() == 2
    evidence = json.loads(gate.EVIDENCE.read_text())
    assert evidence["status"] == "BLOCKED" and evidence["gate"] == "browser_e2e"


def test_evidence_with_a_token_is_replaced_by_a_failure(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(gate, "EVIDENCE", tmp_path / "browser-e2e.json")
    token = "eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJ4In0aaaaaaaaaaaa.c2ln"  # noqa: S105 - synthetic
    assert gate._finish({"gate": "browser_e2e", "detail": token}, "PASS", "ok", 0) == 1
    raw = gate.EVIDENCE.read_text()
    assert json.loads(raw)["status"] == "FAIL" and token not in raw


def test_artifact_scan_finds_credentials_in_files_and_traces(tmp_path) -> None:
    (tmp_path / "clean.png").write_bytes(b"\x89PNG harmless")
    (tmp_path / "leak.json").write_text('{"note": "Pw-Secret-123"}')
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("trace.network", "Authorization: Bearer abcdefghijklmnopqrstuvwxyz0123")
    (tmp_path / "trace-x.zip").write_bytes(buffer.getvalue())
    assert gate.scan_artifacts(tmp_path, ["Pw-Secret-123"]) == ["leak.json", "trace-x.zip"]


def test_sanitize_strips_queries_and_tokens() -> None:
    text = gate._sanitize("GET http://h/cb?code=abc&state=def Bearer abcdefghijklmnopqrstuvwxyz")
    assert "code=abc" not in text and "abcdefghijklmnopqrstuvwxyz" not in text


def test_synthetic_pdf_is_a_valid_pdf() -> None:
    from pypdf import PdfReader

    content = gate.synthetic_pdf("RC E2E Beispiel")
    assert content.startswith(b"%PDF-")
    assert "RC E2E Beispiel" in PdfReader(io.BytesIO(content)).pages[0].extract_text()


def test_provider_watchdog_blocks_paid_providers_and_counts_invocations(tmp_path) -> None:
    from ragops.llm import providers

    ledger = tmp_path / "ledger.txt"
    originals = (
        providers.OpenAIProvider.__init__,
        providers.AzureOpenAIProvider.__init__,
        providers.DeterministicTestProvider.generate,
    )
    try:
        services.install_provider_watchdog(ledger)
        with pytest.raises(RuntimeError):
            providers.OpenAIProvider()  # type: ignore[call-arg]
        providers.DeterministicTestProvider().generate("frage", [], "")
    finally:
        (
            providers.OpenAIProvider.__init__,
            providers.AzureOpenAIProvider.__init__,
            providers.DeterministicTestProvider.generate,
        ) = originals  # type: ignore[method-assign]
    assert ledger.read_text().split() == ["paid", "invoke"]


def test_rc_runner_merges_browser_evidence_and_keeps_prior_pass() -> None:
    assert "browser_e2e" in rc_live_gate.DETAILED_EVIDENCE_GATES
    prior = dict.fromkeys(gate.PRIOR_PASS_GATES, "PASS")
    prior.update(browser_e2e="BLOCKED", security="BLOCKED")
    merged = rc_live_gate.merge_gate_statuses(
        prior, {"browser_e2e"}, {"browser_e2e": {"status": "PASS"}}
    )
    assert sum(status == "PASS" for status in merged.values()) == 14
    assert merged["security"] == "BLOCKED"
    assert rc_live_gate.aggregate_statuses(merged)[0] == "BLOCKED"
