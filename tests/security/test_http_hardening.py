"""HTTP hardening: security headers, early body limits and malformed input without 5xx."""

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
        data_dir=Path("data/synthetic"),
        evidence_dir=tmp_path / "evidence",
        max_request_bytes=4096,
    )
    return TestClient(create_app(settings), raise_server_exceptions=False)


def test_every_response_carries_security_headers(client) -> None:
    for response in (client.get("/health"), client.get("/v1/workspaces")):
        headers = response.headers
        assert headers["x-content-type-options"] == "nosniff"
        assert headers["x-frame-options"] == "DENY"
        assert headers["referrer-policy"] == "no-referrer"
        assert "frame-ancestors 'none'" in headers["content-security-policy"]
        assert headers["cache-control"] == "no-store"
    assert client.get("/v1/workspaces").status_code == 401


def test_oversized_body_is_rejected_before_parsing(client, keys) -> None:
    headers = bearer(keys[0], "tenant-alpha", ["admin"])
    response = client.post(
        "/v1/query", content=b'{"question": "' + b"x" * 8000 + b'"}', headers=headers
    )
    assert response.status_code == 413
    chunked = client.post(
        "/v1/query",
        content=(chunk for chunk in [b'{"question": "', b"x" * 8000, b'"}']),
        headers=headers,
    )
    assert chunked.status_code == 413
    invalid = client.post("/v1/query", content=b"{}", headers={**headers, "content-length": "x"})
    assert invalid.status_code == 400


@pytest.mark.parametrize(
    "payload",
    [
        b'{"retrieved_chunks": Infinity}',
        b'{"estimated_input_tokens": -Infinity}',
        b'{"source_count": NaN}',
        b'{"intent": ' + b"[" * 3000 + b"]" * 3000 + b"}",
    ],
)
def test_malformed_numbers_never_cause_server_errors(client, keys, payload) -> None:
    headers = {**bearer(keys[0], "tenant-alpha", ["admin"]), "content-type": "application/json"}
    response = client.post("/v1/admin/model-router/simulate", content=payload, headers=headers)
    assert response.status_code < 500


@pytest.mark.parametrize(
    "payload",
    [
        {"budget_amount": "abc"},
        {"budget_amount": "NaN"},
        {"budget_amount": "Infinity"},
        {"budget_amount": -1},
        {"budget_amount": 10, "enforcement_mode": "disable_everything"},
    ],
)
def test_budget_input_is_validated(client, keys, payload) -> None:
    headers = bearer(keys[0], "tenant-alpha", ["admin"])
    assert client.post("/v1/admin/budgets", json=payload, headers=headers).status_code == 422
    created = client.post("/v1/admin/budgets", json={"budget_amount": 10}, headers=headers)
    assert created.status_code == 201
    patched = client.patch(
        f"/v1/admin/budgets/{created.json()['id']}", json=payload, headers=headers
    )
    assert patched.status_code == 422
