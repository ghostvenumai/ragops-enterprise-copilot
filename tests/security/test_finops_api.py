"""FinOps admin simulation uses the shared Decimal decision, through real OIDC validation."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from tests.security.oidc_helpers import AUDIENCE, ISSUER, bearer

from ragops.api.app import create_app
from ragops.config.settings import Settings


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
    )
    return TestClient(create_app(settings), raise_server_exceptions=False)


@pytest.mark.parametrize(
    ("estimated", "budget", "decision", "state", "spend"),
    [
        ("0.9", "1", "ALLOW_WITH_WARNING", "WARNING", "0.9"),
        (8.5, 10, "ALLOW_WITH_WARNING", "WARNING", "8.5"),
        ("7.99999999", "10", "ALLOW", "NORMAL", "7.99999999"),
        ("11.99", "10", "ROUTE_CHEAPER", "SOFT_LIMIT", "11.99"),
        ("12", "10", "DENY_BUDGET_LIMIT", "HARD_LIMIT", "12"),
    ],
)
def test_simulation_matches_preflight_decision(
    client, keys, estimated, budget, decision, state, spend
) -> None:
    response = client.post(
        "/v1/admin/finops/policy/simulate",
        json={"estimated_cost": estimated, "budget_amount": budget},
        headers=bearer(keys[0], "tenant-alpha", ["admin"]),
    )
    assert response.status_code == 200
    body = response.json()
    assert (body["decision"], body["state"], body["estimated_post_request_spend"]) == (
        decision,
        state,
        spend,
    )
    assert body["reason_codes"][0] == "SIMULATION_ONLY"


@pytest.mark.parametrize(
    "payload",
    [
        {"estimated_cost": -1, "budget_amount": 10},
        {"estimated_cost": "NaN", "budget_amount": 10},
        {"estimated_cost": "Infinity", "budget_amount": 10},
        {"estimated_cost": True, "budget_amount": 10},
        {"estimated_cost": "ten", "budget_amount": 10},
        {"estimated_cost": 1, "budget_amount": 0},
    ],
)
def test_simulation_rejects_invalid_money(client, keys, payload) -> None:
    response = client.post(
        "/v1/admin/finops/policy/simulate",
        json=payload,
        headers=bearer(keys[0], "tenant-alpha", ["admin"]),
    )
    assert response.status_code == 422
