"""Privacy egress through the HTTP API (ENT-13.1): 409, idempotent retry, forged fields,
read-only administration. Real tokens, real catalog administration, real OpenAI adapter
class with a counting fake SDK client; synthetic data only."""

from __future__ import annotations

import json
import secrets
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest

pytest.importorskip("alembic", reason="Install declared persistence extra; product gate blocks")

from fastapi.testclient import TestClient
from tests.security.finops_helpers import migrated_sqlite, provision
from tests.security.oidc_helpers import AUDIENCE, ISSUER, bearer
from tests.security.privacy_helpers import synthetic_grant

from ragops.api.app import create_app
from ragops.config.settings import Settings
from ragops.llm.providers import OpenAIProvider
from ragops.vector.embedding import EMBEDDING_MODEL, DeterministicEmbeddingProvider
from ragops.vector.index import DeterministicVectorIndex, VectorPayload

MODEL = "gpt-synthetic"
TEXT = "Schuldnerin Erika Musterfrau, Az. 7 M 88/26, offene Forderung 512,40 EUR."
QUESTION = "Welche Forderung gegen Erika Musterfrau ist offen?"
SECRETS = ("Musterfrau", "88/26", "512,40")


class FakeSDK:
    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.responses = SimpleNamespace(create=self._create)

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(
            output_text="Antwort [doc-a-v1:0]",
            model=MODEL,
            usage=SimpleNamespace(input_tokens=30, output_tokens=6, total_tokens=36),
        )


def chunk(tenant: str, document: str) -> VectorPayload:
    now = datetime.now(UTC)
    return VectorPayload(
        tenant, "w", "c", document, f"{document}-v1", f"{document}-v1:0", "internal",
        "indexed", "indexed", f"hash-{document}", 0, f"{document}.pdf", now.isoformat(),
        (now - timedelta(minutes=1)).isoformat(), None, "Akte", 1, TEXT, EMBEDDING_MODEL,
    )  # fmt: skip


@pytest.fixture
def api(keys, tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "fake-not-a-key")
    monkeypatch.setenv("OPENAI_MODEL", MODEL)
    engine, url = migrated_sqlite(tmp_path / "api.db")
    monkeypatch.setenv("RAGOPS_DATABASE_URL", url)
    monkeypatch.setenv("RAGOPS_ENV", "test")  # SQLite is accepted only in tests
    for tenant in ("tenant-a", "tenant-b"):
        provision(
            engine,
            tenant,
            issuer=ISSUER,
            subject=f"user-{tenant}",
            models=(("openai", MODEL, Decimal("0.00001"), Decimal("0.00002")),),
            kinds={"openai": "openai"},
        )
    embedding = DeterministicEmbeddingProvider(64)
    index = DeterministicVectorIndex(64)
    index.upsert_chunks(
        [
            (embedding.embed([TEXT])[0], chunk("tenant-a", "doc-a")),
            (embedding.embed([TEXT])[0], chunk("tenant-b", "doc-b")),
        ]
    )
    sdk = FakeSDK()
    settings = Settings(
        environment="test",
        identity_provider="oidc",
        oidc_issuer=ISSUER,
        oidc_audience=AUDIENCE,
        oidc_public_key=keys[1],
        evidence_dir=tmp_path,
        vector_provider="qdrant",
        qdrant_url="http://127.0.0.1:9",
        embedding_dimension=64,
        platform_admin_tenant_id="tenant-a",
        query_idempotency_hmac_key=secrets.token_hex(32),
    )
    client = TestClient(
        create_app(settings, vector_index=index, llm_provider=OpenAIProvider(client=sdk)),
        raise_server_exceptions=False,
    )
    admin = bearer(keys[0], "tenant-a", ["admin"])
    assert (
        client.post(
            "/v1/admin/models", json={"provider_id": "openai", "model_id": MODEL}, headers=admin
        ).status_code
        == 201
    )
    for provider, model, enabled in (
        ("openai", MODEL, True),
        ("deterministic", "deterministic-ragops-v1", False),
    ):
        response = client.patch(
            f"/v1/admin/models/{provider}/{model}", json={"enabled": enabled}, headers=admin
        )
        assert response.status_code == 200
    return SimpleNamespace(client=client, sdk=sdk, engine=engine, keys=keys, path=tmp_path)


