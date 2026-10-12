"""reservation_reconciliation RC gate: held query reservations are recovered or kept held.

The gate serves the real FastAPI application over loopback HTTP (uvicorn in this process) in
vector query mode, backed by a gate-owned PostgreSQL database migrated to head, with the
local deterministic provider (counted per tenant; one tenant times out, one reports no
usage). Failures are injected only through gate-owned PostgreSQL triggers on that database.
Reconciliation runs through ``ReconciliationService`` and the operator CLI; while it runs the
provider refuses and counts every call.

Scenarios, one isolated tenant each: an undisturbed query (n), an accounting failure after
provider success recovered from its durable receipt (f), a lost provider-boundary marker
released and recovered by the same key (p), a provider timeout (t) and unreported usage (v)
that stay held, usage that already exists (u), ten concurrent reconcilers of one case (c),
a reconciliation datastore failure (d), cross-tenant probing (x) and an ENT-11.5 hold
without ledger evidence (l). Evidence holds counts, states and classifications only.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import uuid
from collections import Counter
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from secrets import token_urlsafe
from types import SimpleNamespace
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import browser_e2e_gate as e2e  # noqa: E402
from scripts import query_idempotency_gate as qi  # noqa: E402

EVIDENCE = ROOT / "evidence" / "product-v1" / "rc-live" / "reservation-reconciliation.json"
PrerequisiteMissing = e2e.PrerequisiteMissing
RECONCILERS = 10
SCENARIOS = ("n", "f", "p", "t", "u", "c", "v", "d", "x", "l")
RECONCILIATION = {"detail": "idempotent request requires reconciliation"}

STAGES = (
    "normal_success",
    "accounting_failure",
    "pre_provider_failure",
    "ambiguous_provider",
    "unknown_usage",
    "existing_usage",
    "concurrency",
    "datastore_failure",
    "tenant_isolation",
    "legacy_hold",
    "operator_cli",
)
CLEANUP_CHECKS = ("server_stopped", "gate_database_removed", "temporary_files_removed")
UNRELATED_CHECKS = ("unrelated_databases_unchanged", "unrelated_files_unchanged")
REQUIRED = frozenset(
    {
        "only_local_provider_registered",
        "normal_success_unchanged",
        "accounting_failure_recovered",
        "accounting_failure_key_settled",
        "pre_provider_released",
        "same_key_recovery",
        "ambiguous_case_held",
        "unknown_usage_held",
        "existing_usage_committed",
        "concurrency_verified",
        "reconciliation_db_failure_fail_closed",
        "cross_tenant_not_found",
        "legacy_hold_kept",
        "operator_cli_verified",
        "ledger_has_no_content",
        *CLEANUP_CHECKS,
        *UNRELATED_CHECKS,
    }
)
ZERO_COUNTERS = (
    "provider_calls_during_reconciliation",
    "pre_provider_provider_calls",
    "duplicate_usage_records",
    "cross_tenant_leakage",
    "paid_provider_calls",
)
FALSE_FLAGS = (
    "ambiguous_released",
    "unknown_usage_booked_as_zero",
    "legacy_unknown_auto_released",
)
FAILURE_CLASSES = (
    "DB_FAILURE",
    "TENANT_SCOPE_FAILURE",
    "EVIDENCE_MISMATCH",
    "UNKNOWN_BILLING",
    "DUPLICATE_USAGE",
    "DUPLICATE_SETTLEMENT",
    "PROVIDER_REPLAY_ATTEMPT",
    "INVALID_STATE",
    "CLEANUP_FAILURE",
    "RESOURCE_BLOCKED",
    "UNDETERMINED",
)
# Which failure class a failing check or counter points at (first match wins).
_CLASS_OF = {
    "provider_calls_during_reconciliation": "PROVIDER_REPLAY_ATTEMPT",
    "pre_provider_provider_calls": "PROVIDER_REPLAY_ATTEMPT",
    "duplicate_usage_records": "DUPLICATE_USAGE",
    "concurrent_terminal_transitions": "DUPLICATE_SETTLEMENT",
    "concurrent_usage_records": "DUPLICATE_USAGE",
    "cross_tenant_leakage": "TENANT_SCOPE_FAILURE",
    "cross_tenant_not_found": "TENANT_SCOPE_FAILURE",
    "ambiguous_released": "UNKNOWN_BILLING",
    "ambiguous_case_held": "UNKNOWN_BILLING",
    "unknown_usage_booked_as_zero": "UNKNOWN_BILLING",
    "unknown_usage_held": "UNKNOWN_BILLING",
    "legacy_unknown_auto_released": "UNKNOWN_BILLING",
    "legacy_hold_kept": "UNKNOWN_BILLING",
    "reconciliation_db_failure_fail_closed": "DB_FAILURE",
    "accounting_failure_recovered": "EVIDENCE_MISMATCH",
    "existing_usage_committed": "EVIDENCE_MISMATCH",
    "accounting_failure_key_settled": "INVALID_STATE",
    "pre_provider_released": "INVALID_STATE",
    "same_key_recovery": "INVALID_STATE",
    **{name: "CLEANUP_FAILURE" for name in (*CLEANUP_CHECKS, *UNRELATED_CHECKS)},
}


@dataclass
class ReconciliationResult(e2e.GateResult):
    provider_calls_during_reconciliation: int = 0
    pre_provider_provider_calls: int = 0
    duplicate_usage_records: int = 0
    cross_tenant_leakage: int = 0
    concurrent_reconcilers: int = 0
    concurrent_usage_records: int = 0
    concurrent_terminal_transitions: int = 0
    ambiguous_released: bool = False
    unknown_usage_booked_as_zero: bool = False
    legacy_unknown_auto_released: bool = False
    cases: dict[str, dict[str, Any]] = field(default_factory=dict)


def classify(result: ReconciliationResult) -> tuple[str, str]:
    missing = sorted(REQUIRED - set(result.checks))
    if missing:
        return "FAIL", f"reservation_reconciliation invariant not executed: {missing[0]}"
    failed = sorted(name for name in REQUIRED if not result.checks[name])
    if failed:
        return "FAIL", f"reservation_reconciliation invariant failed: {failed[0]}"
    for counter in ZERO_COUNTERS:
        if getattr(result, counter):
            return "FAIL", f"{counter} must be 0"
    for flag in FALSE_FLAGS:
        if getattr(result, flag):
            return "FAIL", f"{flag} must be false"
    if result.concurrent_reconcilers < RECONCILERS:
        return "FAIL", "too few concurrent reconcilers were started"
    if result.concurrent_usage_records > 1:
        return "FAIL", "concurrent_usage_records must be at most 1"
    if result.concurrent_terminal_transitions > 1:
        return "FAIL", "concurrent_terminal_transitions must be at most 1"
    return "PASS", "held reservations were recovered from durable evidence or kept held"


def failure_class(result: ReconciliationResult, status: str, reason: str) -> str | None:
    """Sanitized diagnosis of a failed run; None when the gate passed."""
    if status == "PASS":
        return None
    if status == "BLOCKED":
        return "RESOURCE_BLOCKED" if "memory" in reason.lower() else "UNDETERMINED"
    for name, kind in _CLASS_OF.items():
        if name in reason:
            return kind
    if any("database" in error.lower() or "sql" in error.lower() for error in result.errors):
        return "DB_FAILURE"
    return "UNDETERMINED"


def build_evidence(result: ReconciliationResult) -> dict[str, Any]:
    def ok(name: str) -> bool:
        return bool(result.checks.get(name))

    case = result.cases
    return {
        "gate": "reservation_reconciliation",
        "durable_authority": "postgresql",
        "normal_success_unchanged": ok("normal_success_unchanged"),
        "accounting_failure_case": case.get("accounting_failure", {}),
        "pre_provider_failure_case": case.get("pre_provider_failure", {}),
        "ambiguous_provider_case": case.get("ambiguous_provider", {}),
        "unknown_usage_case": case.get("unknown_usage", {}),
        "existing_usage_case": case.get("existing_usage", {}),
        "concurrency": case.get("concurrency", {}),
        "datastore_failure_case": case.get("datastore_failure", {}),
        "tenant_isolation": case.get("tenant_isolation", {}),
        "legacy_case": case.get("legacy_hold", {}),
        "provider_calls_during_reconciliation": result.provider_calls_during_reconciliation,
        "accounting_failure_recovered": ok("accounting_failure_recovered"),
        "pre_provider_released": ok("pre_provider_released"),
        "same_key_recovery": ok("same_key_recovery"),
        "ambiguous_released": result.ambiguous_released,
        "review_required_verified": ok("ambiguous_case_held"),
        "existing_usage_committed": ok("existing_usage_committed"),
        "duplicate_usage_records": result.duplicate_usage_records,
        "concurrent_reconcilers": result.concurrent_reconcilers,
        "concurrent_usage_records": result.concurrent_usage_records,
        "concurrent_terminal_transitions": result.concurrent_terminal_transitions,
        "reconciliation_db_failure_fail_closed": ok("reconciliation_db_failure_fail_closed"),
        "cross_tenant_leakage": result.cross_tenant_leakage,
        "unknown_usage_booked_as_zero": result.unknown_usage_booked_as_zero,
        "legacy_unknown_auto_released": result.legacy_unknown_auto_released,
        "operator_cli_verified": ok("operator_cli_verified"),
        "ledger_has_no_content": ok("ledger_has_no_content"),
        "provider_invocations": result.provider_invocations,
        "paid_provider_calls": result.paid_provider_calls,
        "cleanup_status": "PASS" if all(ok(name) for name in CLEANUP_CHECKS) else "FAIL",
        "unrelated_data_unchanged": all(ok(name) for name in UNRELATED_CHECKS),
        "metrics": result.metrics,
        "checks": dict(sorted(result.checks.items())),
        "errors": result.errors,
    }


# --------------------------------------------------------------------------- environment


class GateProvider(qi.GateProvider):
    """Counted deterministic provider; refuses (and counts) any call during reconciliation."""

    def __init__(self) -> None:
        super().__init__()
        self.unreported: set[str] = set()
        self.reconciling = False
        self.refused = 0

    def generate(self, question: str, citations: Any, context: str) -> Any:
        from ragops.llm.providers import LLMResponse, LLMUsage

        if self.reconciling:
            with self.lock:
                self.refused += 1
            raise AssertionError("provider called during reconciliation")
        response = super().generate(question, citations, context)
        tenant = citations[0].tenant_id if citations else ""
        if tenant not in self.unreported:
            return response
        usage = LLMUsage(0, 0, 0, 0.0, response.usage.latency_ms, self.model, reported=False)
        return LLMResponse(response.text, usage, response.abstained, response.used_source_ids)


@contextmanager
def environment(result: ReconciliationResult, evidence: dict[str, Any]) -> Iterator[Any]:
    from alembic import command
    from alembic.config import Config
    from sqlalchemy import create_engine, text

    base = qi._base_url()
    run_id = str(evidence.get("run_id") or uuid.uuid4().hex[:10])
    name = f"ragops_test_recon_{run_id}"
    admin = create_engine(base, isolation_level="AUTOCOMMIT")
    audit_file = ROOT / "evidence" / "audit-events.jsonl"
    before_files = qi._file_digest(audit_file)
    before_databases = qi._databases(admin)
    work = Path(tempfile.mkdtemp(prefix="rc-recon-"))
    previous_url = os.environ.get("RAGOPS_DATABASE_URL")
    engine = server = thread = None
    created = False
    try:
        with admin.connect() as connection:
            connection.execute(text(f'CREATE DATABASE "{name}"'))
        created = True
        url = base.set(database=name)
        engine = create_engine(url)
        with engine.begin() as connection:
            config = Config(str(ROOT / "alembic.ini"))
            config.set_main_option("script_location", str(ROOT / "migrations"))
            config.attributes["connection"] = connection
            command.upgrade(config, "head")
        tenants = {label: f"tenant-recon{run_id}-{label}" for label in SCENARIOS}
        qi._provision(engine, tenants)
        rendered = url.render_as_string(hide_password=False)
        os.environ["RAGOPS_DATABASE_URL"] = rendered
        private, public = qi._keys()
        hmac_secret = token_urlsafe(48)
        provider = GateProvider()
        server, thread, port, app = qi._serve(work, public, provider, tenants, hmac_secret)
        result.check(
            "only_local_provider_registered",
            provider.provider_id == "deterministic" and app.state.query_idempotency is not None,
        )
        keys = {label: f"recon-{run_id}-{label}-{uuid.uuid4().hex}" for label in SCENARIOS}
        yield SimpleNamespace(
            base=f"http://127.0.0.1:{port}",
            engine=engine,
            url=rendered,
            provider=provider,
            tenants=tenants,
            keys=keys,
            private=private,
            audit=work / "evidence" / "audit-events.jsonl",
            secrets=[*keys.values(), qi.QUESTION, qi.OTHER_QUESTION, hmac_secret, rendered],
        )
    finally:
        stopped = True
        if server is not None and thread is not None:
            server.should_exit = True
            thread.join(timeout=15)
            stopped = not thread.is_alive()
        result.check("server_stopped", stopped)
        if engine is not None:
            engine.dispose()
        if previous_url is None:
            os.environ.pop("RAGOPS_DATABASE_URL", None)
        else:
            os.environ["RAGOPS_DATABASE_URL"] = previous_url
        removed = True
        if created:
            with admin.connect() as connection:
                connection.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
            removed = name not in qi._databases(admin)
        result.check("gate_database_removed", removed)
        result.check("unrelated_databases_unchanged", qi._databases(admin) == before_databases)
        admin.dispose()
        shutil.rmtree(work, ignore_errors=True)
        result.check("temporary_files_removed", not work.exists())
        result.check("unrelated_files_unchanged", qi._file_digest(audit_file) == before_files)


# --------------------------------------------------------------------------- helpers


def ask(env: Any, label: str, key: str | None = None) -> qi.Reply:
    return qi.ask(env, label, env.keys[label] if key is None else key)


def _rows(env: Any, sql: str, label: str) -> list[Any]:
    from sqlalchemy import text

    with env.engine.connect() as connection:
        return list(connection.execute(text(sql), {"t": env.tenants[label]}))


def _states(env: Any, label: str) -> dict[str, Any]:
    reservations = [row[0] for row in _rows(env, _RESERVATIONS, label)]
    ledger = [row[0] for row in _rows(env, _LEDGER, label)]
    idempotency = [row[0] for row in _rows(env, _IDEMPOTENCY, label)]
    usage = int(_rows(env, _USAGE, label)[0][0])
    return {
        "reservations": reservations,
        "ledger": ledger,
        "idempotency": idempotency,
        "usage": usage,
    }


_RESERVATIONS = "SELECT status FROM budget_reservations WHERE tenant_id = :t ORDER BY created_at"
_LEDGER = "SELECT state FROM query_executions WHERE tenant_id = :t ORDER BY attempt"
_IDEMPOTENCY = "SELECT state FROM query_idempotency_records WHERE tenant_id = :t"
_USAGE = "SELECT count(*) FROM usage_records WHERE tenant_id = :t"


def _reservation_id(env: Any, label: str) -> uuid.UUID:
    """The one held reservation of a scenario tenant."""
    (row,) = _rows(
        env,
        "SELECT id FROM budget_reservations WHERE tenant_id = :t AND status = 'reserved'",
        label,
    )
    return uuid.UUID(str(row[0]))


def _service(env: Any, label: str) -> Any:
    from ragops.finops.reconciliation import ReconciliationService

    return ReconciliationService(lambda: env.engine, env.tenants[label], grace_seconds=0)


@contextmanager
def reconciling(env: Any, result: ReconciliationResult) -> Iterator[None]:
    """Any provider call while reconciling is refused and counted as a replay attempt."""
    before = sum(env.provider.calls.values())
    env.provider.reconciling = True
    try:
        yield
    finally:
        env.provider.reconciling = False
        replayed = sum(env.provider.calls.values()) - before + env.provider.refused
        result.provider_calls_during_reconciliation += replayed
        env.provider.refused = 0


@contextmanager
def failing(env: Any, table: str, label: str, timing: str = "BEFORE INSERT") -> Iterator[None]:
    """A gate-owned trigger on the gate database rejects writes of exactly one tenant."""
    from sqlalchemy import text

    name = f"gate_recon_{table}_{label}"
    tenant = env.tenants[label]
    with env.engine.begin() as connection:
        connection.execute(
            text(
                f"CREATE FUNCTION {name}() RETURNS trigger AS $$ BEGIN "  # noqa: S608
                "IF NEW.tenant_id = '" + tenant + "' THEN "
                "RAISE EXCEPTION 'injected gate failure'; END IF; RETURN NEW; END; "
                "$$ LANGUAGE plpgsql"
            )
        )
        connection.execute(
            text(
                f"CREATE TRIGGER {name} {timing} ON {table} FOR EACH ROW EXECUTE FUNCTION {name}()"
            )
        )
    try:
        yield
    finally:
        with env.engine.begin() as connection:
            connection.execute(text(f"DROP TRIGGER {name} ON {table}"))
            connection.execute(text(f"DROP FUNCTION {name}()"))


def _receipt_case(env: Any, label: str) -> qi.Reply:
    with failing(env, "usage_records", label):
        return ask(env, label)


# --------------------------------------------------------------------------- stages


def normal_success(env: Any, result: ReconciliationResult) -> None:
    reply = ask(env, "n")
    again = ask(env, "n")
    states = _states(env, "n")
    result.check(
        "normal_success_unchanged",
        reply.status == 200
        and reply.body.get("abstained") is False
        and again.replayed
        and states
        == {
            "reservations": ["committed"],
            "ledger": ["accounted"],
            "idempotency": ["succeeded"],
            "usage": 1,
        }
        and env.provider.calls[env.tenants["n"]] == 1
        and _service(env, "n").cases() == [],
    )


def accounting_failure(env: Any, result: ReconciliationResult) -> None:
    first = _receipt_case(env, "f")
    blocked = ask(env, "f")
    before = _states(env, "f")
    calls_before = env.provider.calls[env.tenants["f"]]
    replays = result.provider_calls_during_reconciliation
    with reconciling(env, result):
        outcome = _service(env, "f").reconcile(_reservation_id(env, "f"))
    replays = result.provider_calls_during_reconciliation - replays
    after = _states(env, "f")
    settled = ask(env, "f")
    from ragops.workflows.query_idempotency import RECONCILED_DETAIL

    result.cases["accounting_failure"] = {
        "provider_calls_before_reconciliation": calls_before,
        "provider_calls_during_reconciliation": replays,
        "usage_records_before": before["usage"],
        "usage_records_after": after["usage"],
        "reservation_before": before["reservations"][0] if before["reservations"] else None,
        "reservation_after": after["reservations"][0] if after["reservations"] else None,
        "ledger_before": before["ledger"],
        "ledger_after": after["ledger"],
        "classification": outcome.classification.value,
        "idempotency_after": after["idempotency"],
    }
    result.check(
        "accounting_failure_recovered",
        first.status == 503
        and first.body == {"detail": "accounting unavailable"}
        and blocked.body == RECONCILIATION
        and calls_before == 1
        and before
        == {
            "reservations": ["reserved"],
            "ledger": ["receipt_recorded"],
            "idempotency": ["ambiguous_held"],
            "usage": 0,
        }
        and outcome.classification.value == "receipt_available"
        and outcome.action == "committed"
        and after["reservations"] == ["committed"]
        and after["ledger"] == ["accounted"]
        and after["usage"] == 1,
    )
    result.check(
        "accounting_failure_key_settled",
        settled.status == 409
        and settled.replayed
        and settled.body == {"detail": RECONCILED_DETAIL}
        and after["idempotency"] == ["reconciled"]
        and env.provider.calls[env.tenants["f"]] == 1,
    )
    # The recovered usage record carries the receipt's cost and the reservation commits it.
    rows = _rows(
        env,
        "SELECT r.final_amount = u.cost AND u.cost = q.cost FROM budget_reservations r "
        "JOIN query_executions q ON q.tenant_id = r.tenant_id AND q.reservation_id = r.id "
        "JOIN usage_records u ON u.tenant_id = q.tenant_id AND u.correlation_id = q.correlation_id "
        "WHERE r.tenant_id = :t",
        "f",
    )
    result.check("accounting_failure_recovered", rows == [(True,)])


def pre_provider_failure(env: Any, result: ReconciliationResult) -> None:
    # Every ledger update of this tenant fails: no marker, and the in-request release fails
    # too, so the reservation is left exactly as a crash before the boundary leaves it.
    with failing(env, "query_executions", "p", "BEFORE UPDATE"):
        first = ask(env, "p")
    before = _states(env, "p")
    result.pre_provider_provider_calls = env.provider.calls[env.tenants["p"]]
    with reconciling(env, result):
        outcome = _service(env, "p").reconcile(_reservation_id(env, "p"))
    after = _states(env, "p")
    retry = ask(env, "p")
    recovered = _states(env, "p")
    result.cases["pre_provider_failure"] = {
        "provider_calls": result.pre_provider_provider_calls,
        "reservation_before": before["reservations"],
        "reservation_after": after["reservations"],
        "ledger_before": before["ledger"],
        "classification": outcome.classification.value,
        "idempotency_after": after["idempotency"],
        "same_key_recovery": retry.status == 200,
        "recovered_ledger": recovered["ledger"],
    }
    result.check(
        "pre_provider_released",
        first.status == 503
        and first.body == {"detail": "accounting unavailable"}
        and before["reservations"] == ["reserved"]
        and before["ledger"] == ["prepared"]
        and before["idempotency"] == ["retryable"]
        and outcome.classification.value == "pre_provider"
        and after["reservations"] == ["released"]
        and after["ledger"] == ["released"],
    )
    result.check(
        "same_key_recovery",
        retry.status == 200
        and not retry.replayed
        and env.provider.calls[env.tenants["p"]] == 1
        and recovered["reservations"] == ["released", "committed"]
        and recovered["ledger"] == ["released", "accounted"]
        and recovered["usage"] == 1,
    )


def ambiguous_provider(env: Any, result: ReconciliationResult) -> None:
    tenant = env.tenants["t"]
    env.provider.timeouts.add(tenant)
    first = ask(env, "t")
    env.provider.timeouts.discard(tenant)
    before = _states(env, "t")
    service = _service(env, "t")
    replays = result.provider_calls_during_reconciliation
    with reconciling(env, result):
        outcome = service.reconcile(_reservation_id(env, "t"))
        batch = service.reconcile_auto()
        case = service.inspect(_reservation_id(env, "t"))
    replays = result.provider_calls_during_reconciliation - replays
    after = _states(env, "t")
    retry = ask(env, "t")
    result.ambiguous_released = "released" in after["reservations"]
    result.cases["ambiguous_provider"] = {
        "provider_calls_during_reconciliation": replays,
        "automatic_release": result.ambiguous_released or bool(batch),
        "reservation_after": after["reservations"][0] if after["reservations"] else None,
        "review_required": case.review_required,
        "classification": outcome.classification.value,
        "ambiguity_reason": case.ambiguity_reason,
    }
    result.check(
        "ambiguous_case_held",
        first.status == 503
        and outcome.classification.value == "unknown_billing"
        and not outcome.changed
        and batch == []
        and case.review_required
        and case.ambiguity_reason == "provider_timeout"
        and before == after
        and after["reservations"] == ["reserved"]
        and after["usage"] == 0
        and retry.body == RECONCILIATION
        and env.provider.calls[tenant] == 1,
    )


def unknown_usage(env: Any, result: ReconciliationResult) -> None:
    tenant = env.tenants["v"]
    env.provider.unreported.add(tenant)
    try:
        first = ask(env, "v")
    finally:
        env.provider.unreported.discard(tenant)
    with reconciling(env, result):
        outcome = _service(env, "v").reconcile(_reservation_id(env, "v"))
    after = _states(env, "v")
    tokens = _rows(
        env,
        "SELECT input_tokens, output_tokens, cost, ambiguity_reason FROM query_executions "
        "WHERE tenant_id = :t",
        "v",
    )
    zero = _rows(
        env,
        "SELECT count(*) FROM usage_records WHERE tenant_id = :t AND cost = 0",
        "v",
    )[0][0]
    result.unknown_usage_booked_as_zero = bool(after["usage"]) or bool(zero)
    result.cases["unknown_usage"] = {
        "classification": outcome.classification.value,
        "usage_records": after["usage"],
        "reservation_after": after["reservations"],
    }
    result.check(
        "unknown_usage_held",
        first.status == 503
        and outcome.classification.value == "unknown_billing"
        and not outcome.changed
        and after["reservations"] == ["reserved"]
        and tokens == [(None, None, None, "usage_not_reported")],
    )


def existing_usage(env: Any, result: ReconciliationResult) -> None:
    from sqlalchemy import text

    _receipt_case(env, "u")
    tenant = env.tenants["u"]
    # Another path booked the usage already (gate-owned SQL copies the durable receipt).
    with env.engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO usage_records (tenant_id, id, created_at, user_id, model_id, "
                "correlation_id, input_tokens, output_tokens, cost, latency_ms, endpoint, "
                "workflow, provider_id, model_id_text, fallback_count) "
                "SELECT tenant_id, gen_random_uuid(), now(), user_id, model_configuration_id, "
                "correlation_id, input_tokens, output_tokens, cost, latency_ms, 'rag', 'query', "
                "provider_id, model_id_text, fallback_count FROM query_executions "
                "WHERE tenant_id = :t"
            ),
            {"t": tenant},
        )
    with reconciling(env, result):
        outcome = _service(env, "u").reconcile(_reservation_id(env, "u"))
    after = _states(env, "u")
    duplicates = after["usage"] - 1
    result.duplicate_usage_records += max(0, duplicates)
    result.cases["existing_usage"] = {
        "classification": outcome.classification.value,
        "duplicate_usage_records": max(0, duplicates),
        "reservation_committed": after["reservations"] == ["committed"],
    }
    result.check(
        "existing_usage_committed",
        outcome.classification.value == "already_accounted"
        and outcome.action == "committed"
        and after["usage"] == 1
        and after["reservations"] == ["committed"]
        and after["ledger"] == ["accounted"],
    )


def concurrency(env: Any, result: ReconciliationResult) -> None:
    _receipt_case(env, "c")
    target = _reservation_id(env, "c")
    barrier = threading.Barrier(RECONCILERS)

    def one() -> Any:
        service = _service(env, "c")
        barrier.wait(timeout=15)
        return service.reconcile(target)

    with reconciling(env, result), ThreadPoolExecutor(max_workers=RECONCILERS) as pool:
        outcomes = [future.result() for future in [pool.submit(one) for _ in range(RECONCILERS)]]
    after = _states(env, "c")
    result.concurrent_reconcilers = len(outcomes)
    result.concurrent_usage_records = after["usage"]
    result.concurrent_terminal_transitions = sum(outcome.changed for outcome in outcomes)
    classes = Counter(outcome.classification.value for outcome in outcomes)
    result.cases["concurrency"] = {
        "reconcilers": len(outcomes),
        "usage_records": after["usage"],
        "reservation_terminal_transitions": result.concurrent_terminal_transitions,
        "classifications": dict(sorted(classes.items())),
    }
    result.check(
        "concurrency_verified",
        result.concurrent_terminal_transitions == 1
        and after["usage"] == 1
        and after["reservations"] == ["committed"]
        and set(classes) <= {"receipt_available", "resolved"},
    )


def datastore_failure(env: Any, result: ReconciliationResult) -> None:
    from ragops.finops.reconciliation import ReconciliationUnavailable

    _receipt_case(env, "d")
    before = _states(env, "d")
    refused = False
    with failing(env, "usage_records", "d"), reconciling(env, result):
        try:
            _service(env, "d").reconcile(_reservation_id(env, "d"))
        except ReconciliationUnavailable:
            refused = True
    after = _states(env, "d")
    result.cases["datastore_failure"] = {"fail_closed": refused, "state_unchanged": before == after}
    result.check("reconciliation_db_failure_fail_closed", refused and before == after)


def tenant_isolation(env: Any, result: ReconciliationResult) -> None:
    from ragops.finops.reconciliation import CaseNotFound

    probes: list[uuid.UUID] = []
    for label in ("t", "v", "d"):
        for row in _rows(
            env,
            "SELECT reservation_id, correlation_id, id FROM query_executions WHERE tenant_id = :t",
            label,
        ):
            probes.extend(uuid.UUID(str(value)) for value in row)
    before = {label: _states(env, label) for label in ("t", "v", "d")}
    other = _service(env, "x")
    leaked = 0
    with reconciling(env, result):
        for probe in probes:
            for action in (other.inspect, other.reconcile):
                try:
                    action(probe)
                    leaked += 1
                except CaseNotFound:
                    pass
        leaked += len(other.cases()) + len(other.reconcile_auto())
    after = {label: _states(env, label) for label in ("t", "v", "d")}
    result.cross_tenant_leakage += leaked + int(before != after)
    result.cases["tenant_isolation"] = {
        "probes": len(probes) * 2,
        "cross_tenant_leakage": leaked + int(before != after),
    }
    result.check("cross_tenant_not_found", leaked == 0 and before == after and len(probes) >= 9)


def legacy_hold(env: Any, result: ReconciliationResult) -> None:
    from sqlalchemy import text

    tenant = env.tenants["l"]
    correlation = uuid.uuid4()
    with env.engine.begin() as connection:
        budget = connection.execute(
            text("SELECT id FROM tenant_budgets WHERE tenant_id = :t"), {"t": tenant}
        ).scalar_one()
        connection.execute(
            text(
                "INSERT INTO budget_reservations (tenant_id, id, created_at, budget_id, "
                "idempotency_key, estimated_amount, currency, status) VALUES (:t, "
                "gen_random_uuid(), now() - interval '30 days', :b, :k, 0.5, 'EUR', 'reserved')"
            ),
            {"t": tenant, "b": budget, "k": f"query:{correlation}"},
        )
        connection.execute(
            text(
                "INSERT INTO query_idempotency_records (tenant_id, id, created_at, principal, "
                "endpoint, key_hash, request_fingerprint, correlation_id, state, attempt, "
                "updated_at, lease_expires_at, expires_at) VALUES (:t, gen_random_uuid(), "
                "now() - interval '30 days', 'legacy', 'POST /v1/query', 'legacy', 'legacy', "
                ":c, 'ambiguous_held', 1, now(), now(), now())"
            ),
            {"t": tenant, "c": correlation},
        )
    service = _service(env, "l")
    with reconciling(env, result):
        (case,) = service.cases()
        outcome = service.reconcile(correlation)
        batch = service.reconcile_auto()
    after = _states(env, "l")
    result.legacy_unknown_auto_released = "reserved" not in after["reservations"] or bool(batch)
    result.cases["legacy_hold"] = {
        "classification": case.classification.value,
        "legacy": case.legacy,
        "review_required": case.review_required,
        "reservation_after": after["reservations"],
    }
    result.check(
        "legacy_hold_kept",
        case.legacy
        and case.classification.value == "unknown_billing"
        and not outcome.changed
        and batch == []
        and after["reservations"] == ["reserved"]
        and after["idempotency"] == ["ambiguous_held"],
    )


def operator_cli(env: Any, result: ReconciliationResult) -> None:
    command = [
        sys.executable,
        str(ROOT / "scripts" / "reconcile_reservations.py"),
        "--tenant",
        env.tenants["t"],
        "--grace-seconds",
        "0",
        "list",
    ]
    with reconciling(env, result):
        completed = subprocess.run(  # noqa: S603 - fixed argument list, gate-owned database
            command,
            env={**os.environ, "RAGOPS_DATABASE_URL": env.url},
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    output = completed.stdout + completed.stderr
    try:
        listing = json.loads(completed.stdout)
    except json.JSONDecodeError:
        listing = {}
    result.check(
        "operator_cli_verified",
        completed.returncode == 0
        and listing.get("count") == 1
        and listing.get("review_required") == 1
        and listing.get("auto_safe") == 0
        and not any(value in output for value in env.secrets),
    )
    from sqlalchemy import text

    with env.engine.connect() as connection:
        stored = " ".join(
            json.dumps(dict(row), default=str)
            for row in connection.execute(text("SELECT * FROM query_executions")).mappings()
        )
    audit = env.audit.read_text(encoding="utf-8") if env.audit.exists() else ""
    result.check(
        "ledger_has_no_content",
        bool(stored)
        and not any(value in stored for value in env.secrets)
        and not any(value in audit for value in env.secrets)
        and "Projekt ORBIT" not in stored,
    )
    result.provider_invocations = sum(env.provider.calls.values())


# --------------------------------------------------------------------------- main


def run(result: ReconciliationResult, evidence: dict[str, Any], secrets: list[str]) -> None:
    with environment(result, evidence) as env:
        secrets.extend(env.secrets)
        module = sys.modules[__name__]
        for name in STAGES:
            result.metrics["last_stage"] = name
            getattr(module, name)(env, result)


def _finish(
    evidence: dict[str, Any],
    status: str,
    reason: str,
    exit_code: int,
    secrets: tuple[str, ...] = (),
) -> int:
    from scripts.security_gate import leaks

    evidence.update(
        {
            "status": status,
            "reason": reason,
            "exit_code": exit_code,
            "secret_scan_passed": True,
            "timestamp": datetime.now(UTC).isoformat(),
        }
    )
    serialized = json.dumps(evidence, indent=2, default=str) + "\n"
    if leaks(serialized, tuple(value for value in secrets if value)) or e2e.SECRET_PATTERNS.search(
        serialized
    ):
        evidence = {
            key: evidence.get(key) for key in ("gate", "tested_commit", "run_id", "timestamp")
        }
        evidence.update(
            {
                "status": "FAIL",
                "reason": "evidence secret scan failed",
                "failure_classification": "UNDETERMINED",
                "exit_code": 1,
                "secret_scan_passed": False,
            }
        )
        serialized = json.dumps(evidence, indent=2) + "\n"
        exit_code = 1
    EVIDENCE.parent.mkdir(parents=True, exist_ok=True)
    EVIDENCE.write_text(serialized, encoding="utf-8")
    return exit_code


def main() -> int:
    run_id = uuid.uuid4().hex[:10]
    evidence: dict[str, Any] = {
        "gate": "reservation_reconciliation",
        "tested_commit": e2e._commit(),
        "run_id": run_id,
    }
    result = ReconciliationResult()
    secrets: list[str] = []
    try:
        run(result, evidence, secrets)
    except PrerequisiteMissing as exc:
        reason = e2e._sanitize(str(exc))
        evidence["failure_classification"] = failure_class(result, "BLOCKED", reason)
        return _finish(evidence, "BLOCKED", reason, 2)
    except Exception as exc:  # noqa: BLE001 - any stage failure is a gate failure
        stage = result.metrics.get("last_stage", "setup")
        result.errors.append(f"{stage}: {e2e._first_line(exc)}")
    evidence.update(build_evidence(result))
    status, reason = classify(result)
    if result.errors and status == "PASS":
        status, reason = "FAIL", "a gate stage raised an error"
    evidence["failure_classification"] = failure_class(result, status, reason)
    return _finish(evidence, status, reason, 0 if status == "PASS" else 1, tuple(secrets))


if __name__ == "__main__":
    raise SystemExit(main())
