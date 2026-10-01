"""Productive /v1/query: model router, budget reservation and tenant-scoped usage, fail closed."""

from __future__ import annotations

import ast
import inspect
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from tests.security.finops_helpers import (
    DETERMINISTIC_MODEL,
    INPUT_PRICE,
    OUTPUT_PRICE,
    exhaust_budget,
    migrated_sqlite,
    provision,
    reservations,
    usage_rows,
)
from tests.security.oidc_helpers import AUDIENCE, ISSUER, bearer

from ragops.api.app import create_app
from ragops.config.settings import Settings
from ragops.finops import reservation as reservation_module
from ragops.llm.providers import DeterministicTestProvider, LLMResponse, LLMUsage
from ragops.modeling import usage as usage_module
from ragops.modeling.router import TenantModelPolicy
from ragops.vector.embedding import EMBEDDING_MODEL, DeterministicEmbeddingProvider
from ragops.vector.index import DeterministicVectorIndex, VectorPayload
from ragops.workflows import vector_query

ROOT = Path(__file__).resolve().parents[2]
DIMENSION = 64
TEXT = {
    "tenant-a": "Das Orbit-Verfahren von Tenant A regelt die Freigabe von Wartungen am Wochenende.",
    "tenant-b": "Das Orbit-Verfahren von Tenant B verlangt eine Freigabe durch zwei Leitungen.",
}
QUESTION = "Was regelt das Orbit-Verfahren zur Freigabe von Wartungen?"


def payload(tenant: str) -> VectorPayload:
    now = datetime.now(UTC)
    return VectorPayload(
        tenant_id=tenant,
        workspace_id="w",
        collection_id="c",
        document_id=f"doc-{tenant}",
        document_version_id=f"doc-{tenant}-v1",
        chunk_id=f"doc-{tenant}-v1:0",
        access_level="internal",
        document_status="indexed",
        version_status="indexed",
        content_hash=f"hash-{tenant}",
        chunk_index=0,
        source_name="orbit.txt",
        created_at=now.isoformat(),
        valid_from=(now - timedelta(minutes=1)).isoformat(),
        title=f"Orbit {tenant}",
        chunk_text=TEXT[tenant],
        embedding_model=EMBEDDING_MODEL,
    )


class CountingProvider(DeterministicTestProvider):
    def __init__(self, events: list[str], *, usage_reported: bool = True) -> None:
        self.events, self.calls, self.usage_reported = events, 0, usage_reported

    def generate(self, question, citations, context):
        self.calls += 1
        self.events.append("provider")
        response = super().generate(question, citations, context)
        if self.usage_reported:
            return response
        usage = LLMUsage(0, 0, 0, 0.0, response.usage.latency_ms, self.model, reported=False)
        return LLMResponse(response.text, usage, response.abstained, response.used_source_ids)


class Harness:
    def __init__(self, keys, tmp_path: Path, monkeypatch, **options) -> None:
        self.engine, url = migrated_sqlite(tmp_path / "finops.db")
        monkeypatch.setenv("RAGOPS_DATABASE_URL", url)
        monkeypatch.setenv("RAGOPS_ENV", "test")
        self.events: list[str] = []
        self.keys, self.evidence = keys, tmp_path / "evidence"
        embedding = DeterministicEmbeddingProvider(DIMENSION)
        index = DeterministicVectorIndex(DIMENSION)
        index.upsert_chunks(
            [(embedding.embed([TEXT[t]])[0], payload(t)) for t in ("tenant-a", "tenant-b")]
        )
        self.provider = CountingProvider(self.events, usage_reported=options.pop("usage", True))
        settings = Settings(
            environment="test",
            identity_provider="oidc",
            oidc_issuer=ISSUER,
            oidc_audience=AUDIENCE,
            oidc_public_key=keys[1],
            evidence_dir=self.evidence,
            vector_provider="qdrant",
            qdrant_url="http://127.0.0.1:9",
            embedding_dimension=DIMENSION,
        )
        self.app = create_app(settings, vector_index=index, llm_provider=self.provider)
        self.client = TestClient(self.app, raise_server_exceptions=False)
        for owner, name, label in (
            (reservation_module.BudgetReservationService, "reserve", "reserve"),
            (reservation_module.BudgetReservationService, "commit", "commit"),
            (reservation_module.BudgetReservationService, "release", "release"),
            (usage_module.UsageService, "record", "usage"),
        ):
            monkeypatch.setattr(owner, name, self._spy(label, getattr(owner, name)))

    def _spy(self, name, target):
        def wrapper(*args, **kwargs):
            self.events.append(name)
            return target(*args, **kwargs)

        return wrapper

    def provision(self, tenant: str, **options):
        return provision(self.engine, tenant, issuer=ISSUER, subject=f"user-{tenant}", **options)

    def ask(self, tenant: str = "tenant-a", body: dict | None = None, **kwargs):
        return self.client.post(
            "/v1/query",
            json={"question": QUESTION, "top_k": 5, **(body or {})},
            headers={**bearer(self.keys[0], tenant, ["viewer"]), **kwargs.pop("headers", {})},
            **kwargs,
        )

    def audit(self) -> list[dict]:
        path = self.evidence / "audit-events.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


