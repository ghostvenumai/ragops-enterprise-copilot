"""rag_query RC gate: the productive RAG chain end to end on real services.

The gate runs the real API and worker processes in RAGOPS_QUERY_MODE=vector with an empty
data directory (no demo corpus), real Keycloak tokens, a gate-owned PostgreSQL database,
a gate-owned Redis namespace and a disposable Qdrant service. The API reaches Qdrant only
through a counting proxy, so every query proves that it touched Qdrant.

For two tenants it uploads one document each through the authenticated API, lets the
worker index it and checks the stored chunks in Qdrant; then it asks /v1/query and follows
each answer through citations, audit events, usage records and budget reservations. Unique
per-run markers in the documents tie every citation to the freshly uploaded content. It
also proves zero-result abstention, an injected accounting failure, a hard budget limit, a
vector-store negative control, a Qdrant outage and a browser journey on the vector path.

The provider is the local deterministic one; a watchdog counts every invocation and fails
any paid provider. Evidence holds identifiers, counts and digests only: no document text,
chunk text, prompts, answers, markers, tokens or credentials.
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import sys
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import browser_e2e_gate as e2e  # noqa: E402

EVIDENCE = ROOT / "evidence" / "product-v1" / "rc-live" / "rag-query.json"
PrerequisiteMissing = e2e.PrerequisiteMissing
MIN_MEMORY_GIB = 3.5
# Three chunks of one document keep the query in the SIMPLE routing class, which the
# deterministic catalog model serves; the classifier itself is not changed.
TOP_K = 3
POLL_SECONDS = 90
MODEL = ("deterministic", "deterministic-ragops-v1")
PRICES = (Decimal("0.00001"), Decimal("0.00002"))
QUESTION = "Welches Team betreut das Projekt mit dem Codenamen ORBIT?"
BROWSER_QUESTION = "Welche Wartungsregel gilt im Projekt KOMET?"
NO_CONTEXT = "Ich verweigere eine fachliche Antwort"

REQUIRED = frozenset(
    {
        # ingestion
        "real_api_upload_used",
        "worker_used",
        "real_chunks_written",
        "embedding_model_validated",
        # productive query
        "productive_query_answered",
        "productive_query_uses_qdrant",
        "productive_query_uses_vector_retriever",
        "productive_query_uses_context_builder",
        "productive_query_uses_model_router",
        "reservation_before_provider",
        "finops_enforced",
        "tenant_filter_enforced",
        "request_tenant_override_ignored",
        "unique_nonce_verified",
        "citations_verified",
        "demo_corpus_absent",
        # negative controls
        "zero_result_abstained",
        "negative_control_abstained",
        "qdrant_outage_fail_closed",
        "accounting_failure_suppressed_answer",
        "hard_budget_limit_blocked_provider",
        # browser on the vector path
        "browser_vector_path_verified",
        # cleanup (recorded by the shared environment)
        "processes_stopped",
        "keycloak_gate_objects_removed",
        "redis_gate_keys_removed",
        "unrelated_redis_data_unchanged",
        "gate_database_removed",
        "unrelated_postgres_data_unchanged",
        "unrelated_files_unchanged",
        "disposable_qdrant_removed",
    }
)
CLEANUP_CHECKS = (
    "processes_stopped",
    "keycloak_gate_objects_removed",
    "redis_gate_keys_removed",
    "gate_database_removed",
    "disposable_qdrant_removed",
)
UNRELATED_CHECKS = (
    "unrelated_redis_data_unchanged",
    "unrelated_postgres_data_unchanged",
    "unrelated_files_unchanged",
)
ZERO_COUNTERS = (
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
)


@dataclass
class RagQueryResult(e2e.GateResult):
    vector_tenant_leakage: int = 0
    citation_tenant_leakage: int = 0
    usage_tenant_leakage: int = 0
    budget_tenant_leakage: int = 0
    duplicate_usage_records: int = 0
    zero_result_provider_calls: int = 0
    zero_result_usage_records: int = 0
    placeholder_vectors_remaining: int = 0
    qdrant_requests_observed: int = 0
    usage_records_written: int = 0
    demo_retrieval_used: bool = False
    citation_context_mismatch: int = 0
    latencies_ms: dict[str, float] = field(default_factory=dict)


def classify(result: RagQueryResult) -> tuple[str, str]:
    missing = sorted(REQUIRED - set(result.checks))
    if missing:
        return "FAIL", f"rag_query invariant not executed: {missing[0]}"
    failed = sorted(name for name in REQUIRED if not result.checks[name])
    if failed:
        return "FAIL", f"rag_query invariant failed: {failed[0]}"
    for counter in ZERO_COUNTERS:
        if getattr(result, counter):
            return "FAIL", f"{counter} must be 0"
    if result.demo_retrieval_used:
        return "FAIL", "demo retrieval was used in vector mode"
    if result.qdrant_requests_observed < 1:
        return "FAIL", "no Qdrant request was observed for the query path"
    if result.usage_records_written < 2:
        return "FAIL", "fewer than two successful queries were accounted"
    if result.citation_context_mismatch:
        return "FAIL", "a citation did not match a stored chunk"
    return "PASS", "uploaded documents were answered through Qdrant, router and accounting"


def build_evidence(result: RagQueryResult) -> dict[str, Any]:
    def ok(name: str) -> bool:
        return bool(result.checks.get(name))

    cleanup = all(ok(name) for name in CLEANUP_CHECKS)
    return {
        "gate": "rag_query",
        "query_mode": "vector",
        "real_api_upload_used": ok("real_api_upload_used"),
        "worker_used": ok("worker_used"),
        "real_chunks_written": ok("real_chunks_written"),
        "embedding_model_validated": ok("embedding_model_validated"),
        "placeholder_vectors_remaining": result.placeholder_vectors_remaining,
        "productive_query_uses_qdrant": ok("productive_query_uses_qdrant"),
        "productive_query_uses_vector_retriever": ok("productive_query_uses_vector_retriever"),
        "productive_query_uses_context_builder": ok("productive_query_uses_context_builder"),
        "productive_query_uses_model_router": ok("productive_query_uses_model_router"),
        "reservation_before_provider": ok("reservation_before_provider"),
        "finops_enforced": ok("finops_enforced"),
        "tenant_filter_enforced": ok("tenant_filter_enforced"),
        "request_tenant_override_ignored": ok("request_tenant_override_ignored"),
        "tenant_leakage": result.tenant_leakage,
        "vector_tenant_leakage": result.vector_tenant_leakage,
        "citation_tenant_leakage": result.citation_tenant_leakage,
        "usage_tenant_leakage": result.usage_tenant_leakage,
        "budget_tenant_leakage": result.budget_tenant_leakage,
        "qdrant_requests_observed": result.qdrant_requests_observed,
        "demo_retrieval_used": result.demo_retrieval_used,
        "demo_corpus_absent": ok("demo_corpus_absent"),
        "unique_nonce_verified": ok("unique_nonce_verified"),
        "citations_verified": ok("citations_verified"),
        "citation_context_mismatch": result.citation_context_mismatch,
        "usage_records_written": result.usage_records_written,
        "duplicate_usage_records": result.duplicate_usage_records,
        "zero_result_abstained": ok("zero_result_abstained"),
        "zero_result_provider_calls": result.zero_result_provider_calls,
        "zero_result_usage_records": result.zero_result_usage_records,
        "negative_control_abstained": ok("negative_control_abstained"),
        "qdrant_outage_fail_closed": ok("qdrant_outage_fail_closed"),
        "accounting_failure_suppressed_answer": ok("accounting_failure_suppressed_answer"),
        "hard_budget_limit_blocked_provider": ok("hard_budget_limit_blocked_provider"),
        "browser_vector_path_verified": ok("browser_vector_path_verified"),
        "routing_class": result.metrics.get("routing_class"),
        "provider_invocations": result.provider_invocations,
        "paid_provider_calls": result.paid_provider_calls,
        "cleanup_status": "PASS" if cleanup else "FAIL",
        "unrelated_data_unchanged": all(ok(name) for name in UNRELATED_CHECKS),
        "latencies_ms": result.latencies_ms,
        "metrics": {key: value for key, value in result.metrics.items() if key != "routing_class"},
        "checks": dict(sorted(result.checks.items())),
        "errors": result.errors,
    }


def nonce_proof(nonce: str) -> str:
    """Evidence carries a digest of a marker, never the marker itself."""
    return hashlib.sha256(nonce.encode()).hexdigest()


def available_memory_gib() -> float:
    for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
        if line.startswith("MemAvailable:"):
            return int(line.split()[1]) / 1024 / 1024
    return 0.0


# --------------------------------------------------------------------------- helpers


def _document(tenant_marker: str, nonce: str, team: str, weekday: str) -> bytes:
    """A synthetic multi-chunk document; the marker sits in the middle of the text."""
    filler = " ".join(
        f"Abschnitt {number}: Die Projektdokumentation beschreibt Ablauf {number} "
        "für Wartungsfenster, Freigaben und Eskalationen."
        for number in range(1, 16)
    )
    core = (
        f"Projektnotiz {tenant_marker}. Das Projekt mit dem Codenamen ORBIT wird von "
        f"Team {team} betreut. Die Wartungsfreigabe erfolgt immer {weekday}. "
        f"Referenz {nonce}."
    )
    return f"{filler} {core} {filler}".encode()


def _multipart(fields: dict[str, str], filename: str, content: bytes) -> tuple[bytes, str]:
    boundary = uuid.uuid4().hex
    parts = [
        f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode()
        for name, value in fields.items()
    ]
    parts.append(
        f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{filename}"\r\n'
        "Content-Type: text/plain\r\n\r\n".encode()
        + content
        + f"\r\n--{boundary}--\r\n".encode()
    )
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def _upload(env: e2e.Environment, label: str, title: str, name: str, content: bytes) -> Any:
    workspace, _, collection, _ = env.workspaces[env.users[label].tenant]
    body, content_type = _multipart(
        {
            "workspace_id": workspace,
            "collection_id": collection,
            "title": title,
            "logical_document_key": name,
        },
        name,
        content,
    )
    request = urllib.request.Request(  # noqa: S310 - gate-owned loopback API
        f"{env.api_url}/v1/documents/upload",
        data=body,
        method="POST",
        headers={"Content-Type": content_type, "Authorization": f"Bearer {env.token(label)}"},
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:  # noqa: S310
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, None


def _wait_for_job(env: e2e.Environment, label: str, job_id: str) -> str:
    deadline = time.monotonic() + POLL_SECONDS
    status = "unknown"
    while time.monotonic() < deadline:
        code, payload = env.api(f"/v1/ingestion/jobs/{job_id}", label)
        status = str((payload or {}).get("status", "unknown")) if code == 200 else "unknown"
        if status in {"completed", "failed", "cancelled"}:
            return status
        time.sleep(0.5)
    return status


def _ledger(env: e2e.Environment) -> tuple[int, int]:
    words = env.ledger.read_text(encoding="utf-8").split()
    return words.count("invoke"), words.count("paid")


def _audit(env: e2e.Environment) -> list[dict[str, Any]]:
    path = env.work / "evidence" / "audit-events.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


class Store:
    """Direct read access to the gate database and Qdrant for independent verification."""

    def __init__(self, env: e2e.Environment) -> None:
        from qdrant_client import QdrantClient
        from sqlalchemy import create_engine

        self.env = env
        self.engine = create_engine(env.database_url, hide_parameters=True)
        self.qdrant = QdrantClient(url=env.qdrant_url, timeout=10)

    def close(self) -> None:
        self.engine.dispose()
        self.qdrant.close()

    def points(self, version_id: str) -> list[Any]:
        from qdrant_client.http import models

        selector = models.Filter(
            must=[
                models.FieldCondition(
                    key="document_version_id", match=models.MatchValue(value=version_id)
                )
            ]
        )
        found, _ = self.qdrant.scroll(
            self.env.collection,
            scroll_filter=selector,
            limit=1000,
            with_payload=True,
            with_vectors=True,
        )
        return list(found)

    def point(self, chunk_id: str) -> Any | None:
        from qdrant_client.http import models

        selector = models.Filter(
            must=[models.FieldCondition(key="chunk_id", match=models.MatchValue(value=chunk_id))]
        )
        found, _ = self.qdrant.scroll(self.env.collection, scroll_filter=selector, limit=2)
        return found[0] if len(found) == 1 else None

    def rows(self, sql: str, **params: Any) -> list[Any]:
        from sqlalchemy import text

        with self.engine.connect() as connection:
            return list(connection.execute(text(sql), params))

    def execute(self, sql: str, **params: Any) -> None:
        from sqlalchemy import text

        with self.engine.begin() as connection:
            connection.execute(text(sql), params)


@dataclass
class Tenancy:
    label: str
    tenant: str
    nonce: str
    title: str
    document_id: str = ""
    version_id: str = ""
    budget_id: str = ""
    chunk_ids: set[str] = field(default_factory=set)


# --------------------------------------------------------------------------- scenario stages


def provision_accounting(env: e2e.Environment, store: Store, tenancies: list[Tenancy]) -> None:
    """Users, the deterministic model and a budget per tenant, as an operator would."""
    from ragops.persistence.database import transaction
    from ragops.persistence.models import (
        ModelConfiguration,
        ProviderConfiguration,
        TenantBudget,
        User,
    )

    issuer = f"{e2e.KEYCLOAK_URL}/realms/{e2e.REALM}"
    today = datetime.now(UTC).date()
    start = today.replace(day=1)
    end = (start + timedelta(days=32)).replace(day=1)
    with transaction(store.engine) as session:
        for user in env.users.values():
            subject = env.admin.user_uuid(user.username)
            if not subject:
                raise PrerequisiteMissing("Keycloak gate user has no subject")
            session.add(User(tenant_id=user.tenant, issuer=issuer, subject=subject))
        for tenancy in tenancies:
            provider = ProviderConfiguration(
                tenant_id=tenancy.tenant, name=MODEL[0], kind="deterministic"
            )
            session.add(provider)
            session.flush()
            session.add(
                ModelConfiguration(
                    tenant_id=tenancy.tenant,
                    provider_id=provider.id,
                    name=MODEL[1],
                    input_price=PRICES[0],
                    output_price=PRICES[1],
                )
            )
            budget = TenantBudget(
                tenant_id=tenancy.tenant,
                period_start=start,
                period_end=end,
                budget_amount=Decimal("100"),
                hard_limit_threshold=Decimal("1"),
                enforcement_mode="hard_limit",
            )
            session.add(budget)
            session.flush()
            tenancy.budget_id = str(budget.id)


def ask(env: e2e.Environment, label: str, **extra: Any) -> tuple[int, Any, float, int, int]:
    """One /v1/query with Qdrant bytes and provider invocations observed around it."""
    proxy = env.qdrant_proxy
    assert proxy is not None
    before_bytes, before_calls = proxy.upstream_bytes, _ledger(env)[0]
    started = time.perf_counter()
    status, payload, _ = e2e.http_json(
        f"{env.api_url}/v1/query",
        "POST",
        {"question": extra.pop("question", QUESTION), "top_k": TOP_K, **extra.pop("body", {})},
        token=env.token(label),
        headers=extra.pop("headers", None),
        timeout=30,
    )
    elapsed = round((time.perf_counter() - started) * 1000, 1)
    return (
        status,
        payload,
        elapsed,
        proxy.upstream_bytes - before_bytes,
        _ledger(env)[0] - before_calls,
    )


def zero_result(env: e2e.Environment, store: Store, result: RagQueryResult, label: str) -> None:
    """Before any upload a tenant has no context: abstain, no provider, no accounting."""
    status, body, _, _, calls = ask(env, label)
    usage = store.rows("SELECT count(*) FROM usage_records")[0][0]
    holds = store.rows("SELECT count(*) FROM budget_reservations")[0][0]
    result.zero_result_provider_calls = calls
    result.zero_result_usage_records = int(usage)
    result.check(
        "zero_result_abstained",
        status == 200
        and bool(body)
        and body["abstained"] is True
        and body["citations"] == []
        and str(body["answer"]).startswith(NO_CONTEXT)
        and calls == 0
        and usage == 0
        and holds == 0,
    )


def ingest(env: e2e.Environment, store: Store, result: RagQueryResult, tenancy: Tenancy) -> None:
    content = _document(
        tenancy.tenant,
        tenancy.nonce,
        *(
            ("Nordlicht", "dienstags")
            if tenancy.tenant == e2e.TENANT_A
            else ("Suedwind", "donnerstags")
        ),
    )
    status, accepted = _upload(env, tenancy.label, tenancy.title, f"{tenancy.title}.txt", content)
    result.check("real_api_upload_used", status == 202 and bool(accepted))
    if status != 202 or not accepted:
        raise RuntimeError(f"upload for {tenancy.tenant} was not accepted ({status})")
    tenancy.document_id, tenancy.version_id = accepted["document_id"], accepted["version_id"]
    persisted = store.rows(
        "SELECT count(*) FROM document_versions WHERE tenant_id = :t AND id = :v",
        t=tenancy.tenant,
        v=tenancy.version_id,
    )[0][0]
    final = _wait_for_job(env, tenancy.label, accepted["job_id"])
    result.check("worker_used", persisted == 1 and final == "completed")
    points = store.points(tenancy.version_id)
    tenancy.chunk_ids = {str(point.payload["chunk_id"]) for point in points}
    placeholders = [
        point
        for point in points
        if not any(point.vector or [])
        or not point.payload.get("chunk_text")
        or not point.payload.get("document_id")
    ]
    result.placeholder_vectors_remaining += len(placeholders)
    result.check(
        "real_chunks_written",
        len(points) >= 2
        and all(
            point.payload["tenant_id"] == tenancy.tenant
            and point.payload["document_id"] == tenancy.document_id
            and point.payload["chunk_id"] == f"{tenancy.version_id}:{point.payload['chunk_index']}"
            for point in points
        ),
    )
    result.check(
        "embedding_model_validated",
        bool(points)
        and all(
            point.payload.get("embedding_model") == "deterministic-hash-v1" for point in points
        ),
    )
    result.check(
        "unique_nonce_verified",
        sum(tenancy.nonce in str(point.payload.get("chunk_text", "")) for point in points) >= 1,
    )
    result.metrics.setdefault("chunks_written", {})[tenancy.tenant] = len(points)


def verify_answer(
    env: e2e.Environment,
    store: Store,
    result: RagQueryResult,
    tenancy: Tenancy,
    other: Tenancy,
    *,
    override: bool = False,
) -> None:
    extra: dict[str, Any] = {}
    if override:
        extra = {
            "body": {"tenant_id": other.tenant, "user_id": "x", "role": "admin"},
            "headers": {"X-Tenant-ID": other.tenant},
        }
    status, body, elapsed, qdrant_bytes, calls = ask(env, tenancy.label, **extra)
    result.latencies_ms.setdefault(f"query_{tenancy.tenant}", elapsed)
    answered = status == 200 and bool(body) and body.get("abstained") is False
    result.check("productive_query_answered", answered and bool(body.get("citations")))
    if qdrant_bytes > 0:
        result.qdrant_requests_observed += 1
    result.check("productive_query_uses_qdrant", qdrant_bytes > 0)
    if not answered:
        return
    citations = body["citations"]
    correlation = str(body["correlation_id"])
    nonce_hits = 0
    for citation in citations:
        if citation.get("tenant_id") != tenancy.tenant:
            result.citation_tenant_leakage += 1
        if (
            citation.get("document_id") == other.document_id
            or citation.get("chunk_id") in other.chunk_ids
        ):
            result.tenant_leakage += 1
            result.vector_tenant_leakage += 1
        point = store.point(str(citation.get("chunk_id")))
        payload = point.payload if point is not None else {}
        matches = (
            point is not None
            and citation.get("chunk_id") in tenancy.chunk_ids
            and payload.get("tenant_id") == tenancy.tenant
            and citation.get("document_id") == payload.get("document_id") == tenancy.document_id
            and citation.get("document_version_id")
            == payload.get("document_version_id")
            == tenancy.version_id
            and citation.get("title") == payload.get("title")
            and citation.get("page_number") == payload.get("page_number")
            and isinstance(citation.get("score"), float)
        )
        result.citation_context_mismatch += int(not matches)
        nonce_hits += int(tenancy.nonce in str(payload.get("chunk_text", "")))
    result.check("citations_verified", bool(citations) and result.citation_context_mismatch == 0)
    result.check(
        "tenant_filter_enforced", all(c.get("tenant_id") == tenancy.tenant for c in citations)
    )
    if override:
        result.check(
            "request_tenant_override_ignored",
            all(c.get("tenant_id") == tenancy.tenant for c in citations),
        )
    events = [event for event in _audit(env) if event.get("correlation_id") == correlation]
    kinds = [str(event.get("event_type")) for event in events]
    query = next((event for event in events if event.get("event_type") == "query"), {})
    details = query.get("details", {})
    result.check(
        "productive_query_uses_vector_retriever",
        details.get("query_mode") == "vector" and details.get("retrieval_outcome") == "answered",
    )
    result.check(
        "productive_query_uses_context_builder", int(details.get("citation_count", 0)) >= 1
    )
    route = next((e for e in events if e.get("event_type") == "model_route_selected"), {})
    result.check(
        "productive_query_uses_model_router",
        route.get("details", {}).get("provider_id") == MODEL[0]
        and route.get("details", {}).get("model_id") == MODEL[1],
    )
    result.metrics["routing_class"] = route.get("details", {}).get("routing_class")
    order = ["model_route_selected", "budget_reserved", "usage_recorded", "reservation_finalized"]
    result.check(
        "reservation_before_provider",
        [kind for kind in kinds if kind in order] == order and calls == 1,
    )
    usage = store.rows(
        "SELECT tenant_id, provider_id, model_id_text, input_tokens, output_tokens, cost "
        "FROM usage_records WHERE correlation_id = :c",
        c=correlation,
    )
    holds = store.rows(
        "SELECT tenant_id, budget_id, status, final_amount FROM budget_reservations "
        "WHERE idempotency_key = :k",
        k=f"query:{correlation}",
    )
    result.duplicate_usage_records += max(len(usage) - 1, 0)
    result.usage_tenant_leakage += sum(row[0] != tenancy.tenant for row in usage)
    result.budget_tenant_leakage += sum(
        row[0] != tenancy.tenant or str(row[1]) != tenancy.budget_id for row in holds
    )
    accounted = (
        len(usage) == 1
        and (usage[0][1], usage[0][2]) == MODEL
        and usage[0][3] > 0
        and usage[0][4] > 0
        and Decimal(usage[0][5])
        == (Decimal(usage[0][3]) * PRICES[0] + Decimal(usage[0][4]) * PRICES[1]).quantize(
            Decimal("0.00000001")
        )
        and len(holds) == 1
        and holds[0][2] == "committed"
        and Decimal(holds[0][3]) == Decimal(usage[0][5])
    )
    result.usage_records_written += int(accounted)
    result.check("finops_enforced", accounted)
    if nonce_hits:
        result.metrics.setdefault("nonce_cited", []).append(tenancy.tenant)


def accounting_failure(
    env: e2e.Environment, store: Store, result: RagQueryResult, tenancy: Tenancy
) -> None:
    """A gate-owned trigger fails the usage insert after the deterministic provider answered."""
    store.execute(
        "CREATE FUNCTION rc_rag_fail() RETURNS trigger LANGUAGE plpgsql AS "
        "$$ BEGIN RAISE EXCEPTION 'rag_query injected accounting failure'; END $$"
    )
    store.execute(
        "CREATE TRIGGER rc_rag_fail BEFORE INSERT ON usage_records FOR EACH ROW "
        f"WHEN (NEW.tenant_id = '{tenancy.tenant}') EXECUTE FUNCTION rc_rag_fail()"
    )
    usage_before = store.rows("SELECT count(*) FROM usage_records")[0][0]
    holds_before = store.rows("SELECT count(*) FROM budget_reservations WHERE status = 'reserved'")[
        0
    ][0]
    try:
        status, body, _, _, calls = ask(env, tenancy.label)
    finally:
        store.execute("DROP TRIGGER rc_rag_fail ON usage_records")
        store.execute("DROP FUNCTION rc_rag_fail()")
    usage_after = store.rows("SELECT count(*) FROM usage_records")[0][0]
    holds_after = store.rows("SELECT count(*) FROM budget_reservations WHERE status = 'reserved'")[
        0
    ][0]
    log = (env.work / "logs" / "api.log").read_text(encoding="utf-8", errors="replace")
    result.check(
        "accounting_failure_suppressed_answer",
        status == 503
        and body == {"detail": "accounting unavailable"}
        and calls == 1
        and usage_after == usage_before
        and holds_after == holds_before + 1
        and "Ergebnis auf Basis" not in log
        and tenancy.nonce not in log,
    )


def hard_budget(
    env: e2e.Environment, store: Store, result: RagQueryResult, tenancy: Tenancy, other: Tenancy
) -> None:
    store.execute(
        "UPDATE tenant_budgets SET budget_amount = 0.00000001 WHERE tenant_id = :t",
        t=tenancy.tenant,
    )
    usage_before = store.rows("SELECT count(*) FROM usage_records")[0][0]
    other_before = store.rows(
        "SELECT count(*), coalesce(sum(estimated_amount), 0) FROM budget_reservations "
        "WHERE tenant_id = :t",
        t=other.tenant,
    )
    status, body, _, _, calls = ask(env, tenancy.label)
    other_after = store.rows(
        "SELECT count(*), coalesce(sum(estimated_amount), 0) FROM budget_reservations "
        "WHERE tenant_id = :t",
        t=other.tenant,
    )
    result.check(
        "hard_budget_limit_blocked_provider",
        status == 429
        and body == {"detail": "budget_limit_exceeded"}
        and calls == 0
        and store.rows("SELECT count(*) FROM usage_records")[0][0] == usage_before
        and list(other_before[0]) == list(other_after[0]),
    )


def negative_controls(
    env: e2e.Environment, store: Store, result: RagQueryResult, tenancy: Tenancy
) -> None:
    """Without the tenant's vectors the same question abstains; without Qdrant it fails."""
    from qdrant_client.http import models

    usage_before = store.rows("SELECT count(*) FROM usage_records")[0][0]
    store.qdrant.delete(
        env.collection,
        points_selector=models.FilterSelector(
            filter=models.Filter(
                must=[
                    models.FieldCondition(
                        key="tenant_id", match=models.MatchValue(value=tenancy.tenant)
                    )
                ]
            )
        ),
        wait=True,
    )
    status, body, _, qdrant_bytes, calls = ask(env, tenancy.label)
    result.check(
        "negative_control_abstained",
        status == 200
        and bool(body)
        and body["abstained"] is True
        and body["citations"] == []
        and calls == 0
        and qdrant_bytes > 0
        and store.rows("SELECT count(*) FROM usage_records")[0][0] == usage_before,
    )
    proxy = env.qdrant_proxy
    assert proxy is not None
    proxy.set_mode("refuse")
    try:
        status, body, _, _, calls = ask(env, tenancy.label)
    finally:
        proxy.set_mode("forward")
    result.check(
        "qdrant_outage_fail_closed",
        status == 503 and body == {"detail": "retrieval unavailable"} and calls == 0,
    )


