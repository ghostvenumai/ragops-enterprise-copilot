"""The upload acknowledgement survives the full reruns its own job triggers.

An accepted upload creates a queued job; the job list then switches into polling with a
full ``st.rerun()`` and back once the job is done. The submit outcome was rendered only in
the submit run, so these reruns removed it within a few hundred milliseconds (browser_e2e
classification SUCCESS_MESSAGE_TRANSIENT). The tests drive the real knowledge page with
Streamlit's AppTest and a fake API.
"""

from __future__ import annotations

from typing import Any

import pytest
import streamlit
from apps.dashboard import dashboard
from streamlit.testing.v1 import AppTest

ACK = "Upload angenommen · Auftrag 7f3a9c21 · In Warteschlange"

SCRIPT = """
from apps.dashboard import dashboard
dashboard.render_knowledge_base(dashboard.st.session_state.get("tenant", "tenant-alpha"))
"""

MAIN_SCRIPT = """
from apps.dashboard import dashboard
dashboard.main()
"""


class FakeApi:
    """The API the knowledge page talks to; the worker finishes a job on the next listing."""

    def __init__(self, accepted: tuple[str, str]) -> None:
        self.accepted = accepted
        self.jobs: list[dict[str, Any]] = []
        self.listings = 0
        self.uploads = 0

    def request(self, method: str, path: str, *args: Any, **kwargs: Any) -> Any:
        if path == "/v1/workspaces":
            return [{"id": "ws-1", "name": "Workspace"}]
        if path.startswith("/v1/collections"):
            return [{"id": "coll-1", "name": "Collection", "access_level": "internal"}]
        if path == "/v1/ingestion/jobs":
            self.listings += 1
            jobs = [dict(job) for job in self.jobs]
            for job in self.jobs:  # the worker completes the job after one listing
                job["status"] = "completed"
            return jobs
        if path == "/v1/documents":
            return []
        if method == "GET":  # sidebar readiness and the other pages: not under test
            return {}
        raise AssertionError(f"unexpected API call {method} {path}")

    def submit(self, workspace_id: str, collection_id: str, title: str, upload: Any) -> Any:
        self.uploads += 1
        if self.accepted[0] == "success":
            self.jobs.append(
                {"job_id": "7f3a9c21-0000", "status": "queued", "title": title, "filename": "x"}
            )
        return self.accepted


@pytest.fixture
def api(monkeypatch: pytest.MonkeyPatch) -> FakeApi:
    fake = FakeApi(("success", ACK))
    monkeypatch.setattr(dashboard, "api_request", fake.request)
    monkeypatch.setattr(dashboard, "submit_upload", fake.submit)
    # AppTest cannot attach files; any truthy upload stands for a validated file.
    monkeypatch.setattr(streamlit, "file_uploader", lambda *args, **kwargs: object())
    return fake


def submit(app: AppTest) -> AppTest:
    button = next(item for item in app.button if item.label == "Hochladen")
    return button.click().run(timeout=30)


def alerts(app: AppTest) -> list[str]:
    return [
        item.value for kind in ("success", "info", "warning", "error") for item in app.get(kind)
    ]


def test_acknowledgement_survives_the_polling_switch_reruns(api: FakeApi) -> None:
    app = AppTest.from_string(SCRIPT).run(timeout=30)
    assert ACK not in alerts(app)
    app = submit(app)
    assert api.uploads == 1
    # The queued job switched the list into polling and the completed job switched it back:
    # two full reruns after the submit run.
    assert api.listings >= 3
    assert app.session_state["jobs_polling"] is False
    assert ACK in alerts(app)
    # Another unrelated rerun (for example a fragment-triggered full run) keeps it as well.
    app = app.run(timeout=30)
    assert ACK in alerts(app)
    assert alerts(app).count(ACK) == 1


def test_a_rejection_is_shown_and_replaced_by_the_next_outcome(
    api: FakeApi, monkeypatch: pytest.MonkeyPatch
) -> None:
    api.accepted = ("error", "Die Datei ist leer.")
    app = submit(AppTest.from_string(SCRIPT).run(timeout=30))
    assert alerts(app) == ["Die Datei ist leer."]
    api.accepted = ("success", ACK)
    app = submit(app)
    assert alerts(app) == [ACK]


def test_the_notice_belongs_to_the_tenant_that_uploaded(api: FakeApi) -> None:
    app = submit(AppTest.from_string(SCRIPT).run(timeout=30))
    assert ACK in alerts(app)
    app.session_state["tenant"] = "tenant-beta"
    app = app.run(timeout=30)
    assert ACK not in alerts(app)


def test_leaving_the_knowledge_page_clears_the_notice(
    api: FakeApi, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(dashboard, "OIDC_MODE", False)
    monkeypatch.setenv("RAGOPS_ENV", "test")
    app = AppTest.from_string(MAIN_SCRIPT).run(timeout=30)
    navigation = app.sidebar.radio[0]
    app = navigation.set_value("Wissensbasis").run(timeout=30)
    app = submit(app)
    assert ACK in alerts(app)
    assert dashboard.UPLOAD_NOTICE in app.session_state
    app = app.sidebar.radio[0].set_value("Monitoring").run(timeout=30)
    assert dashboard.UPLOAD_NOTICE not in app.session_state
    app = app.sidebar.radio[0].set_value("Wissensbasis").run(timeout=30)
    assert ACK not in alerts(app)
