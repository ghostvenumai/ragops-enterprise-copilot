"""The retry acknowledgement survives the full rerun its own requeued job triggers.

A retried job is queued again, so the job list switches into polling with a full
``st.rerun()``. The notice was popped and rendered just before that switch and vanished
(browser_e2e outage_recovery: "Auftrag erneut eingereiht" never visible when the worker had
not finished the job by the next listing). The tests drive the real knowledge page with
Streamlit's AppTest and a fake API.
"""

from __future__ import annotations

from typing import Any

import pytest
import streamlit
from apps.dashboard import dashboard
from streamlit.testing.v1 import AppTest

RETRIED = "Auftrag erneut eingereiht."
JOB = "5b1e0c77-0000"

SCRIPT = """
from apps.dashboard import dashboard
dashboard.render_knowledge_base(dashboard.st.session_state.get("tenant", "tenant-alpha"))
"""


class FakeApi:
    """A failed job; after a retry it stays queued for ``queued_listings`` listings."""

    def __init__(self, queued_listings: int) -> None:
        self.job = {"job_id": JOB, "status": "failed", "title": "Ausfall", "filename": "a.pdf"}
        self.queued_listings = queued_listings
        self.retries = 0

    def request(self, method: str, path: str, *args: Any, **kwargs: Any) -> Any:
        if path == "/v1/workspaces":
            return [{"id": "ws-1", "name": "Workspace"}]
        if path.startswith("/v1/collections"):
            return [{"id": "coll-1", "name": "Collection", "access_level": "internal"}]
        if path == "/v1/ingestion/jobs":
            listed = [dict(self.job)]
            if self.job["status"] == "queued":
                if self.queued_listings <= 0:
                    self.job["status"] = "completed"
                self.queued_listings -= 1
            return listed
        if path == f"/v1/ingestion/jobs/{JOB}/retry" and method == "POST":
            self.retries += 1
            self.job["status"] = "queued"
            return {"job_id": JOB, "status": "queued"}
        if path == "/v1/documents":
            return []
        if method == "GET":
            return {}
        raise AssertionError(f"unexpected API call {method} {path}")


@pytest.fixture(autouse=True)
def full_reruns(monkeypatch: pytest.MonkeyPatch) -> None:
    # AppTest runs a button click as a full script run, where a fragment-scoped rerun is not
    # allowed; a full rerun renders a superset of the fragment and keeps the same order.
    rerun = streamlit.rerun
    monkeypatch.setattr(streamlit, "rerun", lambda *args, **kwargs: rerun())


def alerts(app: AppTest) -> list[str]:
    return [
        item.value for kind in ("success", "info", "warning", "error") for item in app.get(kind)
    ]


def retry(app: AppTest) -> AppTest:
    button = next(item for item in app.button if item.label == "Erneut versuchen")
    return button.click().run(timeout=30)


@pytest.mark.parametrize("queued_listings", [0, 1, 3])
def test_retry_notice_survives_the_polling_switch(
    monkeypatch: pytest.MonkeyPatch, queued_listings: int
) -> None:
    api = FakeApi(queued_listings)
    monkeypatch.setattr(dashboard, "api_request", api.request)
    app = AppTest.from_string(SCRIPT).run(timeout=30)
    assert RETRIED not in alerts(app)
    app = retry(app)
    assert api.retries == 1
    assert RETRIED in alerts(app)
    # Polling ticks and unrelated reruns keep the outcome until the next action.
    app = app.run(timeout=30)
    assert alerts(app).count(RETRIED) == 1


def test_retry_notice_belongs_to_the_tenant_that_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    api = FakeApi(1)
    monkeypatch.setattr(dashboard, "api_request", api.request)
    app = retry(AppTest.from_string(SCRIPT).run(timeout=30))
    assert RETRIED in alerts(app)
    app.session_state["tenant"] = "tenant-beta"
    app = app.run(timeout=30)
    assert RETRIED not in alerts(app)
