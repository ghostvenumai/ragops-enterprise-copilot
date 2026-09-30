"""The shared model catalog is a platform resource: tenant admins cannot change it."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from tests.security.oidc_helpers import AUDIENCE, ISSUER, bearer

from ragops.api.app import create_app
from ragops.config.settings import Settings

MODEL = "/v1/admin/models/deterministic/deterministic-ragops-v1"


def make_client(keys: tuple[str, str], tmp_path: Path, platform: str | None) -> TestClient:
    settings = Settings(
        environment="test",
        identity_provider="oidc",
        oidc_issuer=ISSUER,
        oidc_audience=AUDIENCE,
        oidc_public_key=keys[1],
        data_dir=tmp_path / "data",
        evidence_dir=tmp_path / "evidence",
        platform_admin_tenant_id=platform,
    )
    return TestClient(create_app(settings), raise_server_exceptions=False)


def mutations(client: TestClient, headers: dict[str, str]) -> list[int]:
    return [
        client.post("/v1/admin/providers", json={"provider_id": "p"}, headers=headers).status_code,
        client.patch("/v1/admin/providers/deterministic", json={}, headers=headers).status_code,
        client.post(
            "/v1/admin/models", json={"provider_id": "p", "model_id": "m"}, headers=headers
        ).status_code,
        client.patch(MODEL, json={"enabled": False}, headers=headers).status_code,
    ]


def test_tenant_admin_cannot_change_the_shared_catalog(keys, tmp_path) -> None:
    client = make_client(keys, tmp_path, "platform")
    beta = bearer(keys[0], "tenant-beta", ["admin"])
    assert mutations(client, beta) == [403, 403, 403, 403]
    platform = bearer(keys[0], "platform", ["admin"])
    catalog = client.get("/v1/admin/models", headers=platform).json()
    assert catalog[0]["enabled"] is True  # nothing changed for any tenant
    assert client.get("/v1/admin/models", headers=beta).status_code == 200  # read stays open


def test_platform_admin_can_change_the_catalog(keys, tmp_path) -> None:
    client = make_client(keys, tmp_path, "platform")
    platform = bearer(keys[0], "platform", ["admin"])
    assert mutations(client, platform) == [201, 200, 201, 200]
    viewer = bearer(keys[0], "platform", ["viewer"])
    assert mutations(client, viewer) == [403, 403, 403, 403]


@pytest.mark.parametrize("platform", [None, ""])
def test_catalog_changes_fail_closed_without_a_platform_tenant(keys, tmp_path, platform) -> None:
    client = make_client(keys, tmp_path, platform)
    assert mutations(client, bearer(keys[0], "tenant-alpha", ["admin"])) == [403, 403, 403, 403]