@pytest.fixture
def harness(keys, tmp_path, monkeypatch) -> Harness:
    return Harness(keys, tmp_path, monkeypatch)


# --------------------------------------------------------------------------- A zero result


def test_zero_results_neither_route_nor_reserve_nor_bill(harness) -> None:
    harness.provision("tenant-c")
    response = harness.ask("tenant-c")
    assert response.status_code == 200 and response.json()["abstained"] is True
    assert harness.events == [] and harness.provider.calls == 0
    assert usage_rows(harness.engine) == [] and reservations(harness.engine) == []
    assert not [e for e in harness.audit() if e["event_type"] == "model_route_selected"]


# --------------------------------------------------------------------------- B router required


def test_the_query_workflow_can_reach_a_provider_only_through_the_router() -> None:
    parameters = inspect.signature(vector_query.VectorQueryService).parameters
    assert "provider" not in parameters and "router_factory" in parameters
    tree = ast.parse((ROOT / "src/ragops/workflows/vector_query.py").read_text(encoding="utf-8"))
    generate_calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "generate"
    ]
    assert len(generate_calls) == 1
    invokes = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "invoke"
    ]
    assert len(invokes) == 1 and generate_calls[0] in list(ast.walk(invokes[0]))
    routed = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "execute_with_fallback"
    ]
    assert len(routed) == 1


# --------------------------------------------------------------------------- C/M tenant


def test_routing_reservation_and_usage_use_only_the_token_tenant(harness) -> None:
    ids_a = harness.provision("tenant-a")
    ids_b = harness.provision("tenant-b")
    response = harness.ask(
        body={"tenant_id": "tenant-b", "user_id": "x", "role": "admin"},
        headers={"X-Tenant-ID": "tenant-b"},
        params={"tenant_id": "tenant-b"},
    )
    assert response.status_code == 200
    (row,) = usage_rows(harness.engine)
    assert (row.tenant_id, row.user_id) == ("tenant-a", ids_a["user"])
    assert row.model_id == ids_a[f"model:deterministic:{DETERMINISTIC_MODEL}"]
    (held,) = reservations(harness.engine)
    assert (held.tenant_id, held.budget_id, held.status) == (
        "tenant-a",
        ids_a["budget"],
        "committed",
    )
    assert (
        usage_rows(harness.engine, "tenant-b") == []
        and reservations(harness.engine, "tenant-b") == []
    )
    assert ids_b["budget"] != held.budget_id


def test_router_policy_lookup_uses_the_authoritative_tenant(keys, tmp_path, monkeypatch) -> None:
    harness = Harness(keys, tmp_path, monkeypatch)
    harness.provision("tenant-a")
    harness.provision("tenant-b")
    seen: list[str] = []
    original = vector_query.VectorQueryService._route

    def spy(self, tenant_id, *rest):
        seen.append(tenant_id)
        return original(self, tenant_id, *rest)

    monkeypatch.setattr(vector_query.VectorQueryService, "_route", spy)
    harness.ask("tenant-a", body={"tenant_id": "tenant-b"}, headers={"X-Tenant-ID": "tenant-b"})
    harness.ask("tenant-b")
    assert seen == ["tenant-a", "tenant-b"]


