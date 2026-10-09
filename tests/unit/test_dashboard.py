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


@pytest.mark.parametrize(
    ("scene", "expected"),
    [
        ("answer", "answer"),
        (["blocked"], "blocked"),
        ("../../etc/passwd", ""),
        (None, ""),
    ],
)
def test_normalize_demo_scene_is_allowlisted(scene: object, expected: str) -> None:
    assert dashboard.normalize_demo_scene(scene) == expected


def test_demo_navigation_and_export_are_deterministic() -> None:
    assert dashboard.navigation_for_demo_scene("monitoring") == "Monitoring"
    assert dashboard.navigation_for_demo_scene("unknown") == "Copilot"
    payload = json.loads(
        dashboard.export_chat([{"role": "assistant", "answer": "Belegte Antwort", "citations": []}])
    )
    assert payload == {
        "synthetic": True,
        "messages": [{"role": "assistant", "answer": "Belegte Antwort", "citations": []}],
    }


def test_internal_api_url_rejects_non_http_and_credentials() -> None:
    with pytest.raises(dashboard.DashboardApiError, match="Konfiguration"):
        dashboard.validated_internal_url("file:///tmp/evidence.json")
    with pytest.raises(dashboard.DashboardApiError, match="Konfiguration"):
        dashboard.validated_internal_url("http://user:secret@localhost:8000")

    assert dashboard.validated_internal_url("http://api:8000/ready") == "http://api:8000/ready"


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

    with pytest.raises(dashboard.DashboardApiError, match="Eingaben prüfen") as raised:
        dashboard.api_request("POST", "/v1/query")
    # The API detail is kept for mapping only and never becomes the visible message.
    assert raised.value.status == 422 and raised.value.detail == "question is required"
    assert "question is required" not in str(raised.value)


@pytest.mark.parametrize(
    ("status", "retry_after", "expected"),
    [
        (401, None, "Sitzung ist abgelaufen"),
        (403, None, "fehlt Ihrer Rolle die Berechtigung"),
        (404, None, "nicht gefunden"),
        (429, 55, "Bitte in 55 Sekunden erneut versuchen"),
        (429, None, "Bitte in Kürze erneut versuchen"),
        (503, None, "vorübergehend nicht verfügbar"),
        (500, None, "Fehler gemeldet"),
    ],
)
def test_http_errors_become_actionable_text(status, retry_after, expected) -> None:
    message = dashboard.http_error_message(status, retry_after)
    assert expected in message and str(status) not in message


def test_rate_limit_error_keeps_retry_after(monkeypatch: pytest.MonkeyPatch) -> None:
    def limited(_request: Request, *, timeout: float) -> FakeResponse:
        raise HTTPError(
            "http://api/v1/query",
            429,
            "limited",
            {"Retry-After": "42"},  # type: ignore[arg-type]
            BytesIO(b'{"detail":"rate limit exceeded"}'),
        )

    monkeypatch.setattr(dashboard, "urlopen", limited)
    with pytest.raises(dashboard.DashboardApiError, match="42 Sekunden") as raised:
        dashboard.api_request("POST", "/v1/query")
    assert raised.value.status == 429 and raised.value.retry_after == 42


def test_development_mode_never_sends_a_bearer_token() -> None:
    assert dashboard.OIDC_MODE is False
    assert dashboard.access_token() is None
    assert "Authorization" not in dashboard._headers()


def test_upload_is_sent_as_multipart(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def fake_urlopen(request: Request, timeout: float) -> FakeResponse:
        captured["type"] = request.headers["Content-type"]
        captured["body"] = request.data
        return FakeResponse({"job_id": "1234567890", "status": "queued"})

    monkeypatch.setattr(dashboard, "urlopen", fake_urlopen)
    result = dashboard.api_upload(
        "/v1/documents/upload", {"title": "T"}, 'a"b.pdf', b"%PDF-1.4", "application/pdf"
    )
    body = captured["body"]
    assert result == {"job_id": "1234567890", "status": "queued"}
    assert str(captured["type"]).startswith("multipart/form-data; boundary=")
    assert isinstance(body, bytes) and b'filename="a_b.pdf"' in body and b"%PDF-1.4" in body


def test_user_text_renders_literally() -> None:
    escaped = dashboard.plain_text('<img src=x onerror="alert(1)"> **fett** [link](http://x)')
    # Every Markdown/HTML control character is backslash-escaped, so it renders as text.
    assert "\\<img" in escaped and "\\*\\*fett\\*\\*" in escaped
    assert "\\]\\(" in escaped and " <img" not in escaped


def test_logical_key_is_safe_and_stable() -> None:
    assert dashboard.logical_key("Quartal <b>2026</b>", "x.pdf") == "quartal-b-2026-b"
    assert dashboard.logical_key("", "Bericht.PDF") == "bericht-pdf"
    assert dashboard.logical_key("!!!", "") == "dokument"


def test_job_rows_show_status_as_text_and_keep_titles_verbatim() -> None:
    rows = dashboard.job_rows(
        [
            {"job_id": "abcdef1234", "status": "failed", "title": "<b>x</b>", "progress": 14},
            {"job_id": "0000000000", "status": "completed", "filename": "a.pdf"},
            "not-a-job",
        ]
    )
    assert rows[0]["Status"] == "Fehlgeschlagen" and rows[0]["Dokument"] == "<b>x</b>"
    assert rows[0]["Hinweis"] == "Erneut versuchen möglich" and rows[0]["Auftrag"] == "abcdef12"
    assert rows[1]["Status"] == "Bereit" and rows[1]["Datei"] == "a.pdf" and len(rows) == 2


def test_known_upload_rejections_have_german_text() -> None:
    for detail in (
        "unsupported file type or MIME type",
        "empty files are not allowed",
        "file content does not match its type",
        "duplicate content in collection",
    ):
        assert (
            dashboard.UPLOAD_REJECTIONS[detail]
            and detail not in dashboard.UPLOAD_REJECTIONS[detail]
        )


def test_vector_mode_shows_the_indexed_documents_hint_instead_of_an_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, str]] = []

    def unavailable(method: str, path: str, *args: object, **kwargs: object) -> object:
        raise dashboard.DashboardApiError(
            "Die angeforderte Ressource wurde nicht gefunden.",
            status=404,
            detail=dashboard.VECTOR_MODE_DETAIL,
        )

    monkeypatch.setattr(dashboard, "api_request", unavailable)
    for name in ("subheader", "caption", "error"):
        monkeypatch.setattr(
            dashboard.st, name, lambda text, *a, _n=name, **k: calls.append((_n, str(text)))
        )
    dashboard.render_query_corpus("tenant-alpha")
    assert [kind for kind, _ in calls] == ["subheader", "caption"]
    assert "indexierten Dokumenten" in calls[1][1]

    calls.clear()

    def other_failure(method: str, path: str, *args: object, **kwargs: object) -> object:
        raise dashboard.DashboardApiError("Fehler", status=404, detail="document not found")

    monkeypatch.setattr(dashboard, "api_request", other_failure)
    dashboard.render_query_corpus("tenant-alpha")
    assert calls == [("error", "Fehler")]
