"""Router admin API contracts through real OIDC JWT validation; no provider is invoked."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from tests.security.oidc_helpers import AUDIENCE, ISSUER, bearer

from ragops.api.app import create_app
from ragops.config.settings import Settings
from ragops.modeling.router import LLMModelRouter, ModelCatalog, ModelSpec


@pytest.fixture
def client(keys: tuple[str, str], tmp_path: Path) -> TestClient:
    settings = Settings(
        environment="test",
        identity_provider="oidc",
        oidc_issuer=ISSUER,
        oidc_audience=AUDIENCE,
        oidc_public_key=keys[1],
        data_dir=tmp_path / "data",
        evidence_dir=tmp_path / "evidence",
        # tenant-alpha operates the shared catalog in these contracts.
        platform_admin_tenant_id="tenant-alpha",
    )
    return TestClient(create_app(settings), raise_server_exceptions=False)


def simulate(client: TestClient, headers: dict[str, str], **signals: object):
    return client.post("/v1/admin/model-router/simulate", json=signals, headers=headers)


def test_no_eligible_model_is_a_precise_conflict_not_a_server_error(client, keys) -> None:
    response = simulate(client, bearer(keys[0], "tenant-beta", ["admin"]), source_count=3)
    assert response.status_code == 409
    assert response.json()["detail"].startswith("NO_ELIGIBLE_MODEL")


def test_admin_created_catalog_entries_are_not_routable_until_enabled(client, keys) -> None:
    alpha = bearer(keys[0], "tenant-alpha", ["admin"])
    beta = bearer(keys[0], "tenant-beta", ["admin"])
    before = simulate(client, beta).json()
    created = client.post("/v1/admin/providers", json={"provider_id": "acme"}, headers=alpha)
    assert created.status_code == 201 and created.json()["enabled"] is False
    model = client.post(
        "/v1/admin/models", json={"provider_id": "acme", "model_id": "x"}, headers=alpha
    )
    assert model.status_code == 201 and model.json()["enabled"] is False
    after = simulate(client, beta).json()
    assert (after["provider_id"], after["model_id"]) == (before["provider_id"], before["model_id"])


def test_tenant_policy_payload_is_applied_only_to_its_tenant(client, keys) -> None:
    alpha = bearer(keys[0], "tenant-alpha", ["admin"])
    beta = bearer(keys[0], "tenant-beta", ["admin"])
    pinned = client.put(
        "/v1/admin/model-policies/tenant-alpha",
        json={"allowed_models": ["not-in-catalog"], "fallback_enabled": False},
        headers=alpha,
    )
    assert pinned.status_code == 200
    policy = client.get("/v1/admin/model-policies", headers=alpha).json()
    assert policy == [
        {
            "tenant_id": "tenant-alpha",
            "allowed_providers": [],
            "allowed_models": ["not-in-catalog"],
            "high_risk_models": [],
            "default_tier": "STANDARD",
            "max_routing_tier": "COMPLEX",
            "cost_ceiling_eur": None,
            "fallback_enabled": False,
        }
    ]
    assert simulate(client, alpha).status_code == 409  # pinned to an unknown model: fail closed
    assert simulate(client, beta).status_code == 200  # tenant-beta keeps the default policy


@pytest.mark.parametrize(
    "payload",
    [
        {"unknown_field": True},
        {"max_routing_tier": "ULTRA"},
        {"allowed_models": "balanced"},
        {"allowed_models": [""]},
        {"cost_ceiling_eur": -1},
        {"cost_ceiling_eur": True},
        {"fallback_enabled": "yes"},
    ],
)
def test_invalid_policy_payload_is_rejected(client, keys, payload) -> None:
    alpha = bearer(keys[0], "tenant-alpha", ["admin"])
    response = client.put("/v1/admin/model-policies/tenant-alpha", json=payload, headers=alpha)
    assert response.status_code == 422
    assert client.get("/v1/admin/model-policies", headers=alpha).json() == []


def test_model_toggle_requires_boolean_and_round_trips(client, keys) -> None:
    alpha = bearer(keys[0], "tenant-alpha", ["admin"])
    path = "/v1/admin/models/deterministic/deterministic-ragops-v1"
    assert client.patch(path, json={"enabled": "false"}, headers=alpha).status_code == 422
    assert client.patch(path, json={}, headers=alpha).status_code == 422
    assert client.patch(path, json={"enabled": False}, headers=alpha).json()["enabled"] is False
    assert simulate(client, alpha).status_code == 409
    assert client.patch(path, json={"enabled": True}, headers=alpha).json()["enabled"] is True
    catalog = client.get("/v1/admin/models", headers=alpha).json()
    assert catalog == [
        {
            "provider_id": "deterministic",
            "model_id": "deterministic-ragops-v1",
            "display_name": "Demo deterministic",
            "enabled": True,
            "routing_tier": "SIMPLE",
            "high_risk_allowed": False,
        }
    ]


def test_non_admin_cannot_simulate_or_change_routing(client, keys) -> None:
    viewer = bearer(keys[0], "tenant-beta", ["sales"])
    assert simulate(client, viewer).status_code == 403
    assert (
        client.post("/v1/admin/providers", json={"provider_id": "x"}, headers=viewer).status_code
        == 403
    )
    assert (
        client.put("/v1/admin/model-policies/tenant-beta", json={}, headers=viewer).status_code
        == 403
    )


@pytest.mark.parametrize(("provider", "model"), [("", "m"), ("p", ""), ("  ", "m")])
def test_incomplete_catalog_entries_fail_closed(provider: str, model: str) -> None:
    with pytest.raises(ValueError):
        LLMModelRouter([ModelSpec(provider, model, "incomplete")])
    with pytest.raises(ValueError):
        ModelCatalog([ModelSpec(provider, model, "incomplete")])
