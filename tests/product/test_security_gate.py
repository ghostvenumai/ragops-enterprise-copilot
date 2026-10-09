"""Security gate self-tests: only present, well-formed, current and true evidence counts."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from scripts import rc_live_gate
from scripts import security_gate as gate

ROOT = Path(__file__).resolve().parents[2]
COMMIT = "a" * 40
NOW = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)
CONTROLS = {control.id: control for control in gate.CONTROLS}
LIVE_FILES = [control for control in gate.CONTROLS if control.evidence_file]


def passing_outcomes() -> dict[str, list[str]]:
    return {test: ["passed"] for test in gate.expected_tests()}


def passing_keycloak() -> dict[str, object]:
    return {
        "realm_exists": True,
        "realm": {
            "bruteForceProtected": True,
            "permanentLockout": False,
            "registrationAllowed": False,
        },
        "clients": [{"clientId": "ragops-api", "directAccessGrantsEnabled": False}],
        "users": [],
    }


def write_live(directory: Path, control: gate.Control, **changes: object) -> None:
    payload = {
        "tested_commit": COMMIT,
        "timestamp": (NOW - timedelta(minutes=5)).isoformat(),
        **dict(control.required),
        **dict(control.minimums),
        **changes,
    }
    (directory / control.evidence_file).write_text(json.dumps(payload), encoding="utf-8")


def fresh() -> str:
    return datetime.now(UTC).isoformat()


def all_passing() -> list[gate.ControlResult]:
    return [gate.ControlResult(control.id, gate.PASS, "ok") for control in gate.CONTROLS]


# --------------------------------------------------------------------------- matrix


def test_every_control_has_exactly_one_evidence_source_and_a_valid_type() -> None:
    assert len(CONTROLS) == len(gate.CONTROLS)
    for control in gate.CONTROLS:
        sources = [
            bool(control.tests),
            bool(control.config_check),
            bool(control.evidence_file),
            control.keycloak,
        ]
        assert sum(sources) == 1, control.id
        assert control.kind in {"regression", "live"}
        assert (control.kind == "live") == bool(control.evidence_file or control.keycloak)
        assert control.config_check in {"", *gate.CONFIG_CHECKS}
        assert not control.evidence_file or dict(control.required).get("status") == "PASS"


def test_named_regression_tests_exist_in_the_repository() -> None:
    for test in gate.expected_tests():
        path, name = test.split("::")
        assert f"def {name}(" in (ROOT / path).read_text(encoding="utf-8"), test


def test_threat_model_documents_every_control_with_its_exact_evidence_source() -> None:
    document = (ROOT / "docs/THREAT_MODEL.md").read_text(encoding="utf-8")
    for row in gate.matrix_markdown().splitlines():
        assert row in document, row.split("|")[1].strip()


# --------------------------------------------------------------------------- regression


def test_regression_control_passes_only_when_every_named_test_passed() -> None:
    control = CONTROLS["SEC-02"]
    assert gate.evaluate_regression(control, passing_outcomes()).status == gate.PASS


@pytest.mark.parametrize(
    ("outcome", "expected"),
    [(None, gate.MISSING), (["skipped"], gate.MISSING), (["passed", "failed"], gate.FAIL)],
)
def test_missing_skipped_or_failed_tests_are_not_evidence(outcome, expected) -> None:
    control = CONTROLS["SEC-02"]
    outcomes = passing_outcomes()
    if outcome is None:
        del outcomes[control.tests[-1]]
    else:
        outcomes[control.tests[-1]] = outcome
    assert gate.evaluate_regression(control, outcomes).status == expected


def test_missing_or_malformed_regression_report_is_malformed(tmp_path) -> None:
    assert gate.parse_junit(tmp_path / "absent.xml") is None
    broken = tmp_path / "broken.xml"
    broken.write_text("<testsuites><testcase", encoding="utf-8")
    assert gate.parse_junit(broken) is None
    nameless = tmp_path / "nameless.xml"
    nameless.write_text("<testsuites><testcase classname='a'/></testsuites>", encoding="utf-8")
    assert gate.parse_junit(nameless) is None
    assert gate.evaluate_regression(CONTROLS["SEC-01"], None).status == gate.MALFORMED


def test_regression_tests_do_not_inherit_the_integration_environment(tmp_path, monkeypatch) -> None:
    host = {
        "PATH": "/usr/bin",
        "HOME": "/home/operator",
        "RAGOPS_ENV": "production",
        "RAGOPS_TEST_DATABASE_URL": "synthetic",
        "KEYCLOAK_ADMIN_PASSWORD": "synthetic",
        "OPENAI_API_KEY": "synthetic",
        "AZURE_OPENAI_API_KEY": "synthetic",
    }
    assert gate.regression_environment(host) == {"PATH": "/usr/bin", "HOME": "/home/operator"}
    seen: dict[str, object] = {}
    monkeypatch.setenv("RAGOPS_ENV", "production")
    monkeypatch.setattr(gate.subprocess, "run", lambda command, **kwargs: seen.update(kwargs))
    gate.run_regression(tmp_path)
    assert "RAGOPS_ENV" not in seen["env"] and "PATH" in seen["env"]  # type: ignore[operator]


def test_junit_outcomes_are_grouped_per_test_function(tmp_path) -> None:
    report = tmp_path / "report.xml"
    report.write_text(
        "<testsuites><testsuite>"
        "<testcase classname='tests.unit.test_x' name='test_a[one]'/>"
        "<testcase classname='tests.unit.test_x' name='test_a[two]'><failure/></testcase>"
        "<testcase classname='tests.unit.test_x' name='test_b'><skipped/></testcase>"
        "<testcase classname='tests.unit.test_x' name='test_c'><error/></testcase>"
        "</testsuite></testsuites>",
        encoding="utf-8",
    )
    assert gate.parse_junit(report) == {
        "tests/unit/test_x.py::test_a": ["passed", "failed"],
        "tests/unit/test_x.py::test_b": ["skipped"],
        "tests/unit/test_x.py::test_c": ["failed"],
    }


# --------------------------------------------------------------------------- config checks


def test_repository_configuration_checks_pass() -> None:
    for name in gate.CONFIG_CHECKS:
        control = next(item for item in gate.CONTROLS if item.config_check == name)
        result = gate.evaluate_config(control)
        assert result.status == gate.PASS, (name, result.detail)


def test_false_or_missing_configuration_is_not_evidence(tmp_path) -> None:
    control = CONTROLS["CFG-01"]
    assert gate.evaluate_config(control, tmp_path).status == gate.MISSING
    (tmp_path / "deploy").mkdir()
    weakened = (ROOT / "deploy/Caddyfile").read_text(encoding="utf-8").replace("-Server", "")
    (tmp_path / "deploy/Caddyfile").write_text(weakened, encoding="utf-8")
    assert gate.evaluate_config(control, tmp_path).status == gate.FAIL


def test_weakened_compose_and_unpinned_dependencies_fail(tmp_path) -> None:
    compose = (ROOT / "docker-compose.prod.yml").read_text(encoding="utf-8")
    (tmp_path / "docker-compose.prod.yml").write_text(
        compose.replace("      - ALL\n", "      - NET_RAW\n"), encoding="utf-8"
    )
    assert gate.evaluate_config(CONTROLS["CFG-02"], tmp_path).status == gate.FAIL
    (tmp_path / "pyproject.toml").write_text(
        '[project]\ndependencies = ["fastapi>=0.1"]\n', encoding="utf-8"
    )
    (tmp_path / "constraints.txt").write_text("fastapi==0.1\n", encoding="utf-8")
    assert gate.evaluate_config(CONTROLS["CFG-03"], tmp_path).status == gate.FAIL
    (tmp_path / "pyproject.toml").write_text("not toml [", encoding="utf-8")
    assert gate.evaluate_config(CONTROLS["CFG-03"], tmp_path).status == gate.MALFORMED


# --------------------------------------------------------------------------- live evidence


def test_current_true_live_evidence_passes(tmp_path) -> None:
    for control in LIVE_FILES:
        write_live(tmp_path, control)
        assert gate.evaluate_live_evidence(control, tmp_path, COMMIT, NOW).status == gate.PASS


def test_missing_live_evidence_is_missing(tmp_path) -> None:
    control = CONTROLS["LIVE-03"]
    assert gate.evaluate_live_evidence(control, tmp_path, COMMIT, NOW).status == gate.MISSING
    write_live(tmp_path, control)
    payload = json.loads((tmp_path / control.evidence_file).read_text())
    del payload["tenant_leakage"]
    (tmp_path / control.evidence_file).write_text(json.dumps(payload))
    assert gate.evaluate_live_evidence(control, tmp_path, COMMIT, NOW).status == gate.MISSING
    write_live(tmp_path, control, status="BLOCKED")
    assert gate.evaluate_live_evidence(control, tmp_path, COMMIT, NOW).status == gate.MISSING


@pytest.mark.parametrize(
    "changes",
    [
        {"status": "FAIL"},
        {"tenant_leakage": 1},
        {"tenant_leakage": "0"},
        {"tenant_leakage": False},
        {"cleanup_status": "NOT_RUN"},
    ],
)
def test_false_live_evidence_fails(tmp_path, changes) -> None:
    control = CONTROLS["LIVE-03"]
    write_live(tmp_path, control, **changes)
    assert gate.evaluate_live_evidence(control, tmp_path, COMMIT, NOW).status == gate.FAIL


def test_boolean_evidence_must_be_a_real_boolean(tmp_path) -> None:
    control = CONTROLS["LIVE-04"]
    write_live(tmp_path, control, cross_tenant_catalog_influence=0)
    assert gate.evaluate_live_evidence(control, tmp_path, COMMIT, NOW).status == gate.FAIL


@pytest.mark.parametrize(
    "raw",
    [
        "{not json",
        "[]",
        '"PASS"',
        json.dumps({"status": "PASS", "tested_commit": COMMIT}),
        json.dumps({"status": "PASS", "timestamp": NOW.isoformat()}),
        json.dumps({"status": "PASS", "tested_commit": COMMIT, "timestamp": "yesterday"}),
        json.dumps({"status": "PASS", "tested_commit": COMMIT, "timestamp": 1}),
        json.dumps({"status": "PASS", "tested_commit": COMMIT, "timestamp": "2026-09-30T11:00"}),
        json.dumps({"status": "PASS", "tested_commit": 7, "timestamp": NOW.isoformat()}),
    ],
)
def test_malformed_live_evidence_is_malformed(tmp_path, raw) -> None:
    control = CONTROLS["LIVE-01"]
    (tmp_path / control.evidence_file).write_text(raw, encoding="utf-8")
    assert gate.evaluate_live_evidence(control, tmp_path, COMMIT, NOW).status == gate.MALFORMED


@pytest.mark.parametrize(
    "changes",
    [
        {"tested_commit": "b" * 40},
        {"timestamp": (NOW - gate.MAX_EVIDENCE_AGE - timedelta(seconds=1)).isoformat()},
        {"timestamp": (NOW + timedelta(hours=1)).isoformat()},
    ],
)
def test_stale_live_evidence_is_stale(tmp_path, changes) -> None:
    control = CONTROLS["LIVE-01"]
    write_live(tmp_path, control, **changes)
    assert gate.evaluate_live_evidence(control, tmp_path, COMMIT, NOW).status == gate.STALE


def test_unknown_commit_never_matches_evidence(tmp_path) -> None:
    control = CONTROLS["LIVE-01"]
    write_live(tmp_path, control, tested_commit="")
    assert gate.evaluate_live_evidence(control, tmp_path, "", NOW).status == gate.STALE


def test_keycloak_settings_are_evidence_only_when_read_and_true() -> None:
    control = CONTROLS["LIVE-02"]
    assert gate.evaluate_keycloak(control, passing_keycloak()).status == gate.PASS
    assert gate.evaluate_keycloak(control, None).status == gate.MISSING
    assert gate.evaluate_keycloak(control, {"realm_exists": False}).status == gate.MISSING
    assert gate.evaluate_keycloak(control, {"realm_exists": True}).status == gate.MALFORMED
    for client in ({"directAccessGrantsEnabled": True}, {}):
        state = passing_keycloak()
        state["clients"] = [{"clientId": "ragops-api", **client}]
        assert gate.evaluate_keycloak(control, state).status == gate.FAIL
    unprotected = passing_keycloak()
    unprotected["realm"] = {**unprotected["realm"], "bruteForceProtected": False}  # type: ignore[dict-item]
    assert gate.evaluate_keycloak(control, unprotected).status == gate.FAIL
    without_client = passing_keycloak()
    without_client["clients"] = []
    assert gate.evaluate_keycloak(control, without_client).status == gate.MISSING


# --------------------------------------------------------------------------- aggregation


def test_pass_requires_every_control_a_clean_cleanup_and_a_clean_worktree() -> None:
    assert gate.classify(all_passing(), True, True)[0] == "PASS"
    assert gate.classify(all_passing(), True, False)[0] == "BLOCKED"
    assert gate.classify(all_passing(), False, True)[0] == "FAIL"
    assert gate.classify(all_passing()[:-1], True, True)[0] == "BLOCKED"
    assert gate.classify([], True, True)[0] == "BLOCKED"


@pytest.mark.parametrize("status", [gate.MISSING, gate.MALFORMED, gate.STALE])
def test_inadmissible_evidence_keeps_the_gate_blocked(status) -> None:
    results = all_passing()
    results[3] = gate.ControlResult(results[3].id, status, "x")
    verdict, reason = gate.classify(results, True, True)
    assert verdict == "BLOCKED" and results[3].id in reason


def test_false_evidence_fails_even_when_other_evidence_is_missing() -> None:
    results = all_passing()
    results[0] = gate.ControlResult(results[0].id, gate.MISSING, "x")
    results[5] = gate.ControlResult(results[5].id, gate.FAIL, "x")
    assert gate.classify(results, True, True)[0] == "FAIL"


def test_evidence_with_a_secret_is_replaced_by_a_failure(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(gate, "EVIDENCE", tmp_path / "security.json")
    evidence = {"gate": "security", "tested_commit": COMMIT, "detail": "login SyntheticPw123"}
    assert gate._finish(evidence, "PASS", "ok", ("SyntheticPw123",)) == 1
    raw = gate.EVIDENCE.read_text()
    written = json.loads(raw)
    assert written["status"] == "FAIL" and written["secret_scan_passed"] is False
    assert "SyntheticPw123" not in raw and "controls" not in written


@pytest.mark.parametrize(
    "text",
    [
        "eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJzeW50aGV0aWMifQ.c2lnbmF0dXJl",
        "-----BEGIN RSA " + "PRIVATE KEY-----",
        "postgresql+psycopg://rc:SyntheticPw123@127.0.0.1:5432/ragops_test",
        "sk-" + "a" * 24,
    ],
    # Plain ids keep the secret-shaped values out of test reports.
    ids=["jwt", "private-key", "database-url", "provider-key"],
)
def test_secret_shaped_evidence_is_detected(text) -> None:
    assert gate.leaks(json.dumps({"detail": text}), ())
    assert not gate.leaks(json.dumps({"detail": "tests/security/test_identity.py"}), ())


# --------------------------------------------------------------------------- end to end


@pytest.fixture
def harness(monkeypatch, tmp_path):
    live = tmp_path / "rc-live"
    live.mkdir()
    state = {"outcomes": passing_outcomes(), "keycloak": passing_keycloak(), "clean": True}
    monkeypatch.setattr(gate, "RC_LIVE", live)
    monkeypatch.setattr(gate, "EVIDENCE", live / "security.json")
    monkeypatch.setattr(gate, "WORK_PARENT", tmp_path / "work")
    monkeypatch.setattr(gate, "tested_commit", lambda: COMMIT)
    monkeypatch.setattr(gate, "worktree_is_clean", lambda: state["clean"])
    monkeypatch.setattr(gate, "run_regression", lambda work: work / "regression.xml")
    monkeypatch.setattr(gate, "parse_junit", lambda report: state["outcomes"])
    monkeypatch.setattr(gate, "read_keycloak_state", lambda: (state["keycloak"], ()))
    for control in LIVE_FILES:
        write_live(live, control, timestamp=fresh())
    return state, live


def result_of(live: Path) -> dict[str, object]:
    return json.loads((live / "security.json").read_text(encoding="utf-8"))


def test_gate_passes_with_complete_admissible_evidence(harness) -> None:
    _, live = harness
    assert gate.main([]) == 0
    evidence = result_of(live)
    assert evidence["status"] == "PASS"
    assert evidence["evidenced_control_count"] == evidence["mandatory_control_count"]
    assert evidence["mandatory_control_count"] == len(gate.CONTROLS)
    assert evidence["regression_tests_passed"] == len(gate.expected_tests())
    assert evidence["cleanup_status"] == "PASS" and evidence["secret_scan_passed"] is True
    assert {item["type"] for item in evidence["controls"]} == {"regression", "live"}  # type: ignore[union-attr]
    assert not list((live.parent / "work").iterdir())


def test_gate_is_blocked_without_live_evidence_or_keycloak_access(harness) -> None:
    state, live = harness
    (live / "oidc.json").unlink()
    state["keycloak"] = None
    assert gate.main([]) == 2
    evidence = result_of(live)
    statuses = {item["id"]: item["status"] for item in evidence["controls"]}  # type: ignore[union-attr]
    assert evidence["status"] == "BLOCKED"
    assert (statuses["LIVE-01"], statuses["LIVE-02"]) == (gate.MISSING, gate.MISSING)
    assert evidence["evidenced_control_count"] == len(gate.CONTROLS) - 2


def test_gate_is_blocked_by_stale_evidence_and_uncommitted_changes(harness) -> None:
    state, live = harness
    state["clean"] = False
    assert gate.main([]) == 2
    assert "uncommitted" in str(result_of(live)["reason"])
    state["clean"] = True
    write_live(live, CONTROLS["LIVE-05"], tested_commit="b" * 40, timestamp=fresh())
    assert gate.main([]) == 2
    assert "LIVE-05" in str(result_of(live)["reason"])


def test_gate_fails_on_false_evidence_and_on_a_failed_regression_test(harness) -> None:
    state, live = harness
    write_live(live, CONTROLS["LIVE-06"], wrong_key_rejected=False, timestamp=fresh())
    assert gate.main([]) == 1
    assert "LIVE-06" in str(result_of(live)["reason"])
    write_live(live, CONTROLS["LIVE-06"], timestamp=fresh())
    state["outcomes"][CONTROLS["SEC-04"].tests[0]] = ["failed"]
    assert gate.main([]) == 1
    assert "SEC-04" in str(result_of(live)["reason"])


def test_gate_fails_when_the_gate_only_client_was_not_cleaned_up(harness) -> None:
    state, live = harness
    state["keycloak"]["clients"].append({"clientId": gate.GATE_ONLY_CLIENT})
    assert gate.main([]) == 1
    evidence = result_of(live)
    assert evidence["cleanup_status"] == "FAIL"
    assert evidence["gate_only_objects_remaining"] == [f"Keycloak client {gate.GATE_ONLY_CLIENT}"]


def test_gate_fails_when_its_work_directory_cannot_be_removed(harness, monkeypatch) -> None:
    _, live = harness
    monkeypatch.setattr(gate.shutil, "rmtree", lambda *args, **kwargs: None)
    assert gate.main([]) == 1
    assert result_of(live)["gate_only_objects_remaining"] == ["regression work directory"]


def test_gate_fails_when_a_credential_reaches_the_evidence(harness, monkeypatch) -> None:
    state, live = harness
    monkeypatch.setattr(
        gate, "read_keycloak_state", lambda: (state["keycloak"], ("SyntheticAdminPw1",))
    )
    state["outcomes"]["tests/security/test_identity.py::SyntheticAdminPw1"] = ["passed"]
    monkeypatch.setattr(
        gate,
        "evaluate_keycloak",
        lambda control, _: gate.ControlResult(control.id, gate.PASS, "SyntheticAdminPw1"),
    )
    assert gate.main([]) == 1
    raw = (live / "security.json").read_text(encoding="utf-8")
    assert "SyntheticAdminPw1" not in raw and json.loads(raw)["reason"] == (
        "evidence redaction failed"
    )


# --------------------------------------------------------------------------- RC runner


def test_rc_runner_runs_the_security_gate_last_and_keeps_its_evidence() -> None:
    assert "security" in rc_live_gate.DETAILED_EVIDENCE_GATES
    assert rc_live_gate.ORCHESTRATOR_GATES == {"security"}
    prior = {name: "PASS" for name in rc_live_gate.GATES if name != "security"}
    merged = rc_live_gate.merge_gate_statuses(prior, {"security"}, {"security": {"status": "FAIL"}})
    assert all(merged[name] == "PASS" for name in prior) and merged["security"] == "FAIL"
    assert rc_live_gate.merge_gate_statuses(prior, {"security"}, {})["security"] == "BLOCKED"
    placeholder = rc_live_gate.blocked_placeholder("security", COMMIT)
    assert (placeholder["status"], placeholder["tested_commit"]) == ("BLOCKED", COMMIT)


def test_oidc_gate_always_removes_its_gate_only_client() -> None:
    calls: list[list[str]] = []

    def run(results: dict[str, tuple[str, str]]):
        def execute(command: list[str]) -> tuple[str, str]:
            calls.append(command)
            key = "cleanup" if "--cleanup" in command else Path(command[1]).stem
            return results.get(key, ("PASS", "ok"))

        return execute

    assert rc_live_gate.run_oidc_gate(run({}), True) == ("PASS", "ok")
    assert [len(command) for command in calls] == [2, 2, 3] and calls[2][-1] == "--cleanup"
    calls.clear()
    failed_check = {"oidc_integration_check": ("FAIL", "wrong audience was accepted")}
    assert rc_live_gate.run_oidc_gate(run(failed_check), True)[0] == "FAIL"
    assert calls[-1][-1] == "--cleanup"
    calls.clear()
    failed_bootstrap = {"configure_local_oidc_integration": ("FAIL", "verification failed")}
    assert rc_live_gate.run_oidc_gate(run(failed_bootstrap), True)[0] == "FAIL"
    assert len(calls) == 2 and calls[-1][-1] == "--cleanup"
    status, detail = rc_live_gate.run_oidc_gate(run({"cleanup": ("FAIL", "left behind")}), True)
    assert status == "FAIL" and "cleanup failed" in detail
    calls.clear()
    assert rc_live_gate.run_oidc_gate(run({}), False) == ("PASS", "ok")
    assert calls == [[".venv/bin/python", "scripts/oidc_integration_check.py"]]


# --------------------------------------------------------------------------- rag_query (LIVE-10)


def test_rag_query_evidence_needs_observed_qdrant_requests(tmp_path) -> None:
    control = CONTROLS["LIVE-10"]
    write_live(tmp_path, control, qdrant_requests_observed=3)
    assert gate.evaluate_live_evidence(control, tmp_path, COMMIT, NOW).status == gate.PASS
    for value in (0, -1, "3", True, None):
        write_live(tmp_path, control, qdrant_requests_observed=value)
        assert gate.evaluate_live_evidence(control, tmp_path, COMMIT, NOW).status == gate.FAIL
    payload = json.loads((tmp_path / control.evidence_file).read_text())
    del payload["qdrant_requests_observed"]
    (tmp_path / control.evidence_file).write_text(json.dumps(payload))
    assert gate.evaluate_live_evidence(control, tmp_path, COMMIT, NOW).status == gate.MISSING


@pytest.mark.parametrize(
    "changes",
    [
        {"tenant_leakage": 1},
        {"citation_tenant_leakage": 1},
        {"usage_tenant_leakage": 1},
        {"budget_tenant_leakage": 1},
        {"demo_retrieval_used": True},
        {"unique_nonce_verified": False},
        {"paid_provider_calls": 1},
        {"secret_scan_passed": False},
    ],
)
def test_false_rag_query_evidence_fails(tmp_path, changes) -> None:
    control = CONTROLS["LIVE-10"]
    write_live(tmp_path, control, qdrant_requests_observed=2, **changes)
    assert gate.evaluate_live_evidence(control, tmp_path, COMMIT, NOW).status == gate.FAIL


def test_stale_or_foreign_rag_query_evidence_is_not_admissible(tmp_path) -> None:
    control = CONTROLS["LIVE-10"]
    write_live(tmp_path, control, qdrant_requests_observed=2, tested_commit="b" * 40)
    assert gate.evaluate_live_evidence(control, tmp_path, COMMIT, NOW).status == gate.STALE
    old = (NOW - gate.MAX_EVIDENCE_AGE - timedelta(minutes=1)).isoformat()
    write_live(tmp_path, control, qdrant_requests_observed=2, timestamp=old)
    assert gate.evaluate_live_evidence(control, tmp_path, COMMIT, NOW).status == gate.STALE
