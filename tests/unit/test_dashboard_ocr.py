"""Dashboard support for scanned documents: image uploads and honest processing states."""

from __future__ import annotations

from pathlib import Path

from apps.dashboard import dashboard

from ragops.security.upload import ALLOWED_EXTENSIONS


def test_upload_accepts_scans_and_every_previous_format() -> None:
    assert set(dashboard.UPLOAD_TYPES) == {"pdf", "docx", "md", "txt", "csv", "jpg", "jpeg", "png"}
    assert {f".{suffix}" for suffix in dashboard.UPLOAD_TYPES} == ALLOWED_EXTENSIONS
    unsupported = dashboard.UPLOAD_REJECTIONS["unsupported file type or MIME type"]
    assert "JPEG" in unsupported and "PNG" in unsupported
    assert "12 MB" in dashboard.UPLOAD_REJECTIONS["file exceeds size limit"]
    assert "Bild" in dashboard.UPLOAD_REJECTIONS["image exceeds safety limits"]


def test_review_required_is_never_shown_as_ready() -> None:
    rows = dashboard.job_rows(
        [
            {
                "job_id": "1" * 10,
                "status": "completed",
                "review_required": False,
                "pages_review": 0,
            },
            {"job_id": "2" * 10, "status": "completed", "review_required": True, "pages_review": 2},
            {
                "job_id": "3" * 10,
                "status": "failed",
                "error_code": "ocr_review_required",
                "review_required": True,
                "pages_review": 1,
            },
            {"job_id": "4" * 10, "status": "failed", "error_code": "ocr_engine_unavailable"},
        ]
    )
    assert rows[0]["Status"] == "Bereit"
    assert rows[1]["Status"] == "Prüfung erforderlich"
    assert "2 Seiten" in str(rows[1]["Hinweis"])
    assert rows[2]["Status"] == "Prüfung erforderlich"
    assert rows[3]["Status"] == "Fehlgeschlagen" and "Texterkennung" in str(rows[3]["Hinweis"])
    assert all(row["Status"] != "Bereit" for row in rows[1:])


def test_the_stable_upload_acknowledgement_is_unchanged() -> None:
    source = Path(dashboard.__file__).read_text(encoding="utf-8")
    assert "st.session_state[UPLOAD_NOTICE] = (tenant_id, *outcome)" in source
    assert "notice = st.session_state.get(UPLOAD_NOTICE)" in source
    # Cleared only when the user navigates away from the knowledge page (ENT-11.5).
    assert source.count("st.session_state.pop(UPLOAD_NOTICE") == 1
