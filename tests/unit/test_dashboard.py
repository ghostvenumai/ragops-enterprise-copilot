from __future__ import annotations

import json
from io import BytesIO
from urllib.error import HTTPError
from urllib.request import Request

import pytest
from apps.dashboard import dashboard


class FakeResponse:
    def __init__(self, payload: object) -> None:
        self.payload = json.dumps(payload).encode("utf-8")

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read(self) -> bytes:
        return self.payload


def test_parse_prometheus_ignores_invalid_lines() -> None:
    payload = (
        "# HELP sample\n"
        "ragops_request_count 4\n"
        "ragops_success_rate 0.75\n"
        "unrelated_metric 99\n"
        "ragops_invalid not-a-number\n"
    )

    assert dashboard.parse_prometheus(payload) == {
        "request_count": 4.0,
        "success_rate": 0.75,
    }


@pytest.mark.parametrize(
    ("value", "expected"),
    [(0.85, "85%"), ("1", "100%"), (None, "--"), (object(), "--")],
)
def test_percent_formats_safe_values(value: object, expected: str) -> None:
    assert dashboard.percent(value) == expected


def test_option_label_hides_internal_values() -> None:
    assert dashboard.option_label(dashboard.TENANTS, "tenant-alpha") == "Atlas Industrial"
    assert dashboard.option_label(dashboard.ROLES, "sales") == "Vertrieb"
    assert dashboard.option_label(dashboard.ROLES, "unknown") == "unknown"


def test_chat_role_rejects_unknown_presentation_roles() -> None:
    assert dashboard.chat_role({"role": "user"}) == "user"
    assert dashboard.chat_role({"role": "admin"}) == "assistant"
    assert dashboard.chat_role({"user_id": "user"}) == "assistant"


def test_api_request_serializes_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def fake_urlopen(request: Request, timeout: float) -> FakeResponse:
        captured["url"] = request.full_url
        captured["method"] = request.method
        captured["body"] = json.loads(request.data or b"{}")
        captured["timeout"] = timeout
        return FakeResponse({"status": "ok"})

    monkeypatch.setattr(dashboard, "urlopen", fake_urlopen)

    result = dashboard.api_request("POST", "/v1/query", {"question": "test"}, timeout=3)

    assert result == {"status": "ok"}
    assert captured == {
        "url": f"{dashboard.API_BASE_URL}/v1/query",
        "method": "POST",
        "body": {"question": "test"},
        "timeout": 3,
    }


def test_api_request_returns_safe_http_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def reject(_request: Request, *, timeout: float) -> FakeResponse:
        raise HTTPError(
            "http://api/v1/query",
            422,
            "invalid",
            {},
            BytesIO(b'{"detail":"question is required"}'),
        )

    monkeypatch.setattr(dashboard, "urlopen", reject)

    with pytest.raises(dashboard.DashboardApiError, match=r"API-Anfrage fehlgeschlagen \(422\)"):
        dashboard.api_request("POST", "/v1/query")
