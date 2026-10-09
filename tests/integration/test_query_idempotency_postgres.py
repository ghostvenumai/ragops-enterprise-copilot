"""PostgreSQL: concurrent same-key queries claim one durable record and bill at most once.

Skipped unless RAGOPS_TEST_DATABASE_URL points at an isolated ragops_test_* database. The
database fixture creates and drops its own database and touches nothing else.
"""

from __future__ import annotations

import hashlib
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

pytest.importorskip("alembic", reason="Install declared persistence extra; product gate blocks")

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from tests.integration.test_query_finops_postgres import client, database, keys  # noqa: F401
from tests.security.finops_helpers import provision, reservations, usage_rows
from tests.security.oidc_helpers import ISSUER, bearer
from tests.security.test_query_finops import QUESTION

from ragops.llm.providers import DeterministicTestProvider
from ragops.persistence.database import transaction
from ragops.persistence.models import QueryIdempotencyRecord
from ragops.workflows.query_idempotency import KeyDigest

pytestmark = pytest.mark.integration
KEY = "pg-concurrency-key"
HMAC_KEY = "postgres-test-hmac-secret-0123456789-abcdef"


class Slow(DeterministicTestProvider):
    calls = 0
    lock = threading.Lock()
    release = threading.Event()

    def generate(self, question, citations, context):
        with Slow.lock:
            Slow.calls += 1
        Slow.release.wait(timeout=10)
        return super().generate(question, citations, context)


def test_ten_concurrent_duplicates_bill_at_most_once_on_postgres(database, keys, tmp_path) -> None:  # noqa: F811
    provision(database, "tenant-a", issuer=ISSUER, subject="user-tenant-a")
    Slow.calls, Slow.release = 0, threading.Event()
    api = client(keys, tmp_path, Slow(), HMAC_KEY)
    headers = {**bearer(keys[0], "tenant-a", ["viewer"]), "Idempotency-Key": KEY}

    def send():
        return api.post("/v1/query", json={"question": QUESTION, "top_k": 3}, headers=headers)

    with ThreadPoolExecutor(max_workers=10) as pool:
        futures = [pool.submit(send) for _ in range(10)]
        time.sleep(1.0)
        Slow.release.set()
        responses = [future.result() for future in futures]
    statuses = [response.status_code for response in responses]
    assert statuses.count(200) >= 1 and set(statuses) <= {200, 409}
    assert Slow.calls == 1
    assert len(usage_rows(database)) == 1 and len(reservations(database)) == 1
    with Session(database) as session:
        (record,) = session.scalars(select(QueryIdempotencyRecord))
        assert record.key_hash == KeyDigest(HMAC_KEY)(KEY)
        assert record.key_hash != hashlib.sha256(KEY.encode()).hexdigest()
        assert session.scalar(select(func.count()).select_from(QueryIdempotencyRecord)) == 1
    replay = send()
    assert replay.status_code == 200 and replay.headers["Idempotency-Replayed"] == "true"
    assert Slow.calls == 1 and len(usage_rows(database)) == 1


def test_postgres_enforces_the_scope_unique_constraint(database, keys, tmp_path) -> None:  # noqa: F811
    provision(database, "tenant-a", issuer=ISSUER, subject="user-tenant-a")
    Slow.calls, Slow.release = 0, threading.Event()
    Slow.release.set()
    api = client(keys, tmp_path, Slow(), HMAC_KEY)
    headers = {**bearer(keys[0], "tenant-a", ["viewer"]), "Idempotency-Key": KEY}
    assert api.post("/v1/query", json={"question": QUESTION}, headers=headers).status_code == 200
    with Session(database) as session:
        (row,) = session.scalars(select(QueryIdempotencyRecord))
        session.expunge(row)
    with pytest.raises(IntegrityError), transaction(database) as session:
        session.add(
            QueryIdempotencyRecord(
                tenant_id=row.tenant_id,
                principal=row.principal,
                endpoint=row.endpoint,
                key_hash=row.key_hash,
                request_fingerprint=row.request_fingerprint,
                correlation_id=row.correlation_id,
                state="claimed",
                attempt=1,
                updated_at=row.updated_at,
                lease_expires_at=row.lease_expires_at,
                expires_at=row.expires_at,
            )
        )