def test_a_denying_policy_of_another_tenant_does_not_apply(keys, tmp_path, monkeypatch) -> None:
    harness = Harness(keys, tmp_path, monkeypatch)
    harness.provision("tenant-a")
    harness.provision("tenant-b")
    policies = harness.app.state.model_policies
    policies["tenant-b"] = TenantModelPolicy("tenant-b", allowed_providers=frozenset({"none"}))
    assert harness.ask("tenant-a").status_code == 200
    denied = harness.ask("tenant-b")
    assert denied.status_code == 409 and denied.json() == {"detail": "no eligible model"}
    assert harness.provider.calls == 1 and len(usage_rows(harness.engine)) == 1


# --------------------------------------------------------------------------- D/O ordering


def test_reservation_precedes_the_provider_and_accounting_precedes_the_response(harness) -> None:
    harness.provision("tenant-a")
    response = harness.ask()
    assert response.status_code == 200 and response.json()["abstained"] is False
    assert harness.events == ["reserve", "provider", "usage", "commit"]
    event_types = [e["event_type"] for e in harness.audit()]
    order = [
        "model_route_selected",
        "budget_reserved",
        "usage_recorded",
        "reservation_finalized",
        "query",
    ]
    assert [t for t in event_types if t in order] == order


# --------------------------------------------------------------------------- E hard limit


def test_hard_budget_limit_rejects_before_the_provider(harness) -> None:
    ids = harness.provision("tenant-a", budget=Decimal("1"))
    exhaust_budget(harness.engine, "tenant-a", ids, Decimal("1"))
    response = harness.ask()
    assert response.status_code == 429
    assert response.json() == {"detail": "budget_limit_exceeded"}
    assert harness.provider.calls == 0 and "provider" not in harness.events
    assert len(usage_rows(harness.engine)) == 1  # only the pre-existing spend
    assert reservations(harness.engine) == []
    rejected = [e for e in harness.audit() if e["event_type"] == "budget_rejected"]
    assert len(rejected) == 1 and "1.0" not in json.dumps(rejected[0]["details"])


# --------------------------------------------------------------------------- F success


def test_a_successful_query_books_exactly_one_tenant_scoped_usage_record(harness) -> None:
    harness.provision("tenant-a")
    response = harness.ask()
    body = response.json()
    (row,) = usage_rows(harness.engine)
    assert str(row.correlation_id) == body["correlation_id"] == row.request_id
    assert (row.provider_id, row.model_id_text) == ("deterministic", DETERMINISTIC_MODEL)
    assert row.input_tokens > 0 and row.output_tokens > 0
    assert row.total_tokens == row.input_tokens + row.output_tokens
    expected = (
        Decimal(row.input_tokens) * INPUT_PRICE + Decimal(row.output_tokens) * OUTPUT_PRICE
    ).quantize(Decimal("0.00000001"))
    assert row.cost == row.actual_cost == expected > 0
    assert row.estimated_cost is not None and row.fallback_count == 0
    assert row.routing_class and row.endpoint == "rag" and row.workflow == "query"
    (held,) = reservations(harness.engine)
    assert held.status == "committed" and held.final_amount == expected
    assert held.estimated_amount >= expected
    assert held.idempotency_key == f"query:{body['correlation_id']}"
    assert body["metrics"]["tokens"] == row.total_tokens
    assert Decimal(str(body["metrics"]["estimated_cost_eur"])) == expected


# --------------------------------------------------------------------------- G accounting failure


