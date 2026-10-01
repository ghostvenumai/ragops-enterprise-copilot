"""PostgreSQL: the productive query reserves, books usage and fails closed on accounting errors.

Skipped unless RAGOPS_TEST_DATABASE_URL points at an isolated ragops_test_* database. The test
creates and drops its own ragops_test_ent113_<run> database and touches nothing else.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from uuid import uuid4

import pytest

pytest.importorskip("alembic", reason="Install declared persistence extra; product gate blocks")

from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine, make_url
from tests.security.finops_helpers import provision, reservations, usage_rows
from tests.security.oidc_helpers import AUDIENCE, ISSUER, bearer
from tests.security.test_query_finops import DIMENSION, QUESTION, TEXT, payload

from ragops.api.app import create_app
from ragops.config.settings import Settings
from ragops.llm.providers import DeterministicTestProvider
from ragops.modeling import usage as usage_module
from ragops.vector.embedding import DeterministicEmbeddingProvider
from ragops.vector.index import DeterministicVectorIndex

pytestmark = pytest.mark.integration
BASE = os.getenv("RAGOPS_TEST_DATABASE_URL", "")


@pytest.fixture(scope="module")
def keys() -> tuple[str, str]:
    """Ephemeral RS256 key pair for the synthetic issuer."""
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


@pytest.fixture
def database(monkeypatch) -> Iterator[Engine]:
    if not BASE.startswith("postgresql+psycopg://"):
        pytest.skip("RAGOPS_TEST_DATABASE_URL is not an isolated PostgreSQL test database")
    base = make_url(BASE)
    if not (base.database or "").startswith("ragops_test_"):
        pytest.skip("RAGOPS_TEST_DATABASE_URL must name a ragops_test_* database")
    name = f"ragops_test_ent113_{uuid4().hex[:10]}"
    admin = create_engine(base, isolation_level="AUTOCOMMIT")
    with admin.connect() as connection:
        connection.execute(text(f'CREATE DATABASE "{name}"'))
    url = base.set(database=name)
    engine = create_engine(url)
    try:
        with engine.begin() as connection:
            cfg = Config("alembic.ini")
            cfg.attributes["connection"] = connection
            command.upgrade(cfg, "head")
        monkeypatch.setenv("RAGOPS_DATABASE_URL", url.render_as_string(hide_password=False))
        yield engine
    finally:
        engine.dispose()
        with admin.connect() as connection:
            connection.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
            remaining = connection.execute(
                text("SELECT count(*) FROM pg_database WHERE datname = :n"), {"n": name}
            ).scalar()
        admin.dispose()
        assert remaining == 0


def client(keys, tmp_path, provider) -> TestClient:
    embedding = DeterministicEmbeddingProvider(DIMENSION)
    index = DeterministicVectorIndex(DIMENSION)
    index.upsert_chunks([(embedding.embed([TEXT["tenant-a"]])[0], payload("tenant-a"))])
    settings = Settings(
        environment="test",
        identity_provider="oidc",
        oidc_issuer=ISSUER,
        oidc_audience=AUDIENCE,
        oidc_public_key=keys[1],
        evidence_dir=tmp_path / "evidence",
        vector_provider="qdrant",
        qdrant_url="http://127.0.0.1:9",
        embedding_dimension=DIMENSION,
    )
    app = create_app(settings, vector_index=index, llm_provider=provider)
    return TestClient(app, raise_server_exceptions=False)


class Counting(DeterministicTestProvider):
    calls = 0

    def generate(self, question, citations, context):
        Counting.calls += 1
        return super().generate(question, citations, context)


def test_query_reserves_books_usage_and_finalizes_on_postgres(database, keys, tmp_path) -> None:
    ids = provision(database, "tenant-a", issuer=ISSUER, subject="user-tenant-a")
    Counting.calls = 0
    response = client(keys, tmp_path, Counting()).post(
        "/v1/query", json={"question": QUESTION}, headers=bearer(keys[0], "tenant-a", ["viewer"])
    )
    assert response.status_code == 200 and response.json()["abstained"] is False
    assert Counting.calls == 1
    (row,) = usage_rows(database)
    assert (row.tenant_id, row.user_id, str(row.correlation_id)) == (
        "tenant-a",
        ids["user"],
        response.json()["correlation_id"],
    )
    assert row.cost == row.actual_cost and row.cost > 0
    (held,) = reservations(database)
    assert (held.status, held.final_amount, held.budget_id) == (
        "committed",
        row.cost,
        ids["budget"],
    )


def test_accounting_failure_on_postgres_suppresses_the_answer(
    database, keys, tmp_path, monkeypatch
) -> None:
    provision(database, "tenant-a", issuer=ISSUER, subject="user-tenant-a")
    original = usage_module.UsageService.record

    def record_then_fail(self, *args, **kwargs):
        original(self, *args, **kwargs)  # the row is written, then the transaction fails
        raise RuntimeError("injected failure after the usage insert")

    monkeypatch.setattr(usage_module.UsageService, "record", record_then_fail)
    Counting.calls = 0
    response = client(keys, tmp_path, Counting()).post(
        "/v1/query", json={"question": QUESTION}, headers=bearer(keys[0], "tenant-a", ["viewer"])
    )
    assert response.status_code == 503 and response.json() == {"detail": "accounting unavailable"}
    assert "Orbit" not in response.text and Counting.calls == 1
    assert usage_rows(database) == []  # rolled back with the failed transaction
    (held,) = reservations(database)
    assert held.status == "reserved" and held.final_amount is None
