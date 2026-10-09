"""Productive /v1/query with Idempotency-Key: one logical execution per key, fail closed."""

from __future__ import annotations

import hashlib
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import jwt
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text, update
from sqlalchemy.orm import Session
from tests.security.finops_helpers import exhaust_budget, reservations, usage_rows
from tests.security.oidc_helpers import AUDIENCE, ISSUER
from tests.security.test_query_finops import DIMENSION, QUESTION, TEXT, Harness

from ragops.api.app import create_app
from ragops.auth.identity import AuthenticatedUserContext
from ragops.config.settings import Settings
from ragops.modeling import usage as usage_module
from ragops.modeling.router import ProviderError, ProviderErrorCategory
from ragops.persistence.database import transaction
from ragops.persistence.models import QueryIdempotencyRecord, TenantBudget, User
from ragops.workflows.query_idempotency import IdempotencyScope, KeyDigest, request_fingerprint

KEY = "client-retry-0001"
REPLAYED = "Idempotency-Replayed"
HMAC_KEY = "api-test-hmac-secret-0123456789-abcdefghijkl"
DIGEST = KeyDigest(HMAC_KEY)
SCOPE_A = IdempotencyScope.for_query(
    AuthenticatedUserContext(
        user_id="user-tenant-a",
        tenant_id="tenant-a",
        roles=("viewer",),
        issuer=ISSUER,
        subject="user-tenant-a",
    ),
    KEY,
    DIGEST,
)


def token(private_key: str, tenant: str, subject: str | None = None) -> dict[str, str]:
    claims = {
        "iss": ISSUER,
        "aud": AUDIENCE,
        "sub": subject or f"user-{tenant}",
        "tenant_id": tenant,
        "roles": ["viewer"],
        "exp": datetime.now(UTC) + timedelta(minutes=5),
    }
    return {"Authorization": f"Bearer {jwt.encode(claims, private_key, algorithm='RS256')}"}


class IdempotentHarness(Harness):
    def send(
        self,
        tenant: str = "tenant-a",
        key: str | None = KEY,
        body: dict | None = None,
        subject: str | None = None,
        client: TestClient | None = None,
    ):
        headers = token(self.keys[0], tenant, subject)
        if key is not None:
            headers["Idempotency-Key"] = key
        return (client or self.client).post(
            "/v1/query", json={"question": QUESTION, "top_k": 3, **(body or {})}, headers=headers
        )

    def records(self, tenant: str | None = None) -> list[QueryIdempotencyRecord]:
        with Session(self.engine) as session:
            query = select(QueryIdempotencyRecord)
            if tenant is not None:
                query = query.where(QueryIdempotencyRecord.tenant_id == tenant)
            return list(session.scalars(query))

    def fault(self, monkeypatch, category: ProviderErrorCategory) -> None:
        def failing(question, citations, context):
            self.provider.calls += 1
            self.events.append("provider")
            raise ProviderError(category)

        monkeypatch.setattr(self.provider, "generate", failing)

    def audit_types(self) -> list[str]:
        return [event["event_type"] for event in self.audit()]


@pytest.fixture
def harness(keys, tmp_path, monkeypatch) -> IdempotentHarness:
    return IdempotentHarness(keys, tmp_path, monkeypatch, hmac_key=HMAC_KEY)


def assert_no_raw_values(harness: IdempotentHarness, *values: str) -> None:
    raw = (harness.evidence / "audit-events.jsonl").read_text()
    for value in (KEY, QUESTION, TEXT["tenant-a"], *values):
        assert value not in raw


# --------------------------------------------------------------------------- API contract


def test_a_valid_key_is_accepted_and_stored_only_as_digests(harness) -> None:
    harness.provision("tenant-a")
    response = harness.send()
    assert response.status_code == 200 and REPLAYED not in response.headers
    (record,) = harness.records()
    assert record.state == "succeeded" and record.status_code == 200
    assert record.key_hash == SCOPE_A.key_hash == DIGEST(KEY)
    assert record.key_hash != hashlib.sha256(KEY.encode()).hexdigest()
    assert str(record.correlation_id) == response.json()["correlation_id"]
    assert record.request_fingerprint == request_fingerprint(QUESTION, 3)
    assert KEY not in json.dumps(record.response)