def test_accounting_failure_after_provider_success_suppresses_the_answer(
    harness, monkeypatch, caplog
) -> None:
    harness.provision("tenant-a")

    def broken(*args, **kwargs):
        raise RuntimeError("database write failed")

    monkeypatch.setattr(usage_module.UsageService, "record", broken)
    response = harness.ask()
    assert response.status_code == 503
    assert response.json() == {"detail": "accounting unavailable"}
    assert "Orbit" not in response.text and "Ergebnis" not in response.text
    assert harness.provider.calls == 1
    assert usage_rows(harness.engine) == []
    (held,) = reservations(harness.engine)
    assert held.status == "reserved" and held.final_amount is None  # still consumes budget
    failed = [e for e in harness.audit() if e["event_type"] == "usage_recording_failed"]
    assert len(failed) == 1
    raw = (harness.evidence / "audit-events.jsonl").read_text()
    assert TEXT["tenant-a"] not in raw and "Ergebnis auf Basis" not in raw
    assert "Ergebnis auf Basis" not in caplog.text


# --------------------------------------------------------------------------- K missing usage


def test_missing_provider_usage_is_never_booked_as_zero(keys, tmp_path, monkeypatch) -> None:
    harness = Harness(keys, tmp_path, monkeypatch, usage=False)
    harness.provision("tenant-a")
    response = harness.ask()
    assert response.status_code == 503 and response.json() == {"detail": "accounting unavailable"}
    assert harness.provider.calls == 1 and usage_rows(harness.engine) == []
    (held,) = reservations(harness.engine)
    assert held.status == "reserved"


# --------------------------------------------------------------------------- L foreign keys


@pytest.mark.parametrize(
    "missing",
    [{"user": False}, {"models": ()}, {"budget": None}],
    ids=["user", "model", "budget"],
)
def test_missing_accounting_references_fail_closed_before_the_provider(harness, missing) -> None:
    harness.provision("tenant-a", **missing)
    response = harness.ask()
    assert response.status_code == 503 and response.json() == {
        "detail": "accounting not configured"
    }
    assert harness.provider.calls == 0
    assert usage_rows(harness.engine) == [] and reservations(harness.engine) == []


def test_another_tenants_user_is_never_used(harness) -> None:
    harness.provision("tenant-b")
    harness.provision("tenant-a", user=False)
    assert harness.ask("tenant-a").status_code == 503
    assert harness.provider.calls == 0 and usage_rows(harness.engine) == []


def test_a_paid_model_without_pricing_fails_closed(harness) -> None:
    harness.provision(
        "tenant-a",
        models=(("deterministic", DETERMINISTIC_MODEL, Decimal("0"), Decimal("0")),),
        kinds={"deterministic": "openai"},
    )
    response = harness.ask()
    assert response.status_code == 503 and response.json() == {
        "detail": "accounting not configured"
    }
    assert harness.provider.calls == 0


def test_without_a_database_the_vector_path_cannot_answer(keys, tmp_path, monkeypatch) -> None:
    harness = Harness(keys, tmp_path, monkeypatch)
    monkeypatch.delenv("RAGOPS_DATABASE_URL")
    harness.app = create_app(
        Settings(
            environment="test",
            identity_provider="oidc",
            oidc_issuer=ISSUER,
            oidc_audience=AUDIENCE,
            oidc_public_key=keys[1],
            evidence_dir=harness.evidence,
            vector_provider="qdrant",
            qdrant_url="http://127.0.0.1:9",
            embedding_dimension=DIMENSION,
        ),
        vector_index=harness.app.state.vector_index,
        llm_provider=harness.provider,
    )
    client = TestClient(harness.app, raise_server_exceptions=False)
    response = client.post(
        "/v1/query", json={"question": QUESTION}, headers=bearer(keys[0], "tenant-a", ["viewer"])
    )
    assert response.status_code == 503 and response.json() == {
        "detail": "accounting not configured"
    }
    assert harness.provider.calls == 0


# --------------------------------------------------------------------------- N precheck


def test_any_precheck_failure_means_zero_provider_calls(harness, monkeypatch) -> None:
    harness.provision("tenant-a")

    def unavailable(*args, **kwargs):
        raise reservation_module.ReservationError("budget not found or currency mismatch")

    monkeypatch.setattr(reservation_module.BudgetReservationService, "reserve", unavailable)
    response = harness.ask()
    assert response.status_code == 503 and harness.provider.calls == 0
    assert usage_rows(harness.engine) == []