def set_top_k(page: Any, steps_below_default: int) -> None:
    """Lower the dashboard's "Maximale Quellen" slider (default 5) with the keyboard."""
    sidebar = e2e.open_sidebar(page)
    sidebar.get_by_text("Retrieval-Einstellungen", exact=True).click()
    slider = sidebar.get_by_role("slider")
    slider.focus()
    for _ in range(steps_below_default):
        slider.press("ArrowLeft")
    page.wait_for_timeout(500)


def browser_journey(
    env: e2e.Environment, result: RagQueryResult, run_dir: Path, a: Tenancy, b: Tenancy
) -> None:
    """Login, upload, indexing and a cited answer on the vector path in a real browser."""
    from playwright.sync_api import sync_playwright

    memory = available_memory_gib()
    result.metrics["memory_before_browser_gib"] = round(memory, 2)
    if memory < MIN_MEMORY_GIB:
        raise PrerequisiteMissing(f"less than {MIN_MEMORY_GIB} GiB available before the browser")
    title = f"RAG Vektor {env.run_id}"
    nonce = f"ENT114-C-{secrets.token_hex(8)}"
    content = (
        _document(a.tenant, nonce, "Nordlicht", "dienstags").decode()
        + " Im Projekt KOMET gilt die Wartungsregel Fenster-Sieben."
    ).encode()
    with sync_playwright() as playwright:
        session = e2e.BrowserSession(playwright, result, run_dir)
        try:
            context, page = session.context(e2e.DESKTOP)
            e2e.login(session, page, env, a.label)
            e2e.navigate(page, "Wissensbasis")
            e2e.upload_via_ui(
                page,
                title=title,
                name=f"komet-{env.run_id}.txt",
                content=content,
                mime="text/plain",
            )
            states = e2e.observe_job(page, title, "completed")
            e2e.navigate(page, "Copilot")
            # Two sources keep one document in the SIMPLE class of the deterministic model.
            set_top_k(page, 3)
            e2e.ask(page, BROWSER_QUESTION)
            shown = e2e.source_titles(page)
            session.screenshot(page, "rag-vector-answer")
            context.close()
            context_b, page_b = session.context(e2e.DESKTOP)
            e2e.login(session, page_b, env, b.label)
            e2e.navigate(page_b, "Copilot")
            set_top_k(page_b, 3)
            e2e.ask(page_b, BROWSER_QUESTION)
            shown_b = e2e.source_titles(page_b)
            leaked = sum(1 for item in shown_b if item != b.title)
            context_b.close()
        finally:
            session.close()
    result.tenant_leakage += leaked
    result.check(
        "browser_vector_path_verified",
        "completed" in states and title in shown and set(shown) <= {title} and leaked == 0,
    )


