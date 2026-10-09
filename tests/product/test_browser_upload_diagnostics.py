"""browser_e2e upload diagnostics: every broken link gets its own class, no secrets are kept.

The live failure behind these tests: the API accepted the upload within ~100 ms and the job
completed, but two full Streamlit reruns (job list switching into polling and back) removed
the acknowledgement 250-600 ms after it appeared, so the gate saw it only by chance
(SUCCESS_MESSAGE_TRANSIENT). Classification is tested on recorded diagnostics; the DOM
recorder runs in the same headless Chromium the gate uses and is skipped without it.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from scripts import browser_e2e_gate as gate
from scripts.browser_e2e_services import RequestRecorder

ACCEPTED = {"status": 202, "start_ms": 170.0, "end_ms": 270.0, "duration_ms": 100.0}
STABLE_ACK = {"shown": True, "first_shown_ms": 500.0, "removed_ms": None, "visible_at_end": True}
COMPLETED_JOB = {"versions": 1, "jobs": [{"job_id": "j", "status": "completed"}]}


def diagnostics(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "request_log_available": True,
        "api_upload_requests": [ACCEPTED],
        "acknowledgement": STABLE_ACK,
        "database": COMPLETED_JOB,
        "state_reached": True,
    }
    return {**base, **overrides}


# --------------------------------------------------------------------------- classification


def test_1_accepted_upload_with_acknowledgement_passes() -> None:
    assert gate.classify_upload(diagnostics()) == gate.UPLOAD_OK


@pytest.mark.parametrize("status", [400, 401, 403, 429, 500, 503, None])
def test_2_rejected_upload_is_api_request_failed(status: int | None) -> None:
    rejected = {**ACCEPTED, "status": status}
    no_ack = {**STABLE_ACK, "shown": False, "visible_at_end": False}
    result = gate.classify_upload(
        diagnostics(api_upload_requests=[rejected], acknowledgement=no_ack)
    )
    assert result == "API_REQUEST_FAILED"


def test_3_request_that_never_reaches_the_api_is_submit_not_sent() -> None:
    no_ack = {**STABLE_ACK, "shown": False, "visible_at_end": False}
    result = gate.classify_upload(diagnostics(api_upload_requests=[], acknowledgement=no_ack))
    assert result == "SUBMIT_NOT_SENT"


def test_4_accepted_upload_without_acknowledgement_fails() -> None:
    no_ack = {"shown": False, "first_shown_ms": None, "removed_ms": None, "visible_at_end": False}
    assert gate.classify_upload(diagnostics(acknowledgement=no_ack)) == (
        "API_ACCEPTED_UI_NOT_UPDATED"
    )


def test_5_acknowledgement_removed_by_a_rerun_is_transient_and_fails() -> None:
    # The recorded live pattern: shown at 504 ms, stale at 584 ms, removed at 756 ms.
    transient = {
        "shown": True,
        "first_shown_ms": 504.0,
        "removed_ms": 756.0,
        "visible_at_end": False,
    }
    assert gate.classify_upload(diagnostics(acknowledgement=transient)) == (
        "SUCCESS_MESSAGE_TRANSIENT"
    )


@pytest.mark.parametrize(
    ("database", "state_reached"),
    [({"versions": 1, "jobs": []}, None), (COMPLETED_JOB, False), (None, True)],
)
def test_6_accepted_upload_without_resulting_job_state_fails(
    database: dict[str, Any] | None, state_reached: bool | None
) -> None:
    result = gate.classify_upload(diagnostics(database=database, state_reached=state_reached))
    assert result == "UPLOAD_STATE_MISSING"


@pytest.mark.parametrize(
    "upload_request",
    [
        {**ACCEPTED, "end_ms": None, "duration_ms": None, "status": None},  # still in flight
        {**ACCEPTED, "end_ms": 20_400.0, "duration_ms": 20_230.0},  # beyond the window
    ],
)
def test_slow_or_hanging_api_is_its_own_class(upload_request: dict[str, Any]) -> None:
    result = gate.classify_upload(diagnostics(api_upload_requests=[upload_request]))
    assert result == "API_TIMEOUT_OR_SLOW_RESPONSE"


def test_missing_or_contradictory_evidence_is_undetermined() -> None:
    assert gate.classify_upload(diagnostics(api_upload_requests=None)) == "UNDETERMINED"
    # The UI acknowledging an upload the API never saw contradicts the evidence.
    assert gate.classify_upload(diagnostics(api_upload_requests=[])) == "UNDETERMINED"


def test_classes_are_never_a_generic_timeout() -> None:
    assert set(gate.UPLOAD_FAILURE_CLASSES) == {
        "SUBMIT_NOT_SENT",
        "API_REQUEST_FAILED",
        "API_TIMEOUT_OR_SLOW_RESPONSE",
        "API_ACCEPTED_UI_NOT_UPDATED",
        "SUCCESS_MESSAGE_TRANSIENT",
        "UPLOAD_STATE_MISSING",
        "UNDETERMINED",
    }


def test_acknowledgement_stability_is_a_required_check() -> None:
    assert "upload_acknowledgement_stable" in gate.REQUIRED
    assert gate.UI_TIMEOUT_MS == 20_000


# --------------------------------------------------------------------------- API request recorder


def _drive(app: Any, scope: dict[str, Any]) -> None:
    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"%PDF secret document body", "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        return None

    asyncio.run(app(scope, receive, send))


def test_request_recorder_keeps_only_method_path_status_and_time(tmp_path: Path) -> None:
    log = tmp_path / "requests.jsonl"

    async def api(scope: Any, receive: Any, send: Any) -> None:
        await receive()
        await send({"type": "http.response.start", "status": 202, "headers": []})
        await send({"type": "http.response.body", "body": b'{"job_id": "x"}'})

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/v1/documents/upload",
        "query_string": b"token=query-secret",
        "headers": [
            (b"authorization", b"Bearer eyJhbGciOi.header-secret"),
            (b"cookie", b"_streamlit_user=cookie-secret"),
        ],
    }
    _drive(RequestRecorder(api, log), scope)
    raw = log.read_text(encoding="utf-8")
    for secret in ("query-secret", "header-secret", "cookie-secret", "secret document", "job_id"):
        assert secret not in raw
    lines = [json.loads(line) for line in raw.splitlines()]
    assert [line["phase"] for line in lines] == ["start", "end"]
    assert set(lines[0]) == {"id", "phase", "method", "path", "t"}
    assert set(lines[1]) == {"id", "phase", "status", "t"}
    paired = gate._read_request_log(log)
    assert paired is not None and len(paired) == 1
    assert paired[0]["status"] == 202 and paired[0]["path"] == "/v1/documents/upload"
    assert paired[0]["end"] >= paired[0]["start"]


def test_request_log_shows_a_request_still_in_flight(tmp_path: Path) -> None:
    log = tmp_path / "requests.jsonl"
    log.write_text(
        json.dumps({"id": "a", "phase": "start", "method": "POST", "path": "/x", "t": 1.0}) + "\n",
        encoding="utf-8",
    )
    assert gate._read_request_log(log) == [
        {"status": None, "end": None, "method": "POST", "path": "/x", "start": 1.0}
    ]
    assert gate._read_request_log(tmp_path / "missing.jsonl") is None
    assert gate._read_request_log(None) is None


# --------------------------------------------------------------------------- DOM recorder

PAGE = """
<!doctype html><html><body>
  <div data-testid="stMainBlockContainer">
    <div id="slot" data-stale="false"></div>
  </div>
  <script>
    const slot = document.getElementById('slot');
    const alert = (kind, text) =>
      '<div data-testid="stAlertContent' + kind + '">' + text + '</div>';
    window.show = () => { slot.innerHTML = alert('Success', ACK); };
    window.rerunRemoves = () => {
      slot.dataset.stale = 'true';
      setTimeout(() => { slot.innerHTML = ''; slot.dataset.stale = 'false'; }, 150);
    };
    window.flash = () => { show(); slot.innerHTML = ''; };
    window.rejectWithError = () => { slot.innerHTML = alert('Error', 'Die Datei ist leer.'); };
  </script>
