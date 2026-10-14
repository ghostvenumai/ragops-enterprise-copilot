"""query_idempotency RC gate: same-key retries of /v1/query on the real API and PostgreSQL.

The gate serves the real FastAPI application over loopback HTTP (uvicorn in this process)
in vector query mode, backed by a gate-owned PostgreSQL database migrated to head. Tokens
come from an ephemeral RS256 issuer and the key digest uses a per-run HMAC secret; budget
reservation, usage booking and the idempotency
store run unchanged. The provider is the local deterministic one, wrapped only to count its
invocations per tenant and to inject a provider timeout for one tenant.

Scenarios, one isolated tenant each: successful replay, conflicting payload, the same key in
another tenant and for another user, zero-result replay, ten concurrent duplicates, an
accounting failure after provider success (PostgreSQL trigger), an ambiguous provider
timeout and an idempotency-store failure (PostgreSQL trigger). Evidence holds counts,
states and digests only: no keys, questions, answers, tokens or credentials. The gate drops
its database and temporary files and checks that nothing else changed.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid
from collections import Counter
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from secrets import token_urlsafe
from types import SimpleNamespace
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import browser_e2e_gate as e2e  # noqa: E402

EVIDENCE = ROOT / "evidence" / "product-v1" / "rc-live" / "query-idempotency.json"
PrerequisiteMissing = e2e.PrerequisiteMissing
CONCURRENT_REQUESTS = 10
ISSUER = "https://issuer.query-idempotency-gate.invalid/"
AUDIENCE = "ragops-api"
DIMENSION = 64
TOP_K = 3
PRICES = (Decimal("0.00001"), Decimal("0.00002"))
QUESTION = "Welche Freigaberegel gilt im Projekt ORBIT?"
OTHER_QUESTION = "Welche Eskalationsstufe gilt im Projekt ORBIT?"
DOCUMENT = "Im Projekt ORBIT gilt für Tenant {tenant} die Freigaberegel Stufe {level}."
SCENARIOS = ("a", "b", "c", "z", "f", "t", "d")  # z has no documents

STAGES = (
    "successful_replay",
    "conflict",
    "cross_tenant",
    "cross_user",
    "zero_result",
    "concurrency",
    "accounting_failure",
    "ambiguous_provider",
    "db_failure",
    "storage_redaction",
)
CLEANUP_CHECKS = ("server_stopped", "gate_database_removed", "temporary_files_removed")
UNRELATED_CHECKS = ("unrelated_databases_unchanged", "unrelated_files_unchanged")
REQUIRED = frozenset(
    {
        "durable_store_used",
        "keyed_digest_used",
        "only_local_provider_registered",
        "successful_replay_verified",
        "replay_marker_verified",
        "original_logical_correlation_id_preserved",
        "conflict_verified",
        "cross_tenant_same_key_verified",
        "cross_user_same_key_verified",
        "zero_result_replay_verified",
        "concurrency_verified",
        "accounting_failure_retry_blocked",
        "accounting_failure_answer_suppressed",
        "ambiguous_provider_retry_blocked",
        "idempotency_db_failure_fail_closed",
        "raw_keys_not_stored",
        "raw_keys_not_logged",
        *CLEANUP_CHECKS,
        *UNRELATED_CHECKS,
    }
)
ZERO_COUNTERS = (
    "idempotency_tenant_leakage",
    "idempotency_user_leakage",
    "response_tenant_leakage",
    "usage_tenant_leakage",
    "budget_tenant_leakage",
    "duplicate_idempotency_records",
    "conflict_provider_calls",
    "zero_result_provider_calls",
    "zero_result_usage_records",
    "zero_result_reservations",
    "db_failure_provider_calls",
    "paid_provider_calls",
)
ONE_COUNTERS = (
    "replay_provider_calls",
    "replay_usage_records",
    "replay_reservations",
    "accounting_failure_provider_calls",
    "ambiguous_provider_calls",
)
AT_MOST_ONE_COUNTERS = (
    "concurrent_provider_calls",
    "concurrent_usage_records",
    "concurrent_reservations",
)
RECONCILIATION = "idempotent request requires reconciliation"


@dataclass
class IdempotencyResult(e2e.GateResult):
    idempotency_tenant_leakage: int = 0
    idempotency_user_leakage: int = 0
    response_tenant_leakage: int = 0
    usage_tenant_leakage: int = 0
    budget_tenant_leakage: int = 0
    duplicate_idempotency_records: int = 0
    replay_provider_calls: int = 0
    replay_usage_records: int = 0
    replay_reservations: int = 0
    conflict_provider_calls: int = 0
    concurrent_request_count: int = 0
    concurrent_provider_calls: int = 0
    concurrent_usage_records: int = 0
    concurrent_reservations: int = 0
    zero_result_provider_calls: int = 0
    zero_result_usage_records: int = 0
    zero_result_reservations: int = 0
    accounting_failure_provider_calls: int = 0
    ambiguous_provider_calls: int = 0
    db_failure_provider_calls: int = 0
    retained_ambiguous_records: int = 0
    raw_keys_stored: bool = False


def classify(result: IdempotencyResult) -> tuple[str, str]:
    missing = sorted(REQUIRED - set(result.checks))
    if missing:
        return "FAIL", f"query_idempotency invariant not executed: {missing[0]}"
    failed = sorted(name for name in REQUIRED if not result.checks[name])
    if failed:
        return "FAIL", f"query_idempotency invariant failed: {failed[0]}"
    for counter in ZERO_COUNTERS:
        if getattr(result, counter):
            return "FAIL", f"{counter} must be 0"
    for counter in ONE_COUNTERS:
        if getattr(result, counter) != 1:
            return "FAIL", f"{counter} must be 1"
    for counter in AT_MOST_ONE_COUNTERS:
        if getattr(result, counter) > 1:
            return "FAIL", f"{counter} must be at most 1"
    if result.concurrent_request_count < CONCURRENT_REQUESTS:
        return "FAIL", "too few concurrent duplicates were sent"
    if result.raw_keys_stored:
        return "FAIL", "a raw idempotency key was stored"
    return "PASS", "same-key retries were replayed or blocked without duplicate side effects"


def build_evidence(result: IdempotencyResult) -> dict[str, Any]:
    from ragops.workflows.query_idempotency import (
        FINGERPRINT_ALGORITHM,
        KEY_DIGEST_ALGORITHM,
        SCOPE_DESCRIPTION,
    )

    def ok(name: str) -> bool:
        return bool(result.checks.get(name))

    return {
        "gate": "query_idempotency",
        "durable_store_used": ok("durable_store_used"),
        "idempotency_scope": SCOPE_DESCRIPTION,
        "raw_keys_stored": result.raw_keys_stored,
        "raw_keys_logged": not ok("raw_keys_not_logged"),
        "keyed_digest_used": ok("keyed_digest_used"),
        "key_digest_algorithm": KEY_DIGEST_ALGORITHM,
        "request_fingerprint_algorithm": FINGERPRINT_ALGORITHM,
        "successful_replay_verified": ok("successful_replay_verified"),
        "replay_marker_verified": ok("replay_marker_verified"),
        "replay_provider_calls": result.replay_provider_calls,
        "replay_usage_records": result.replay_usage_records,
        "replay_reservations": result.replay_reservations,
        "original_logical_correlation_id_preserved": ok(
            "original_logical_correlation_id_preserved"
        ),
        "concurrent_request_count": result.concurrent_request_count,
        "concurrent_provider_calls": result.concurrent_provider_calls,
        "concurrent_usage_records": result.concurrent_usage_records,
        "concurrent_reservations": result.concurrent_reservations,
        "duplicate_idempotency_records": result.duplicate_idempotency_records,
        "conflict_verified": ok("conflict_verified"),
        "conflict_provider_calls": result.conflict_provider_calls,
        "cross_tenant_same_key_verified": ok("cross_tenant_same_key_verified"),
        "cross_user_same_key_verified": ok("cross_user_same_key_verified"),
        "idempotency_tenant_leakage": result.idempotency_tenant_leakage,
        "idempotency_user_leakage": result.idempotency_user_leakage,
        "response_tenant_leakage": result.response_tenant_leakage,
        "usage_tenant_leakage": result.usage_tenant_leakage,
        "budget_tenant_leakage": result.budget_tenant_leakage,
        "zero_result_replay_verified": ok("zero_result_replay_verified"),
        "zero_result_provider_calls": result.zero_result_provider_calls,
        "zero_result_usage_records": result.zero_result_usage_records,
        "zero_result_reservations": result.zero_result_reservations,
        "accounting_failure_retry_blocked": ok("accounting_failure_retry_blocked"),
        "accounting_failure_answer_suppressed": ok("accounting_failure_answer_suppressed"),
        "accounting_failure_provider_calls": result.accounting_failure_provider_calls,
        "ambiguous_provider_retry_blocked": ok("ambiguous_provider_retry_blocked"),
        "ambiguous_provider_calls": result.ambiguous_provider_calls,
        "idempotency_db_failure_fail_closed": ok("idempotency_db_failure_fail_closed"),
        "db_failure_provider_calls": result.db_failure_provider_calls,
        "retained_ambiguous_records": result.retained_ambiguous_records,
        "provider_invocations": result.provider_invocations,
        "paid_provider_calls": result.paid_provider_calls,
        "cleanup_status": "PASS" if all(ok(name) for name in CLEANUP_CHECKS) else "FAIL",
        "unrelated_data_unchanged": all(ok(name) for name in UNRELATED_CHECKS),
        "metrics": result.metrics,
        "checks": dict(sorted(result.checks.items())),
        "errors": result.errors,
    }


# --------------------------------------------------------------------------- environment


class GateProvider:
    """The deterministic provider, counted per tenant; one tenant may be made to time out."""

    # It wraps the in-process deterministic provider: nothing leaves the host (ENT-13.1).
    egress_locality = "local"

    def __init__(self) -> None:
        from ragops.llm.providers import DeterministicTestProvider

        self.inner = DeterministicTestProvider()
        self.provider_id, self.model = self.inner.provider_id, self.inner.model
        self.calls: Counter[str] = Counter()
        self.lock = threading.Lock()
        self.timeouts: set[str] = set()
        self.holds: dict[str, threading.Event] = {}

    def health(self) -> str:
        return self.inner.health()

    def generate(self, question: str, citations: Any, context: str) -> Any:
        from ragops.modeling.router import ProviderError, ProviderErrorCategory

        tenant = citations[0].tenant_id if citations else ""
        with self.lock:
            self.calls[tenant] += 1
        if tenant in self.timeouts:
            raise ProviderError(ProviderErrorCategory.TIMEOUT)
        hold = self.holds.get(tenant)
        if hold is not None:
            hold.wait(timeout=15)
        return self.inner.generate(question, citations, context)


def _keys() -> tuple[str, str]:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return (
        private.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ).decode(),
        private.public_key()
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        .decode(),
    )


def _base_url() -> Any:
    from sqlalchemy.engine import make_url

    raw = os.environ.get("RAGOPS_TEST_DATABASE_URL", "")
    if not raw.startswith("postgresql+psycopg://"):
        raise PrerequisiteMissing("RAGOPS_TEST_DATABASE_URL is not an isolated PostgreSQL URL")
    url = make_url(raw)
    if not (url.database or "").startswith("ragops_test_"):
        raise PrerequisiteMissing("RAGOPS_TEST_DATABASE_URL must name a ragops_test_* database")
    return url


def _databases(admin: Any) -> list[str]:
    from sqlalchemy import text

    with admin.connect() as connection:
        return sorted(
            connection.execute(
                text("SELECT datname FROM pg_database WHERE NOT datistemplate")
            ).scalars()
        )


def _file_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else ""


def _provision(engine: Any, tenants: dict[str, str]) -> None:
    from ragops.persistence.database import transaction
    from ragops.persistence.models import (
        ModelConfiguration,
        ProviderConfiguration,
        Tenant,
        TenantBudget,
        User,
    )

    today = datetime.now(UTC).date()
    start = today.replace(day=1)
    end = (start + timedelta(days=32)).replace(day=1)
    with transaction(engine) as session:
        for tenant in tenants.values():
            session.add(Tenant(id=tenant, name=tenant))
            session.flush()
            for subject in (f"{tenant}-user", f"{tenant}-second"):
                session.add(User(tenant_id=tenant, issuer=ISSUER, subject=subject))
            provider = ProviderConfiguration(
                tenant_id=tenant, name="deterministic", kind="deterministic"
            )
            session.add(provider)
            session.flush()
            session.add(
                ModelConfiguration(
                    tenant_id=tenant,
                    provider_id=provider.id,
                    name="deterministic-ragops-v1",
                    input_price=PRICES[0],
                    output_price=PRICES[1],
                )
            )
            session.add(
                TenantBudget(
                    tenant_id=tenant,
                    period_start=start,
                    period_end=end,
                    budget_amount=Decimal("100"),
                    hard_limit_threshold=Decimal("1"),
                    enforcement_mode="hard_limit",
                )
            )


def _index(tenants: dict[str, str]) -> Any:
    from ragops.vector.embedding import EMBEDDING_MODEL, DeterministicEmbeddingProvider
    from ragops.vector.index import DeterministicVectorIndex, VectorPayload

    embedding = DeterministicEmbeddingProvider(DIMENSION)
    index = DeterministicVectorIndex(DIMENSION)
    now = datetime.now(UTC)
    points = []
    for level, (label, tenant) in enumerate(sorted(tenants.items()), start=1):
        if label == "z":
            continue
        chunk = DOCUMENT.format(tenant=label.upper(), level=level)
        points.append(
            (
                embedding.embed([chunk])[0],
                VectorPayload(
                    tenant_id=tenant,
                    workspace_id="w",
                    collection_id="c",
                    document_id=f"doc-{tenant}",
                    document_version_id=f"doc-{tenant}-v1",
                    chunk_id=f"doc-{tenant}-v1:0",
                    access_level="internal",
                    document_status="indexed",
                    version_status="indexed",
                    content_hash=hashlib.sha256(chunk.encode()).hexdigest(),
                    chunk_index=0,
                    source_name="orbit.txt",
                    created_at=now.isoformat(),
                    valid_from=(now - timedelta(minutes=1)).isoformat(),
                    title=f"ORBIT {label.upper()}",
                    chunk_text=chunk,
                    embedding_model=EMBEDDING_MODEL,
                ),
            )
        )
    index.upsert_chunks(points)
    return index


@contextmanager
def environment(result: IdempotencyResult, evidence: dict[str, Any]) -> Iterator[Any]:
    from alembic import command
    from alembic.config import Config
    from sqlalchemy import create_engine, text

    base = _base_url()
    run_id = str(evidence.get("run_id") or uuid.uuid4().hex[:10])
    name = f"ragops_test_idem_{run_id}"
    admin = create_engine(base, isolation_level="AUTOCOMMIT")
    audit_file = ROOT / "evidence" / "audit-events.jsonl"
    before_files = _file_digest(audit_file)
    before_databases = _databases(admin)
    work = Path(tempfile.mkdtemp(prefix="rc-idem-"))
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
        tenants = {label: f"tenant-idem{run_id}-{label}" for label in SCENARIOS}
        _provision(engine, tenants)
        os.environ["RAGOPS_DATABASE_URL"] = url.render_as_string(hide_password=False)
        private, public = _keys()
        hmac_secret = token_urlsafe(48)
        provider = GateProvider()
        server, thread, port, app = _serve(work, public, provider, tenants, hmac_secret)
        # create_app registers exactly the adapter it is given: no paid provider is reachable.
        result.check(
            "only_local_provider_registered",
            provider.provider_id == "deterministic" and app.state.query_idempotency is not None,
        )
        keys = {label: f"idem-{run_id}-{label}-{uuid.uuid4().hex}" for label in SCENARIOS}
        yield SimpleNamespace(
            base=f"http://127.0.0.1:{port}",
            engine=engine,
            provider=provider,
            tenants=tenants,
            keys=keys,
            private=private,
            audit=work / "evidence" / "audit-events.jsonl",
            hmac_secret=hmac_secret,
            secrets=[*keys.values(), QUESTION, OTHER_QUESTION, hmac_secret],
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
            removed = name not in _databases(admin)
        result.check("gate_database_removed", removed)
        result.check("unrelated_databases_unchanged", _databases(admin) == before_databases)
        admin.dispose()
        shutil.rmtree(work, ignore_errors=True)
        result.check("temporary_files_removed", not work.exists())
        result.check("unrelated_files_unchanged", _file_digest(audit_file) == before_files)


def _serve(
    work: Path,
    public_key: str,
    provider: GateProvider,
    tenants: dict[str, str],
    hmac_secret: str,
) -> tuple[Any, threading.Thread, int, Any]:
    import uvicorn

    from ragops.api.app import create_app
    from ragops.config.settings import Settings

    settings = Settings(
        environment="test",
        identity_provider="oidc",
        oidc_issuer=ISSUER,
        oidc_audience=AUDIENCE,
        oidc_public_key=public_key,
        evidence_dir=work / "evidence",
        data_dir=work / "data",
        vector_provider="qdrant",
        qdrant_url="http://127.0.0.1:9",
        embedding_dimension=DIMENSION,
        query_mode="vector",
        query_idempotency_hmac_key=hmac_secret,
    )
    app = create_app(settings, vector_index=_index(tenants), llm_provider=provider)
    port = e2e.free_port()
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", access_log=False)
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 20
    while not server.started:
        if time.monotonic() > deadline or not thread.is_alive():
            raise RuntimeError("API server did not start")
        time.sleep(0.05)
    return server, thread, port, app


# --------------------------------------------------------------------------- requests


@dataclass(frozen=True)
class Reply:
    status: int
    replayed: bool
    body: dict[str, Any]


def ask(
    env: Any,
    label: str,
    key: str | None,
    *,
    question: str = QUESTION,
    subject: str | None = None,
) -> Reply:
    import jwt

    tenant = env.tenants[label]
    claims = {
        "iss": ISSUER,
        "aud": AUDIENCE,
        "sub": subject or f"{tenant}-user",
        "tenant_id": tenant,
        "roles": ["viewer"],
        "exp": datetime.now(UTC) + timedelta(minutes=5),
    }
    headers = {
        "Authorization": f"Bearer {jwt.encode(claims, env.private, algorithm='RS256')}",
        "Content-Type": "application/json",
    }
    if key is not None:
        headers["Idempotency-Key"] = key
    request = urllib.request.Request(  # noqa: S310 - loopback URL built by the gate
        f"{env.base}/v1/query",
        data=json.dumps({"question": question, "top_k": TOP_K}).encode(),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
            return Reply(
                response.status,
                response.headers.get("Idempotency-Replayed") == "true",
                json.loads(response.read()),
            )
    except urllib.error.HTTPError as error:
        return Reply(
            error.code,
            error.headers.get("Idempotency-Replayed") == "true",
            json.loads(error.read() or b"{}"),
        )


def _count(env: Any, table: str, label: str, extra: str = "") -> int:
    from sqlalchemy import text

    with env.engine.connect() as connection:
        return int(
            connection.execute(
                text(f"SELECT count(*) FROM {table} WHERE tenant_id = :t {extra}"),  # noqa: S608
                {"t": env.tenants[label]},
            ).scalar_one()
        )


def _record_state(env: Any, label: str) -> list[str]:
    from sqlalchemy import text

    with env.engine.connect() as connection:
        return list(
            connection.execute(
                text("SELECT state FROM query_idempotency_records WHERE tenant_id = :t"),
                {"t": env.tenants[label]},
            ).scalars()
        )


def _foreign_citations(reply: Reply, tenant: str) -> int:
    return sum(1 for item in reply.body.get("citations", []) if item.get("tenant_id") != tenant)


# --------------------------------------------------------------------------- stages


def successful_replay(env: Any, result: IdempotencyResult) -> None:
    first = ask(env, "a", env.keys["a"])
    second = ask(env, "a", env.keys["a"])
    result.replay_provider_calls = env.provider.calls[env.tenants["a"]]
    result.replay_usage_records = _count(env, "usage_records", "a")
    result.replay_reservations = _count(env, "budget_reservations", "a")
    result.response_tenant_leakage += _foreign_citations(first, env.tenants["a"])
    result.response_tenant_leakage += _foreign_citations(second, env.tenants["a"])
    correlation = first.body.get("correlation_id")
    from sqlalchemy import text

    with env.engine.connect() as connection:
        booked = (
            connection.execute(
                text("SELECT correlation_id::text FROM usage_records WHERE tenant_id = :t"),
                {"t": env.tenants["a"]},
            )
            .scalars()
            .all()
        )
    result.check("durable_store_used", _record_state(env, "a") == ["succeeded"])
    result.check("replay_marker_verified", second.replayed and not first.replayed)
    result.check(
        "original_logical_correlation_id_preserved",
        bool(correlation)
        and second.body.get("correlation_id") == correlation
        and booked == [correlation],
    )
    result.check(
        "successful_replay_verified",
        first.status == second.status == 200
        and first.body.get("abstained") is False
        and second.body == first.body,
    )


def conflict(env: Any, result: IdempotencyResult) -> None:
    before = env.provider.calls[env.tenants["a"]]
    reply = ask(env, "a", env.keys["a"], question=OTHER_QUESTION)
    result.conflict_provider_calls = env.provider.calls[env.tenants["a"]] - before
    result.check(
        "conflict_verified",
        reply.status == 409
        and reply.body == {"detail": "idempotency key reused with a different request"}
        and _count(env, "usage_records", "a") == 1
        and _count(env, "budget_reservations", "a") == 1,
    )


def cross_tenant(env: Any, result: IdempotencyResult) -> None:
    # Tenant B reuses tenant A's raw key: it must neither replay nor conflict.
    reply = ask(env, "b", env.keys["a"])
    replay_a = ask(env, "a", env.keys["a"])
    result.response_tenant_leakage += _foreign_citations(reply, env.tenants["b"])
    result.response_tenant_leakage += _foreign_citations(replay_a, env.tenants["a"])
    leaked = reply.replayed or reply.body.get("correlation_id") == replay_a.body.get(
        "correlation_id"
    )
    result.idempotency_tenant_leakage += int(leaked)
    result.check(
        "cross_tenant_same_key_verified",
        reply.status == 200
        and not leaked
        and env.provider.calls[env.tenants["b"]] == 1
        and _count(env, "usage_records", "b") == 1
        and _record_state(env, "b") == ["succeeded"],
    )


def cross_user(env: Any, result: IdempotencyResult) -> None:
    tenant = env.tenants["a"]
    first = ask(env, "a", env.keys["a"])
    second = ask(env, "a", env.keys["a"], subject=f"{tenant}-second")
    leaked = second.replayed or second.body.get("correlation_id") == first.body.get(
        "correlation_id"
    )
    result.idempotency_user_leakage += int(leaked)
    result.check(
        "cross_user_same_key_verified",
        first.replayed
        and second.status == 200
        and not leaked
        and sorted(_record_state(env, "a")) == ["succeeded", "succeeded"],
    )


def zero_result(env: Any, result: IdempotencyResult) -> None:
    first = ask(env, "z", env.keys["z"])
    second = ask(env, "z", env.keys["z"])
    result.zero_result_provider_calls = env.provider.calls[env.tenants["z"]]
    result.zero_result_usage_records = _count(env, "usage_records", "z")
    result.zero_result_reservations = _count(env, "budget_reservations", "z")
    result.check(
        "zero_result_replay_verified",
        first.status == second.status == 200
        and first.body.get("abstained") is True
        and second.replayed
        and second.body == first.body,
    )


def concurrency(env: Any, result: IdempotencyResult) -> None:
    tenant = env.tenants["c"]
    env.provider.holds[tenant] = threading.Event()
    with ThreadPoolExecutor(max_workers=CONCURRENT_REQUESTS) as pool:
        futures = [pool.submit(ask, env, "c", env.keys["c"]) for _ in range(CONCURRENT_REQUESTS)]
        time.sleep(1.5)
        env.provider.holds[tenant].set()
        replies = [future.result() for future in futures]
    result.concurrent_request_count = len(replies)
    result.concurrent_provider_calls = env.provider.calls[tenant]
    result.concurrent_usage_records = _count(env, "usage_records", "c")
    result.concurrent_reservations = _count(env, "budget_reservations", "c")
    statuses = Counter(reply.status for reply in replies)
    result.metrics["concurrent_statuses"] = {str(key): value for key, value in statuses.items()}
    in_progress = all(
        reply.body == {"detail": "idempotent request in progress"}
        for reply in replies
        if reply.status == 409
    )
    after = ask(env, "c", env.keys["c"])
    result.check(
        "concurrency_verified",
        statuses[200] >= 1
        and set(statuses) <= {200, 409}
        and in_progress
        and after.replayed
        and env.provider.calls[tenant] == 1
        and len(_record_state(env, "c")) == 1,
    )


def accounting_failure(env: Any, result: IdempotencyResult) -> None:
    from sqlalchemy import text

    tenant = env.tenants["f"]
    with env.engine.begin() as connection:
        connection.execute(
            text(
                "CREATE FUNCTION gate_idem_usage_fail() RETURNS trigger AS $$ BEGIN "  # noqa: S608
                "IF NEW.tenant_id = '" + tenant + "' THEN "
                "RAISE EXCEPTION 'injected accounting failure'; END IF; RETURN NEW; END; "
                "$$ LANGUAGE plpgsql"
            )
        )
        connection.execute(
            text(
                "CREATE TRIGGER gate_idem_usage_fail BEFORE INSERT ON usage_records "
                "FOR EACH ROW EXECUTE FUNCTION gate_idem_usage_fail()"
            )
        )
    try:
        first = ask(env, "f", env.keys["f"])
    finally:
        with env.engine.begin() as connection:
            connection.execute(text("DROP TRIGGER gate_idem_usage_fail ON usage_records"))
            connection.execute(text("DROP FUNCTION gate_idem_usage_fail()"))
    retries = [ask(env, "f", env.keys["f"]) for _ in range(2)]  # the database is healthy again
    result.accounting_failure_provider_calls = env.provider.calls[tenant]
    result.check(
        "accounting_failure_answer_suppressed",
        first.status == 503
        and first.body == {"detail": "accounting unavailable"}
        and "answer" not in first.body,
    )
    result.check(
        "accounting_failure_retry_blocked",
        all(reply.status == 409 and reply.body == {"detail": RECONCILIATION} for reply in retries)
        and _count(env, "usage_records", "f") == 0
        and _count(env, "budget_reservations", "f", "AND status = 'reserved'") == 1
        and _count(env, "budget_reservations", "f") == 1
        and _record_state(env, "f") == ["ambiguous_held"],
    )


def ambiguous_provider(env: Any, result: IdempotencyResult) -> None:
    tenant = env.tenants["t"]
    env.provider.timeouts.add(tenant)
    first = ask(env, "t", env.keys["t"])
    env.provider.timeouts.discard(tenant)  # a healthy provider must still not be called again
    retries = [ask(env, "t", env.keys["t"]) for _ in range(2)]
    result.ambiguous_provider_calls = env.provider.calls[tenant]
    result.check(
        "ambiguous_provider_retry_blocked",
        first.status == 503
        and first.body == {"detail": "generation unavailable"}
        and all(
            reply.status == 409 and reply.body == {"detail": RECONCILIATION} for reply in retries
        )
        and _count(env, "usage_records", "t") == 0
        and _count(env, "budget_reservations", "t", "AND status = 'reserved'") == 1
        and _record_state(env, "t") == ["ambiguous_held"],
    )


def db_failure(env: Any, result: IdempotencyResult) -> None:
    from sqlalchemy import text

    tenant = env.tenants["d"]
    with env.engine.begin() as connection:
        connection.execute(
            text(
                "CREATE FUNCTION gate_idem_store_fail() RETURNS trigger AS $$ BEGIN "  # noqa: S608
                "IF NEW.tenant_id = '" + tenant + "' THEN "
                "RAISE EXCEPTION 'injected idempotency store failure'; END IF; RETURN NEW; END; "
                "$$ LANGUAGE plpgsql"
            )
        )
        connection.execute(
            text(
                "CREATE TRIGGER gate_idem_store_fail BEFORE INSERT ON query_idempotency_records "
                "FOR EACH ROW EXECUTE FUNCTION gate_idem_store_fail()"
            )
        )
    try:
        reply = ask(env, "d", env.keys["d"])
    finally:
        with env.engine.begin() as connection:
            connection.execute(
                text("DROP TRIGGER gate_idem_store_fail ON query_idempotency_records")
            )
            connection.execute(text("DROP FUNCTION gate_idem_store_fail()"))
    result.db_failure_provider_calls = env.provider.calls[tenant]
    result.check(
        "idempotency_db_failure_fail_closed",
        reply.status == 503
        and reply.body == {"detail": "idempotency unavailable"}
        and _count(env, "budget_reservations", "d") == 0
        and _count(env, "usage_records", "d") == 0
        and _record_state(env, "d") == [],
    )


def storage_redaction(env: Any, result: IdempotencyResult) -> None:
    from sqlalchemy import text

    with env.engine.connect() as connection:
        stored = " ".join(
            json.dumps(dict(row), default=str)
            for row in connection.execute(
                text("SELECT * FROM query_idempotency_records")
            ).mappings()
        )
        totals = connection.execute(
            text(
                "SELECT count(*), count(DISTINCT (tenant_id, principal, endpoint, key_hash)), "
                "count(*) FILTER (WHERE state IN ('ambiguous_held', 'executing')) "
                "FROM query_idempotency_records"
            )
        ).one()
        # A usage record or reservation that points at another tenant's record or budget.
        result.usage_tenant_leakage = int(
            connection.execute(
                text(
                    "SELECT count(*) FROM usage_records u JOIN query_idempotency_records q "
                    "ON q.correlation_id = u.correlation_id AND q.tenant_id <> u.tenant_id"
                )
            ).scalar_one()
        )
        result.budget_tenant_leakage = int(
            connection.execute(
                text(
                    "SELECT count(*) FROM budget_reservations r JOIN tenant_budgets b "
                    "ON b.id = r.budget_id AND b.tenant_id <> r.tenant_id"
                )
            ).scalar_one()
        )
    result.duplicate_idempotency_records = int(totals[0]) - int(totals[1])
    result.retained_ambiguous_records = int(totals[2])
    result.raw_keys_stored = any(key in stored for key in env.keys.values())
    result.check("raw_keys_not_stored", not result.raw_keys_stored)
    from ragops.workflows.query_idempotency import KeyDigest

    with env.engine.connect() as connection:
        stored_digests = set(
            connection.execute(text("SELECT key_hash FROM query_idempotency_records")).scalars()
        )
    keyed = KeyDigest(env.hmac_secret)
    plain = {hashlib.sha256(key.encode()).hexdigest() for key in env.keys.values()}
    result.check(
        "keyed_digest_used",
        keyed(env.keys["a"]) in stored_digests
        and keyed(env.keys["c"]) in stored_digests
        and not stored_digests & plain,
    )
    audit = env.audit.read_text(encoding="utf-8") if env.audit.exists() else ""
    result.check(
        "raw_keys_not_logged",
        bool(audit) and not any(value in audit for value in env.secrets),
    )
    result.provider_invocations = sum(env.provider.calls.values())


# --------------------------------------------------------------------------- main


def run(result: IdempotencyResult, evidence: dict[str, Any], secrets: list[str]) -> None:
    with environment(result, evidence) as env:
        secrets.extend(env.secrets)
        module = sys.modules[__name__]
        for name in STAGES:
            result.metrics["last_stage"] = name
            getattr(module, name)(env, result)


def _commit() -> str:
    return e2e._commit()


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
    raw_keys = re.compile(r"idem-[0-9a-f]{10}-[a-z]-[0-9a-f]{32}")
    if (
        leaks(serialized, tuple(value for value in secrets if value))
        or e2e.SECRET_PATTERNS.search(serialized)
        or raw_keys.search(serialized)
    ):
        evidence = {
            key: evidence.get(key) for key in ("gate", "tested_commit", "run_id", "timestamp")
        }
        evidence.update(
            {
                "status": "FAIL",
                "reason": "evidence secret scan failed",
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
        "gate": "query_idempotency",
        "tested_commit": _commit(),
        "run_id": run_id,
    }
    result = IdempotencyResult()
    secrets: list[str] = []
    try:
        run(result, evidence, secrets)
    except PrerequisiteMissing as exc:
        return _finish(evidence, "BLOCKED", e2e._sanitize(str(exc)), 2)
    except Exception as exc:  # noqa: BLE001 - any stage failure is a gate failure
        stage = result.metrics.get("last_stage", "setup")
        result.errors.append(f"{stage}: {e2e._first_line(exc)}")
    evidence.update(build_evidence(result))
    status, reason = classify(result)
    if result.errors and status == "PASS":
        status, reason = "FAIL", "a gate stage raised an error"
    return _finish(evidence, status, reason, 0 if status == "PASS" else 1, tuple(secrets))


if __name__ == "__main__":
    raise SystemExit(main())