def run(result: RagQueryResult, evidence: dict[str, Any]) -> tuple[str, ...]:
    """Build the environment, run every stage, and always clean up through the environment."""
    try:
        import playwright  # noqa: F401
        import qdrant_client  # noqa: F401
    except ImportError as exc:
        raise PrerequisiteMissing("playwright and qdrant-client are required") from exc
    run_dir = e2e.ARTIFACTS.parent / "rag-query" / str(evidence["run_id"])
    run_dir.mkdir(parents=True, exist_ok=True)
    a = Tenancy(
        "a-admin", e2e.TENANT_A, f"ENT114-A-{secrets.token_hex(8)}", f"RAG A {evidence['run_id']}"
    )
    b = Tenancy(
        "b-admin", e2e.TENANT_B, f"ENT114-B-{secrets.token_hex(8)}", f"RAG B {evidence['run_id']}"
    )
    found: list[str] = [a.nonce, b.nonce]
    with e2e.environment(
        result, evidence, query_mode="vector", observe_qdrant=True, idp_down_dashboard=False
    ) as env:
        found += [env.client_secret, *(user.password for user in env.users.values())]
        result.check(
            "demo_corpus_absent",
            not any(path.is_file() for path in (env.work / "data").rglob("*.md")),
        )
        store = Store(env)
        try:
            stages: list[tuple[str, Callable[[], None]]] = [
                ("provision", lambda: provision_accounting(env, store, [a, b])),
                ("zero_result", lambda: zero_result(env, store, result, b.label)),
                ("ingest_a", lambda: ingest(env, store, result, a)),
                ("ingest_b", lambda: ingest(env, store, result, b)),
                ("query_a", lambda: verify_answer(env, store, result, a, b)),
                ("query_b", lambda: verify_answer(env, store, result, b, a)),
                (
                    "query_a_override",
                    lambda: verify_answer(env, store, result, a, b, override=True),
                ),
                ("negative_controls", lambda: negative_controls(env, store, result, a)),
                ("browser", lambda: browser_journey(env, result, run_dir, a, b)),
                ("accounting_failure", lambda: accounting_failure(env, store, result, b)),
                ("hard_budget", lambda: hard_budget(env, store, result, b, a)),
            ]
            for name, stage in stages:
                result.metrics["last_stage"] = name
                stage()
        finally:
            store.close()
        result.demo_retrieval_used = any(
            event.get("details", {}).get("query_mode") not in (None, "vector")
            for event in _audit(env)
            if event.get("event_type") == "query"
        )
    hits = e2e.scan_artifacts(run_dir, found)
    for name in hits:
        (run_dir / name).unlink(missing_ok=True)
    result.check("no_sensitive_artifacts", not hits)
    return tuple(found)


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
    markers = re.compile(r"ENT114-[A-C]-[0-9a-f]{8,}")
    if (
        leaks(serialized, tuple(value for value in secrets if value))
        or e2e.SECRET_PATTERNS.search(serialized)
        or markers.search(serialized)
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
    evidence: dict[str, Any] = {"gate": "rag_query", "tested_commit": _commit(), "run_id": run_id}
    memory = available_memory_gib()
    evidence["memory_available_gib"] = round(memory, 2)
    if memory < MIN_MEMORY_GIB:
        return _finish(
            evidence, "BLOCKED", f"less than {MIN_MEMORY_GIB} GiB of memory available", 2
        )
    result = RagQueryResult()
    found: tuple[str, ...] = ()
    try:
        found = run(result, evidence) or ()
    except PrerequisiteMissing as exc:
        return _finish(evidence, "BLOCKED", e2e._sanitize(str(exc)), 2)
    except Exception as exc:  # noqa: BLE001 - any stage failure is a gate failure
        result.errors.append(f"{result.metrics.get('last_stage', 'setup')}: {e2e._first_line(exc)}")
    evidence.update(build_evidence(result))
    status, reason = classify(result)
    if result.errors and status == "PASS":
        status, reason = "FAIL", "a gate stage raised an error"
    return _finish(evidence, status, reason, 0 if status == "PASS" else 1, found)


if __name__ == "__main__":
    raise SystemExit(main())