</body></html>
"""
ACK_TEXT = "Upload angenommen · Auftrag 7f3a9c21 · In Warteschlange · Titel Geheim-Titel"


@pytest.fixture(scope="module")
def browser() -> Iterator[Any]:
    playwright_api = pytest.importorskip("playwright.sync_api")
    with playwright_api.sync_playwright() as playwright:
        try:
            instance = playwright.chromium.launch(channel="chromium", headless=True)
        except Exception as exc:  # noqa: BLE001 - no local browser build
            pytest.skip(f"headless Chromium unavailable: {type(exc).__name__}")
        yield instance
        instance.close()


def probe_on(browser: Any, tmp_path: Path, accepted: bool = True) -> tuple[Any, Any]:
    context = browser.new_context()
    context.add_cookies(
        [{"name": "_streamlit_user", "value": "cookie-secret-value", "url": "http://127.0.0.1"}]
    )
    page = context.new_page()
    page.set_content(PAGE.replace("ACK", json.dumps(ACK_TEXT)))
    log = tmp_path / "api-requests.jsonl"
    log.touch()  # the recorder ran; whether it saw the upload is up to the test
    session = SimpleNamespace(ws_frames={}, run_dir=tmp_path)
    env = SimpleNamespace(request_log=log, database_url="sqlite://")
    probe = gate.UploadProbe(session, page, env, b"%PDF-1.4 secret document content")  # type: ignore[arg-type]
    probe.arm()
    probe.mark_submit()
    if accepted:
        start = probe.submitted_at + 0.1
        log.write_text(
            json.dumps(
                {
                    "id": "r",
                    "phase": "start",
                    "method": "POST",
                    "path": gate.UPLOAD_PATH,
                    "t": start,
                }
            )
            + "\n"
            + json.dumps({"id": "r", "phase": "end", "status": 202, "t": start + 0.1})
            + "\n",
            encoding="utf-8",
        )
    probe._database = lambda: COMPLETED_JOB  # type: ignore[method-assign]
    return context, probe


def test_dom_recorder_sees_a_stable_acknowledgement(browser: Any, tmp_path: Path) -> None:
    context, probe = probe_on(browser, tmp_path)
    try:
        probe.page.evaluate("show()")
        probe.page.wait_for_timeout(100)
        result = probe.collect(state_reached=True)
        ack = result["acknowledgement"]
        assert ack["shown"] and ack["visible_at_end"] and ack["removed_ms"] is None
        assert result["classification"] == gate.UPLOAD_OK
        assert result["api_upload_requests"][0]["status"] == 202
    finally:
        context.close()


def test_dom_recorder_sees_a_rerun_remove_the_acknowledgement(browser: Any, tmp_path: Path) -> None:
    context, probe = probe_on(browser, tmp_path)
    try:
        probe.page.evaluate("show()")
        probe.page.evaluate("rerunRemoves()")
        probe.page.wait_for_timeout(400)
        result = probe.collect(state_reached=True)
        kinds = [event["kind"] for event in result["dom_events"]]
        assert kinds.index("ack_shown") < kinds.index("ack_stale") < kinds.index("ack_removed")
        assert result["acknowledgement"]["removed_ms"] is not None
        assert result["classification"] == "SUCCESS_MESSAGE_TRANSIENT"
    finally:
        context.close()


def test_dom_recorder_sees_an_acknowledgement_inside_one_mutation_batch(
    browser: Any, tmp_path: Path
) -> None:
    context, probe = probe_on(browser, tmp_path)
    try:
        probe.page.evaluate("flash()")
        probe.page.wait_for_timeout(100)
        result = probe.collect(state_reached=True)
        assert result["acknowledgement"]["shown"] is True
        assert result["acknowledgement"]["visible_at_end"] is False
        assert result["classification"] == "SUCCESS_MESSAGE_TRANSIENT"
    finally:
        context.close()


def test_dom_recorder_reports_an_error_alert_without_its_text(browser: Any, tmp_path: Path) -> None:
    context, probe = probe_on(browser, tmp_path, accepted=False)
    try:
        probe.page.evaluate("rejectWithError()")
        probe.page.wait_for_timeout(100)
        result = probe.collect(state_reached=None)
        assert "error_shown" in result["alerts"]
        assert result["acknowledgement"]["shown"] is False
        assert result["classification"] == "SUBMIT_NOT_SENT"  # no API request was logged
    finally:
        context.close()


# --------------------------------------------------------------------------- 7 no secrets kept


def test_failure_artifact_holds_no_secrets_or_document_content(
    browser: Any, tmp_path: Path
) -> None:
    context, probe = probe_on(browser, tmp_path)
    try:
        probe.page.evaluate("show()")
        probe.page.evaluate("rerunRemoves()")
        probe.page.wait_for_timeout(400)
        result = gate.GateResult()
        gate.record_upload_diagnostics(
            result, probe.session, probe.collect(state_reached=True), failed=True
        )
    finally:
        context.close()
    artifact = tmp_path / "upload-diagnostics.json"
    raw = artifact.read_text(encoding="utf-8")
    for forbidden in (
        "cookie-secret-value",
        "secret document content",
        "Geheim-Titel",
        "Upload angenommen",
        "Bearer",
        "?",
    ):
        assert forbidden not in raw
    assert gate.scan_artifacts(tmp_path, ["cookie-secret-value"]) == []
    assert result.metrics["upload_failure_classification"] == "SUCCESS_MESSAGE_TRANSIENT"
    assert set(result.metrics["upload_diagnostics"]) == {
        "classification",
        "api_upload_requests",
        "dashboard_full_reruns_after_upload",
        "acknowledgement",
        "state_reached",
    }


def test_successful_upload_keeps_no_failure_artifact(browser: Any, tmp_path: Path) -> None:
    context, probe = probe_on(browser, tmp_path)
    try:
        probe.page.evaluate("show()")
        probe.page.wait_for_timeout(100)
        result = gate.GateResult()
        gate.record_upload_diagnostics(
            result, probe.session, probe.collect(state_reached=True), failed=False
        )
    finally:
        context.close()
    assert not (tmp_path / "upload-diagnostics.json").exists()
    assert result.metrics["upload_failure_classification"] is None
