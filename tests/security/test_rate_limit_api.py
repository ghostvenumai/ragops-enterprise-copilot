"""Rate limiting on the real FastAPI routes through OIDC validation and a counted provider stub."""

from __future__ import annotations

import socket
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from tests.security.oidc_helpers import AUDIENCE, ISSUER, bearer

from ragops.api.app import create_app
from ragops.config.settings import Settings
from ragops.llm.providers import DeterministicTestProvider


def _closed_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _client(keys, tmp_path: Path, **limits: object) -> TestClient:
    settings = Settings(
        environment="test",
        identity_provider="oidc",
        oidc_issuer=ISSUER,
        oidc_audience=AUDIENCE,
        oidc_public_key=keys[1],
        evidence_dir=tmp_path,
        **limits,  # type: ignore[arg-type]
    )
    return TestClient(create_app(settings), raise_server_exceptions=False)


@pytest.fixture
def provider_calls(monkeypatch) -> list[str]:
    calls: list[str] = []
    original = DeterministicTestProvider.generate

    def counted(self, question, citations, context):
        calls.append(question)
        return original(self, question, citations, context)

    monkeypatch.setattr(DeterministicTestProvider, "generate", counted)
    monkeypatch.setenv("RAGOPS_LLM_PROVIDER", "deterministic")
    return calls


def test_query_route_enforces_limit_before_provider(keys, tmp_path, provider_calls) -> None:
    client = _client(keys, tmp_path, rate_limit_requests=2, rate_limit_window_seconds=60)
    alpha = bearer(keys[0], "tenant-alpha", ["sales"])
    body = {"question": "Welche SLA gilt?"}
    first, second = (client.post("/v1/query", json=body, headers=alpha) for _ in range(2))
    assert (first.status_code, second.status_code) == (200, 200)
    assert (first.headers["RateLimit-Remaining"], second.headers["RateLimit-Remaining"]) == (
        "1",
        "0",
    )
    assert first.headers["RateLimit-Limit"] == "2"
    denied = client.post("/v1/query", json=body, headers=alpha)
    assert denied.status_code == 429
    assert 1 <= int(denied.headers["Retry-After"]) <= 60
    assert denied.headers["RateLimit-Remaining"] == "0"
    assert len(provider_calls) == 2
    spoofed_body = client.post(
        "/v1/query", json={**body, "tenant_id": "tenant-beta", "user_id": "x"}, headers=alpha
    )
    spoofed_header = client.post(
        "/v1/query", json=body, headers={**alpha, "X-Tenant-ID": "tenant-beta"}
    )
    assert (spoofed_body.status_code, spoofed_header.status_code) == (429, 429)
    assert (
        client.post(
            "/v1/query", json=body, headers=bearer(keys[0], "tenant-beta", ["sales"])
        ).status_code
        == 200
    )
    assert client.post("/v1/query", json=body).status_code == 401
    assert client.get("/health").status_code == 200
    assert len(provider_calls) == 3


def test_redis_outage_fails_closed_with_503(keys, tmp_path, provider_calls) -> None:
    client = _client(
        keys,
        tmp_path,
        rate_limit_backend="redis",
        rate_limit_redis_url=f"redis://127.0.0.1:{_closed_port()}/0",
        rate_limit_timeout_seconds=0.2,
    )
    response = client.post(
        "/v1/query",
        json={"question": "Welche SLA gilt?"},
        headers=bearer(keys[0], "tenant-alpha", ["sales"]),
    )
    assert response.status_code == 503
    assert response.headers["Retry-After"] == "1"
    assert provider_calls == []
    assert client.get("/health").status_code == 200


def test_every_authenticated_route_is_limited_and_exemptions_are_narrow(keys, tmp_path) -> None:
    app = _client(keys, tmp_path).app
    limited, exempt = set(), set()
    for route in app.routes:  # type: ignore[attr-defined]
        dependant = getattr(route, "dependant", None)
        if dependant is None or route.path.startswith(("/docs", "/redoc", "/openapi")):
            continue
        names = {dep.call.__qualname__ for dep in dependant.dependencies}
        (limited if any("limited" in name for name in names) else exempt).add(route.path)
    assert exempt == {"/health", "/ready", "/metrics"}
    assert "/v1/query" in limited and "/v1/admin/model-router/simulate" in limited