@pytest.mark.parametrize("key", ["", "x" * 129, "with space", "semi;colon", "a,b"])
def test_a_malformed_key_is_rejected_before_any_work(harness, key) -> None:
    harness.provision("tenant-a")
    response = harness.send(key=key)
    assert response.status_code == 400 and response.json() == {"detail": "invalid idempotency key"}
    assert harness.provider.calls == 0 and harness.records() == []
    assert usage_rows(harness.engine) == [] and reservations(harness.engine) == []


def test_without_a_key_the_ent114_behaviour_is_unchanged(harness) -> None:
    harness.provision("tenant-a")
    first, second = harness.send(key=None), harness.send(key=None)
    assert first.status_code == second.status_code == 200
    assert first.json()["correlation_id"] != second.json()["correlation_id"]
    assert harness.provider.calls == 2 and len(usage_rows(harness.engine)) == 2
    assert harness.records() == []


# --------------------------------------------------------------------------- replay


def test_a_successful_retry_replays_without_any_side_effect(harness) -> None:
    harness.provision("tenant-a")
    first = harness.send()
    events = list(harness.events)
    second = harness.send()
    assert first.status_code == second.status_code == 200
    assert second.headers[REPLAYED] == "true"
    assert second.json() == first.json()
    assert events == ["reserve", "provider", "usage", "commit"] and harness.events == events
    assert harness.provider.calls == 1
    (row,) = usage_rows(harness.engine)
    (held,) = reservations(harness.engine)
    assert str(row.correlation_id) == second.json()["correlation_id"]
    assert held.idempotency_key == f"query:{first.json()['correlation_id']}"
    replayed = [e for e in harness.audit() if e["event_type"] == "query_idempotency_replayed"]
    assert len(replayed) == 1 and replayed[0]["correlation_id"] == first.json()["correlation_id"]
    assert_no_raw_values(harness)


def test_an_equivalent_request_is_the_same_logical_query(harness) -> None:
    harness.provision("tenant-a")
    harness.send()
    again = harness.send(body={"question": f"  {QUESTION}  "})
    assert again.status_code == 200 and again.headers[REPLAYED] == "true"
    assert harness.provider.calls == 1


# --------------------------------------------------------------------------- conflict


@pytest.mark.parametrize(
    "change", [{"question": "Welche Regel gilt im Projekt KOMET?"}, {"top_k": 4}]
)
def test_same_key_with_a_different_request_conflicts_before_side_effects(harness, change) -> None:
    harness.provision("tenant-a")
    harness.send()
    events = list(harness.events)
    conflict = harness.send(body=change)
    assert conflict.status_code == 409
    assert conflict.json() == {"detail": "idempotency key reused with a different request"}
    assert harness.events == events and harness.provider.calls == 1
    assert len(usage_rows(harness.engine)) == 1 and len(reservations(harness.engine)) == 1
    assert "query_idempotency_conflict" in harness.audit_types()
    assert_no_raw_values(harness, "KOMET")


# --------------------------------------------------------------------------- tenant / user scope


def test_the_same_key_in_two_tenants_executes_independently(harness) -> None:
    harness.provision("tenant-a")
    harness.provision("tenant-b")
    a = harness.send("tenant-a")
    b = harness.send("tenant-b")
    assert a.status_code == b.status_code == 200 and REPLAYED not in b.headers
    assert {c["tenant_id"] for c in a.json()["citations"]} == {"tenant-a"}
    assert {c["tenant_id"] for c in b.json()["citations"]} == {"tenant-b"}
    assert a.json()["correlation_id"] != b.json()["correlation_id"]
    assert harness.provider.calls == 2
    assert len(usage_rows(harness.engine, "tenant-a")) == len(
        usage_rows(harness.engine, "tenant-b")
    )
    assert len(harness.records("tenant-a")) == len(harness.records("tenant-b")) == 1
    replay_a = harness.send("tenant-a")
    assert replay_a.json() == a.json() and "tenant-b" not in replay_a.text


def test_tenant_b_cannot_learn_that_tenant_a_used_a_key(harness) -> None:
    harness.provision("tenant-a")
    harness.provision("tenant-b")
    harness.send("tenant-a")
    other = harness.send("tenant-b", body={"question": "Was verlangt das Orbit-Verfahren?"})
    assert other.status_code == 200 and REPLAYED not in other.headers  # no conflict either


