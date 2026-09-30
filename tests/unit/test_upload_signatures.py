"""Binary uploads must carry their format signature; renamed files are rejected at intake."""

from __future__ import annotations

import pytest

from ragops.knowledge.service import validate_upload
from ragops.security.upload import UnsafeUploadError

DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def test_real_pdf_and_docx_signatures_are_accepted() -> None:
    from scripts.browser_e2e_gate import synthetic_pdf
    from tests.unit.test_document_inspection import docx

    assert validate_upload("a.pdf", "application/pdf", synthetic_pdf("x")) == "a.pdf"
    assert validate_upload("a.docx", DOCX, docx()) == "a.docx"
    assert validate_upload("a.txt", "text/plain", b"plain text") == "a.txt"


@pytest.mark.parametrize(
    ("name", "mime", "content"),
    [
        ("a.pdf", "application/pdf", b"not a pdf"),
        ("a.pdf", "application/pdf", b"MZ\x90\x00 renamed executable"),
        ("a.docx", DOCX, b"%PDF-1.4 wrong container"),
    ],
)
def test_mismatched_content_is_rejected(name, mime, content) -> None:
    with pytest.raises(UnsafeUploadError, match="does not match its type"):
        validate_upload(name, mime, content)
