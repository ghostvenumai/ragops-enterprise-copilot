"""Readiness/recovery gate: classification, blocking, evidence secrets, RC state, fault proxy."""

from __future__ import annotations

import json
import socket
import threading
import time

import pytest
from scripts import rc_live_gate
from scripts import readiness_failure_recovery_gate as gate
from scripts.fault_proxy import FaultProxy


def passing() -> gate.DrillResult:
    result = gate.DrillResult(checks=dict.fromkeys(gate.REQUIRED, True))
    result.metrics.update(
        lost_jobs=0,
        duplicate_completed_jobs=0,
        partial_active_versions=0,
        duplicate_vector_points=0,
        stale_aliases=0,
        postgres_partial_writes=0,
    )
    return result


def test_all_required_checks_pass() -> None:
    assert gate.classify(passing())[0] == "PASS"
    evidence = gate.build_evidence(passing())
    truths = {
        "readiness_payload_sanitized": True,
        "rate_limit_fail_closed": True,
        "retry_budget_verified": True,
        "backoff_verified": True,
        "busy_loop_detected": False,
        "combined_outage_verified": True,
        "startup_unready_verified": True,
        "recovery_without_restart_verified": True,
        "shutdown_unready_first": True,
        "restart_verified": True,
        "unrelated_data_unchanged": True,
        "readiness_recovery_tenant_leakage": 0,
        "paid_provider_calls": 0,
        "cleanup_status": "PASS",
        "lost_jobs": 0,
        "duplicate_completed_jobs": 0,
    }
    assert {key: evidence[key] for key in truths} == truths


@pytest.mark.parametrize(
    "check", ["no_lost_jobs", "rate_limit_fail_closed", "shutdown_unready_first", "no_busy_loop"]
)
def test_any_failed_or_missing_check_fails(check) -> None:
    failed = passing()
    failed.checks[check] = False
    assert gate.classify(failed)[0] == "FAIL"
    missing = passing()
    del missing.checks[check]
    status, reason = gate.classify(missing)
    assert status == "FAIL" and check in reason


def test_tenant_leakage_and_paid_provider_fail() -> None:
    leaking = passing()
    leaking.tenant_leakage = 1
    assert gate.classify(leaking) == ("FAIL", "tenant data crossed boundaries after recovery")
    paid = passing()
    paid.paid_provider_calls = 1
    assert gate.classify(paid)[0] == "FAIL"


def test_gate_database_names_are_restricted() -> None:
    assert gate.GATE_DATABASE.match("ragops_test_rfr_0123456789abcdef"[:26])
    for name in ("ragops", "postgres", "ragops_test_integration", "ragops_test_rfr_x; DROP"):
        assert not gate.GATE_DATABASE.match(name)


def test_missing_credentials_are_blocked(monkeypatch, tmp_path) -> None:
    import scripts.backup_restore_gate as backup

    monkeypatch.setattr(gate, "EVIDENCE", tmp_path / "rfr.json")
    monkeypatch.setattr(backup, "resolve_test_database_url", lambda: None)
    assert gate.main() == 2
    evidence = json.loads(gate.EVIDENCE.read_text())
    assert evidence["status"] == "BLOCKED" and evidence["gate"] == "readiness_failure_recovery"


def test_evidence_with_a_connection_url_is_replaced_by_a_failure(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(gate, "EVIDENCE", tmp_path / "rfr.json")
    evidence = {"gate": "readiness_failure_recovery", "detail": "redis://:Pw@127.0.0.1:1/0"}
    assert gate._finish(evidence, "PASS", "ok", 0) == 1
    raw = gate.EVIDENCE.read_text()
    assert json.loads(raw)["status"] == "FAIL" and "Pw@" not in raw and "redis://" not in raw


def test_rc_runner_merges_readiness_evidence_and_keeps_prior_pass() -> None:
    assert "readiness_failure_recovery" in rc_live_gate.DETAILED_EVIDENCE_GATES
    prior = {name: "PASS" for name in rc_live_gate.GATES[:12]}
    prior.update(readiness_failure_recovery="BLOCKED", browser_e2e="BLOCKED", security="BLOCKED")
    merged = rc_live_gate.merge_gate_statuses(
        prior,
        {"readiness_failure_recovery"},
        {"readiness_failure_recovery": {"status": "PASS"}},
    )
    assert [gate for gate, status in merged.items() if status == "PASS"] == list(
        rc_live_gate.GATES[:13]
    )
    assert (merged["browser_e2e"], merged["security"]) == ("BLOCKED", "BLOCKED")
    assert rc_live_gate.aggregate_statuses(merged)[0] == "BLOCKED"


@pytest.fixture
def echo_server():
    server = socket.create_server(("127.0.0.1", 0))
    stop = threading.Event()

    def serve() -> None:
        server.settimeout(0.1)
        while not stop.is_set():
            try:
                connection, _ = server.accept()
            except OSError:
                continue
            threading.Thread(target=echo, args=(connection,), daemon=True).start()

    def echo(connection: socket.socket) -> None:
        with connection:
            while data := connection.recv(1024):
                connection.sendall(data)

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    yield server.getsockname()[1]
    stop.set()
    thread.join(timeout=2)
    server.close()


def _round_trip(port: int, timeout: float = 0.5) -> bytes:
    with socket.create_connection(("127.0.0.1", port), timeout=timeout) as connection:
        connection.sendall(b"ping")
        return connection.recv(16)


def test_fault_proxy_modes_affect_only_its_clients(echo_server) -> None:
    proxy = FaultProxy("127.0.0.1", echo_server)
    try:
        assert _round_trip(proxy.port) == b"ping"
        port = proxy.port
        proxy.set_mode("refuse")
        with pytest.raises(ConnectionRefusedError):
            _round_trip(port)
        assert _round_trip(echo_server) == b"ping"  # the upstream itself is untouched
        proxy.set_mode("blackhole")
        started = time.monotonic()
        with pytest.raises(TimeoutError):
            _round_trip(port, timeout=0.3)
        assert time.monotonic() - started < 1.0
        proxy.set_mode("forward")
        assert proxy.port == port and _round_trip(port) == b"ping"
        with socket.create_connection(("127.0.0.1", port), timeout=0.5) as connection:
            connection.sendall(b"a")
            assert connection.recv(4) == b"a"
            proxy.cut()
            assert connection.recv(4) == b""
        with pytest.raises(ValueError):
            proxy.set_mode("pause")
    finally:
        proxy.stop()