def test_two_users_of_one_tenant_are_independent(harness) -> None:
    harness.provision("tenant-a")
    with transaction(harness.engine) as session:
        session.add(User(tenant_id="tenant-a", issuer=ISSUER, subject="second-user"))
    first = harness.send(subject="user-tenant-a")
    second = harness.send(subject="second-user")
    assert first.status_code == second.status_code == 200 and REPLAYED not in second.headers
    assert first.json()["correlation_id"] != second.json()["correlation_id"]
    assert harness.provider.calls == 2 and len(harness.records("tenant-a")) == 2


# --------------------------------------------------------------------------- failure semantics


def test_a_zero_result_retry_stays_free_of_provider_and_accounting(harness) -> None:
    harness.provision("tenant-c")
    first = harness.send("tenant-c")
    second = harness.send("tenant-c")
    assert first.status_code == second.status_code == 200
    assert first.json()["abstained"] is True and second.headers[REPLAYED] == "true"
    assert harness.events == [] and harness.provider.calls == 0
    assert usage_rows(harness.engine) == [] and reservations(harness.engine) == []


def test_a_hard_budget_rejection_is_safely_retryable(harness) -> None:
    ids = harness.provision("tenant-a", budget=Decimal("1"))
    exhaust_budget(harness.engine, "tenant-a", ids, Decimal("1"))
    first = harness.send()
    assert first.status_code == 429 and first.json() == {"detail": "budget_limit_exceeded"}
    (record,) = harness.records()
    assert record.state == "retryable"
    assert harness.send().status_code == 429 and harness.provider.calls == 0
    with transaction(harness.engine) as session:
        session.execute(
            update(TenantBudget)
            .where(TenantBudget.tenant_id == "tenant-a")
            .values(budget_amount=Decimal("100"))
        )
    third = harness.send()
    assert third.status_code == 200 and REPLAYED not in third.headers
    assert third.json()["correlation_id"] == str(record.correlation_id)
    assert harness.provider.calls == 1 and len(reservations(harness.engine)) == 1
    assert harness.send().headers[REPLAYED] == "true" and harness.provider.calls == 1


def test_an_accounting_failure_is_never_retried_automatically(harness, monkeypatch) -> None:
    harness.provision("tenant-a")

    def broken(*args, **kwargs):
        raise RuntimeError("database write failed")

    monkeypatch.setattr(usage_module.UsageService, "record", broken)
    first = harness.send()
    assert first.status_code == 503 and first.json() == {"detail": "accounting unavailable"}
    monkeypatch.undo()  # even a healthy database must not lead to a second provider call
    second = harness.send()
    assert second.status_code == 409
    assert second.json() == {"detail": "idempotent request requires reconciliation"}
    assert "Orbit" not in second.text and harness.provider.calls == 1
    assert usage_rows(harness.engine) == []
    (held,) = reservations(harness.engine)
    assert held.status == "reserved"
    (record,) = harness.records()
    assert record.state == "ambiguous_held" and record.response is None
    assert "query_idempotency_ambiguous_held" in harness.audit_types()


@pytest.mark.parametrize(
    "category", [ProviderErrorCategory.TIMEOUT, ProviderErrorCategory.PROVIDER_ERROR]
)
def test_an_ambiguous_provider_failure_is_never_retried(harness, monkeypatch, category) -> None:
    harness.provision("tenant-a")
    harness.fault(monkeypatch, category)
    first = harness.send()
    assert first.status_code == 503 and first.json() == {"detail": "generation unavailable"}
    for _ in range(3):
        retry = harness.send()
        assert retry.status_code == 409
        assert retry.json() == {"detail": "idempotent request requires reconciliation"}
    assert harness.provider.calls == 1 and usage_rows(harness.engine) == []
    (held,) = reservations(harness.engine)
    assert held.status == "reserved"


def test_a_non_billed_provider_failure_is_replayed_as_a_stable_failure(
    harness, monkeypatch
) -> None:
    harness.provision("tenant-a")
    harness.fault(monkeypatch, ProviderErrorCategory.RATE_LIMIT)
    first = harness.send()
    assert first.status_code == 503 and first.json() == {"detail": "generation unavailable"}
    second = harness.send()
    assert second.status_code == 503 and second.headers[REPLAYED] == "true"
    assert second.json() == first.json() and harness.provider.calls == 1
    (released,) = reservations(harness.engine)
    assert released.status == "released"


