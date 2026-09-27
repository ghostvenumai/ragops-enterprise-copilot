"""Tenant-bound admin API regressions, exercised through real OIDC JWT validation."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from ragops.api.app import create_app
from ragops.config.settings import Settings

ISSUER = "https://issuer.synthetic.example/"
AUDIENCE = "ragops-api"


@pytest.fixture(scope="module")
def keys() -> tuple[str, str]:
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = private.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    public_pem = (
        private.public_key()
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        .decode()
    )
    return private_pem, public_pem


def bearer(private_key: str, tenant: str, roles: list[str]) -> dict[str, str]:
    claims = {
        "iss": ISSUER,
        "aud": AUDIENCE,
        "sub": f"user-{tenant}",
        "tenant_id": tenant,
        "roles": roles,
        "exp": datetime.now(UTC) + timedelta(minutes=5),
    }
    return {"Authorization": f"Bearer {jwt.encode(claims, private_key, algorithm='RS256')}"}


@pytest.fixture
def client(keys: tuple[str, str], tmp_path: Path) -> TestClient:
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    events = [
        {"event_id": f"evt-{tenant}", "tenant_id": tenant, "user_id": f"user-{tenant}"}
        for tenant in ("tenant-alpha", "tenant-beta", "tenant-alpha")
    ]
    (evidence / "audit-events.jsonl").write_text(
        "\n".join(json.dumps(event) for event in events) + "\n", encoding="utf-8"
    )
    settings = Settings(
        environment="test",
        identity_provider="oidc",
        oidc_issuer=ISSUER,
        oidc_audience=AUDIENCE,
        oidc_public_key=keys[1],
        data_dir=tmp_path / "data",
        evidence_dir=evidence,
    )
    return TestClient(create_app(settings))


def test_model_policy_listing_is_scoped_to_admin_tenant(client, keys) -> None:
    alpha = bearer(keys[0], "tenant-alpha", ["admin"])
    beta = bearer(keys[0], "tenant-beta", ["admin"])
    assert (
        client.put("/v1/admin/model-policies/tenant-alpha", json={}, headers=alpha).status_code
        == 200
    )
    assert (
        client.put("/v1/admin/model-policies/tenant-alpha", json={}, headers=beta).status_code
        == 404
    )
    assert client.get("/v1/admin/model-policies", headers=beta).json() == []
    assert [
        row["tenant_id"] for row in client.get("/v1/admin/model-policies", headers=alpha).json()
    ] == ["tenant-alpha"]


def test_audit_events_are_scoped_to_admin_tenant(client, keys) -> None:
    beta = client.get("/v1/audit-events", headers=bearer(keys[0], "tenant-beta", ["admin"]))
    alpha = client.get("/v1/audit-events", headers=bearer(keys[0], "tenant-alpha", ["admin"]))
    assert beta.status_code == alpha.status_code == 200
    assert [event["tenant_id"] for event in beta.json()] == ["tenant-beta"]
    assert [event["tenant_id"] for event in alpha.json()] == ["tenant-alpha", "tenant-alpha"]
    assert "tenant-alpha" not in beta.text


def test_admin_dependency_resolves_bearer_identity_over_http(client, keys) -> None:
    # Regression: the dependency previously surfaced as a required query parameter (422).
    admin = bearer(keys[0], "tenant-alpha", ["admin"])
    assert client.get("/v1/admin/models", headers=admin).status_code == 200
    assert client.get("/v1/admin/models").status_code == 401


def test_non_admin_cannot_read_admin_tenant_views(client, keys) -> None:
    viewer = bearer(keys[0], "tenant-beta", ["sales"])
    assert client.get("/v1/admin/model-policies", headers=viewer).status_code == 403
    assert client.get("/v1/audit-events", headers=viewer).status_code == 403
