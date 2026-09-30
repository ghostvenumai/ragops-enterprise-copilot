"""Uploaded documents are structurally inspected before persistence; nothing is executed."""

from __future__ import annotations

import io
import zipfile

import pytest
from scripts.browser_e2e_gate import synthetic_pdf

from ragops.knowledge.service import validate_upload
from ragops.security.upload import UnsafeUploadError

DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
PDF = "application/pdf"


def docx(extra: dict[str, bytes] | None = None, *, drop: str = "") -> bytes:
    from docx import Document

    buffer = io.BytesIO()
    Document().save(buffer)
    source = zipfile.ZipFile(io.BytesIO(buffer.getvalue()))
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as target:
        for item in source.infolist():
            if item.filename != drop:
                target.writestr(item, source.read(item.filename))
        for name, content in (extra or {}).items():
            target.writestr(name, content)
    return out.getvalue()


def test_real_documents_are_accepted() -> None:
    assert validate_upload("a.pdf", PDF, synthetic_pdf("hallo")) == "a.pdf"
    assert validate_upload("a.docx", DOCX, docx()) == "a.docx"
    assert validate_upload("a.md", "text/markdown", "# Überschrift\n".encode()) == "a.md"


@pytest.mark.parametrize(
    ("name", "content"),
    [
        ("truncated.pdf", synthetic_pdf("x")[:-40]),
        (
            "script.pdf",
            synthetic_pdf("x").replace(
                b"/Type /Catalog",
                b"/Type /Catalog /OpenAction << /S /JavaScript /JS (app.alert(1)) >>",
            ),
        ),
        (
            "launch.pdf",
            synthetic_pdf("x").replace(
                b"/Type /Catalog", b"/Type /Catalog /AA << /O << /S /Launch /F (cmd.exe) >> >>"
            ),
        ),
        (
            "embedded.pdf",
            synthetic_pdf("x").replace(
                b"/Type /Catalog", b"/Type /Catalog /Names << /EmbeddedFiles 9 0 R >>"
            ),
        ),
        (
            "encrypted.pdf",
            synthetic_pdf("x").replace(b"trailer\n<<", b"trailer\n<< /Encrypt 9 0 R"),
        ),
        (
            "polyglot.pdf",
            synthetic_pdf("x") + b"PK\x03\x04" + b"\x00" * 26 + b"PK\x05\x06" + b"\x00" * 18,
        ),
        ("garbage.pdf", b"%PDF-1.7\n" + b"\x00" * 200),
    ],
)
def test_unsafe_or_malformed_pdfs_are_rejected(name, content) -> None:
    with pytest.raises(UnsafeUploadError):
        validate_upload(name, PDF, content)


def _bomb() -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("word/document.xml", "<w:document/>")
        archive.writestr("word/media/zeros.bin", b"\x00" * 30_000_000)
    return out.getvalue()


@pytest.mark.parametrize(
    "content",
    [
        docx({"../../evil.txt": b"x"}),  # zip slip
        docx({"/etc/cron.d/x": b"x"}),  # absolute path
        docx({"word/vbaProject.bin": b"macro"}),  # macros
        docx({f"word/media/{i}.xml": b"<a/>" for i in range(600)}),  # member count
        docx(drop="word/document.xml"),  # structurally incomplete
        docx(
            {
                "word/document.xml": b'<?xml version="1.0"?><!DOCTYPE d [<!ENTITY x SYSTEM '
                b'"file:///etc/passwd">]><d>&x;</d>'
            },
            drop="word/document.xml",
        ),  # external entity
        _bomb(),  # decompression bomb
        b"PK\x03\x04" + b"\x00" * 100,  # truncated archive
    ],
)
def test_unsafe_or_malformed_docx_archives_are_rejected(content) -> None:
    with pytest.raises(UnsafeUploadError):
        validate_upload("a.docx", DOCX, content)


@pytest.mark.parametrize("content", [b"\xff\xfe invalid", b"text\x00with nul"])
def test_text_uploads_must_be_clean_utf8(content) -> None:
    with pytest.raises(UnsafeUploadError):
        validate_upload("a.txt", "text/plain", content)