def ask(api, tenant="tenant-a", body=None, headers=None):
    return api.client.post(
        "/v1/query",
        json={"question": QUESTION, "top_k": 3, **(body or {})},
        headers={**bearer(api.keys[0], tenant, ["viewer"]), **(headers or {})},
    )


def audit_text(api) -> str:
    path = api.path / "audit-events.jsonl"
    return path.read_text(encoding="utf-8") if path.exists() else ""


def test_without_a_policy_the_api_refuses_with_409_and_sends_nothing(api) -> None:
    response = ask(api)
    assert response.status_code == 409
    assert response.json()["detail"] == "external processing not authorized"
    assert api.sdk.calls == []
    log = audit_text(api)
    assert "privacy_egress_denied" in log and not any(s in log for s in SECRETS)
    assert not any(s in response.text for s in SECRETS)


def test_a_granted_tenant_is_answered_and_the_other_tenant_is_not(api) -> None:
    synthetic_grant(api.engine, "tenant-a", "openai", MODEL)
    assert ask(api).status_code == 200
    assert ask(api, "tenant-b").status_code == 409
    assert len(api.sdk.calls) == 1 and api.sdk.calls[0]["store"] is False


def test_a_denied_keyed_request_can_be_retried_after_a_grant(api) -> None:
    key = {"Idempotency-Key": "privacy-retry-0001"}
    assert ask(api, headers=key).status_code == 409
    assert api.sdk.calls == []
    synthetic_grant(api.engine, "tenant-a", "openai", MODEL)
    retried = ask(api, headers=key)
    assert retried.status_code == 200 and len(api.sdk.calls) == 1
    replayed = ask(api, headers=key)  # a replay is served from the record, not the provider
    assert replayed.status_code == 200 and len(api.sdk.calls) == 1


@pytest.mark.parametrize(
    "forged",
    [
        {"tenant_id": "tenant-b"},
        {"privacy_approved": True},
        {"data_classes": ["synthetic_probe"]},
        {"provider": "openai", "model": MODEL},
    ],
)
def test_client_supplied_tenant_or_approval_fields_never_authorize(api, forged) -> None:
    synthetic_grant(api.engine, "tenant-b", "openai", MODEL)  # only another tenant is approved
    response = ask(api, body=forged)
    assert response.status_code in {409, 422}
    assert api.sdk.calls == []


def test_policies_are_readable_only_by_admins_of_the_same_tenant(api) -> None:
    synthetic_grant(api.engine, "tenant-a", "openai", MODEL)
    ask(api)
    own = api.client.get(
        "/v1/admin/privacy/egress-policies", headers=bearer(api.keys[0], "tenant-a", ["admin"])
    )
    assert own.status_code == 200
    body = own.json()
    assert [p["provider_id"] for p in body["policies"]] == ["openai"]
    assert {d["decision"] for d in body["recent_decisions"]} == {"allow"}
    assert not any(s in json.dumps(body) for s in SECRETS)
    other = api.client.get(
        "/v1/admin/privacy/egress-policies", headers=bearer(api.keys[0], "tenant-b", ["admin"])
    )
    assert other.status_code == 200 and other.json() == {"policies": [], "recent_decisions": []}
    viewer = api.client.get(
        "/v1/admin/privacy/egress-policies", headers=bearer(api.keys[0], "tenant-a", ["viewer"])
    )
    assert viewer.status_code == 403


def test_there_is_no_http_write_path_for_privacy_policies(api) -> None:
    admin = bearer(api.keys[0], "tenant-a", ["admin"])
    for method in ("post", "put", "patch", "delete"):
        response = getattr(api.client, method)("/v1/admin/privacy/egress-policies", headers=admin)
        assert response.status_code == 405
    routes = {getattr(route, "path", "") for route in api.client.app.routes}
    assert {r for r in routes if "privacy" in r} == {"/v1/admin/privacy/egress-policies"}


def test_a_misconfigured_policy_store_denies_even_with_a_grant(api, monkeypatch) -> None:
    """A database the runtime refuses to open (here SQLite outside tests) is a deny."""
    synthetic_grant(api.engine, "tenant-a", "openai", MODEL)
    monkeypatch.setenv("RAGOPS_ENV", "local")
    response = ask(api)
    assert response.status_code in {409, 503}
    assert api.sdk.calls == []
