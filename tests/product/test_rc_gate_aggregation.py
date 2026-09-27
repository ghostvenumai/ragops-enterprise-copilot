from scripts.postgres_concurrency_gate import migration_revision, validate_isolated_database
from scripts.rc_live_gate import (
    aggregate_statuses,
    merge_detailed_gate_evidence,
    merge_gate_statuses,
)


def test_fail_precedes_blocked() -> None:
    assert aggregate_statuses({"postgresql": "FAIL", "redis": "BLOCKED"})[0] == "FAIL"


def test_blocked_precedes_pass() -> None:
    assert aggregate_statuses({"postgresql": "PASS", "redis": "BLOCKED"})[0] == "BLOCKED"


def test_all_pass() -> None:
    assert aggregate_statuses({"postgresql": "PASS"})[0] == "PASS"


def test_selective_gate_run_preserves_unselected_state() -> None:
    merged = merge_gate_statuses(
        {"postgresql": "PASS", "redis_worker": "FAIL"},
        {"postgresql_concurrency"},
        {"postgresql_concurrency": {"status": "BLOCKED"}},
    )
    assert merged["postgresql"] == "PASS"
    assert merged["redis_worker"] == "FAIL"
    assert merged["postgresql_concurrency"] == "BLOCKED"


def test_redis_gate_runner_preserves_detailed_evidence() -> None:
    evidence = merge_detailed_gate_evidence(
        {
            "status": "FAIL",
            "reason": "setpriv failure",
            "redis_bootstrap_status": "FAIL",
            "sanitized_startup_log_summary": "setpriv: setresuid failed",
        },
        {"status": "FAIL", "detail": "fallback"},
        "abc123",
        "2026-09-16T00:00:00Z",
    )
    assert evidence["redis_bootstrap_status"] == "FAIL"
    assert evidence["sanitized_startup_log_summary"] == "setpriv: setresuid failed"


def test_qdrant_gate_runner_preserves_detailed_evidence() -> None:
    evidence = merge_detailed_gate_evidence(
        {
            "gate": "qdrant",
            "status": "BLOCKED",
            "integration_status": "NOT_EXECUTED",
            "cleanup_status": "NOT_RUN",
            "qdrant_client_available": False,
        },
        {"status": "BLOCKED", "detail": "fallback"},
        "abc123",
        "2026-09-16T00:00:00Z",
        "qdrant",
    )
    assert evidence["gate"] == "qdrant"
    assert evidence["integration_status"] == "NOT_EXECUTED"
    assert evidence["qdrant_client_available"] is False


def test_external_provider_gate_runner_preserves_detailed_evidence() -> None:
    evidence = merge_detailed_gate_evidence(
        {
            "gate": "external_llm_provider",
            "status": "BLOCKED",
            "paid_integration_opt_in": False,
            "live_request_status": "NOT_EXECUTED",
            "request_count": 0,
        },
        {"status": "BLOCKED", "detail": "fallback"},
        "abc123",
        "2026-09-16T00:00:00Z",
        "external_llm_provider",
    )
    assert evidence["gate"] == "external_llm_provider"
    assert evidence["paid_integration_opt_in"] is False
    assert evidence["request_count"] == 0


def test_concurrency_database_requires_isolated_postgres_name() -> None:
    valid, _, name = validate_isolated_database(
        "postgresql+psycopg://u:p@localhost/ragops_test_concurrency"
    )
    assert valid is True
    assert name == "ragops_test_concurrency"
    valid, reason, _ = validate_isolated_database("postgresql+psycopg://u:p@localhost/production")
    assert valid is False
    assert "ragops_test_" in reason


def test_migration_revision_requires_expected_head() -> None:
    assert migration_revision("0006_finops_reservations (head)") == "0006_finops_reservations"
    assert migration_revision("0005_ai_finops") is None