def test_a_request_in_progress_is_not_executed_twice(harness) -> None:
    harness.provision("tenant-a")
    store = harness.app.state.query_idempotency
    store.claim(SCOPE_A, request_fingerprint(QUESTION, 3))
    response = harness.send()
    assert response.status_code == 409 and response.headers["Retry-After"] == "1"
    assert response.json() == {"detail": "idempotent request in progress"}
    assert harness.provider.calls == 0 and reservations(harness.engine) == []


def test_an_idempotency_database_failure_fails_closed(harness) -> None:
    harness.provision("tenant-a")
    with harness.engine.begin() as connection:
        connection.execute(text("DROP TABLE query_idempotency_records"))
    response = harness.send()
    assert response.status_code == 503 and response.json() == {"detail": "idempotency unavailable"}
    assert harness.provider.calls == 0
    assert usage_rows(harness.engine) == [] and reservations(harness.engine) == []
    assert "query_idempotency_unavailable" in harness.audit_types()
    # Without the header the productive path keeps working.
    assert harness.send(key=None).status_code == 200


def test_a_key_without_a_durable_store_fails_closed(keys, tmp_path, monkeypatch) -> None:
    harness = IdempotentHarness(keys, tmp_path, monkeypatch)
    monkeypatch.delenv("RAGOPS_DATABASE_URL")
    app = create_app(
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
            query_idempotency_hmac_key=HMAC_KEY,
        ),
        vector_index=harness.app.state.vector_index,
        llm_provider=harness.provider,
    )
    assert app.state.query_idempotency is None
    response = harness.send(client=TestClient(app, raise_server_exceptions=False))
    assert response.status_code == 503 and response.json() == {"detail": "idempotency unavailable"}
    assert harness.provider.calls == 0


def test_a_key_without_a_configured_hmac_secret_fails_closed(keys, tmp_path, monkeypatch) -> None:
    harness = IdempotentHarness(keys, tmp_path, monkeypatch)  # no RAGOPS_IDEMPOTENCY_HMAC_KEY
    harness.provision("tenant-a")
    assert harness.app.state.query_idempotency is None
    response = harness.send()
    assert response.status_code == 503 and response.json() == {"detail": "idempotency unavailable"}
    assert harness.provider.calls == 0 and harness.records() == []
    assert usage_rows(harness.engine) == [] and reservations(harness.engine) == []
    assert harness.send(key=None).status_code == 200  # requests without a key are unaffected


def test_the_same_key_digest_differs_between_server_secrets(keys, tmp_path, monkeypatch) -> None:
    for name in ("one", "two"):
        (tmp_path / name).mkdir()
    first = IdempotentHarness(keys, tmp_path / "one", monkeypatch, hmac_key=HMAC_KEY)
    first.provision("tenant-a")
    assert first.send().status_code == 200
    other_key = "another-api-test-hmac-" + "secret-9876543210-zyx"
    second = IdempotentHarness(keys, tmp_path / "two", monkeypatch, hmac_key=other_key)
    second.provision("tenant-a")
    assert second.send().status_code == 200
    (one,), (two,) = first.records(), second.records()
    assert one.key_hash == DIGEST(KEY) and two.key_hash == KeyDigest(other_key)(KEY)
    assert one.key_hash != two.key_hash
    raw = (first.evidence / "audit-events.jsonl").read_text()
    assert HMAC_KEY not in raw and KEY not in raw


# --------------------------------------------------------------------------- concurrency


def test_concurrent_duplicates_execute_the_provider_at_most_once(harness, monkeypatch) -> None:
    harness.provision("tenant-a")
    original = harness.provider.generate
    gate = threading.Event()

    def slow(question, citations, context):
        gate.wait(timeout=5)
        return original(question, citations, context)

    monkeypatch.setattr(harness.provider, "generate", slow)
    with ThreadPoolExecutor(max_workers=10) as pool:
        futures = [pool.submit(harness.send) for _ in range(10)]
        time.sleep(0.5)
        gate.set()
        responses = [future.result() for future in futures]
    statuses = sorted(response.status_code for response in responses)
    assert statuses.count(200) >= 1 and set(statuses) <= {200, 409}
    for response in responses:
        if response.status_code == 409:
            assert response.json() == {"detail": "idempotent request in progress"}
    assert harness.provider.calls == 1
    assert len(usage_rows(harness.engine)) == 1 and len(reservations(harness.engine)) == 1
    assert len(harness.records()) == 1
    assert harness.send().headers[REPLAYED] == "true" and harness.provider.calls == 1
