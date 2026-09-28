"""/health stays live, /ready fails closed with sanitized reasons, outages map to 503."""

from __future__ import annotations

import json
import socket
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError

from ragops.api.app import create_app
from ragops.config.settings import Settings


def _closed_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _settings(tmp_path: Path, **overrides) -> Settings:
    return Settings(data_dir=Path("data/synthetic"), evidence_dir=tmp_path, **overrides)


def test_unconfigured_dependencies_do_not_block_readiness(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("RAGOPS_DATABASE_URL", raising=False)
    monkeypatch.delenv("RAGOPS_DATABASE_URL_FILE", raising=False)
    with TestClient(create_app(_settings(tmp_path))) as client:
        ready = client.get("/ready")
        assert ready.status_code == 200 and ready.json()["status"] == "ready"
        assert {c["status"] for c in ready.json()["components"].values()} == {"not_configured"}


def test_down_critical_dependencies_are_unready_but_live(tmp_path, monkeypatch) -> None:
    secret_url = f"postgresql+psycopg://rc:TopSecretPw1@127.0.0.1:{_closed_port()}/ragops"
    monkeypatch.setenv("RAGOPS_DATABASE_URL", secret_url)
    settings = _settings(
        tmp_path,
        async_ingestion_required=True,
        redis_url=f"redis://:RedisPw1@127.0.0.1:{_closed_port()}/0",
    )
    with TestClient(create_app(settings)) as client:  # startup never probes dependencies
        assert client.get("/health").json() == {"status": "ok"}
        ready = client.get("/ready")
        assert ready.status_code == 503 and ready.json()["status"] == "unready"
        components = ready.json()["components"]
        assert components["postgres"] == {
            "critical": True,
            "status": "down",
            "reason": "unreachable",
        }
        assert components["redis"]["reason"] == "unreachable"
        for secret in ("TopSecretPw1", "RedisPw1", "127.0.0.1", "psycopg", "redis://"):
            assert secret not in json.dumps(ready.json())
        metrics = client.get("/metrics")
        assert metrics.status_code == 200 and "ragops_ingestion_queue_up 0" in metrics.text


def test_shutdown_turns_the_instance_unready(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("RAGOPS_DATABASE_URL", raising=False)
    app = create_app(_settings(tmp_path))
    with TestClient(app) as client:
        assert client.get("/ready").status_code == 200
    assert app.state.readiness.shutting_down.is_set()
    status, payload = app.state.readiness.evaluate()
    assert status == 503 and payload["status"] == "shutting_down"


def test_database_outage_inside_a_request_is_a_detail_free_503(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("RAGOPS_DATABASE_URL", raising=False)
    app = create_app(_settings(tmp_path))

    def broken() -> None:
        raise OperationalError("SELECT 1", {}, Exception("server at 10.0.0.5 closed connection"))

    app.add_api_route("/drill/broken", broken)
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/drill/broken")
    assert response.status_code == 503 and response.headers["Retry-After"] == "1"
    assert response.json() == {"detail": "dependency unavailable"}
