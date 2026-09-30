"""Structural inspection of uploaded documents before they are persisted.

Nothing in an upload is ever executed, rendered or resolved. These checks reject files that
are truncated, encrypted, carry active content, exceed archive safety limits or declare XML
entities, so only plain, parseable documents reach storage and indexing.
"""

from __future__ import annotations

import io
import re
import zipfile
from pathlib import PurePosixPath

from ragops.security.upload import UnsafeUploadError

MISMATCH = "file content does not match its type"
ACTIVE_CONTENT = "file is encrypted or contains active content"
ARCHIVE_LIMITS = "archive exceeds safety limits"

PDF_ACTIVE = re.compile(
    rb"/(JavaScript|JS|Launch|EmbeddedFiles?|RichMedia|XFA|SubmitForm|ImportData|GoToR|Encrypt)\b"
)
DOCX_REQUIRED = frozenset({"[Content_Types].xml", "word/document.xml"})
DOCX_MAX_MEMBERS = 500
DOCX_MAX_TOTAL_BYTES = 20_000_000
DOCX_MAX_MEMBER_BYTES = 10_000_000
DOCX_MAX_RATIO = 100
DOCX_FORBIDDEN_SUFFIXES = (".bin", ".exe", ".dll", ".js", ".vbs", ".ps1", ".sh")
XML_MEMBERS = ("[Content_Types].xml", "word/document.xml")


def inspect_document(suffix: str, content: bytes) -> None:
    """Raise UnsafeUploadError unless the content is a safe document of its type."""
    if suffix == ".pdf":
        _inspect_pdf(content)
    elif suffix == ".docx":
        _inspect_docx(content)
    else:
        _inspect_text(content)


def _inspect_pdf(content: bytes) -> None:
    if not content.startswith(b"%PDF-") or b"%%EOF" not in content[-1024:]:
        raise UnsafeUploadError(MISMATCH)
    if PDF_ACTIVE.search(content):
        raise UnsafeUploadError(ACTIVE_CONTENT)
    if b"PK\x05\x06" in content or b"PK\x03\x04" in content:
        raise UnsafeUploadError(MISMATCH)  # PDF/ZIP polyglot
    from pypdf import PdfReader
    from pypdf.errors import PdfReadError

    try:
        reader = PdfReader(io.BytesIO(content), strict=True)
        pages = len(reader.pages)
    except (PdfReadError, ValueError, KeyError, TypeError, OSError) as exc:
        raise UnsafeUploadError(MISMATCH) from exc
    if reader.is_encrypted or not 1 <= pages <= 1000:
        raise UnsafeUploadError(ACTIVE_CONTENT if reader.is_encrypted else MISMATCH)


def _safe_member_name(name: str) -> bool:
    path = PurePosixPath(name)
    return (
        bool(name)
        and not name.startswith("/")
        and "\\" not in name
        and ":" not in name
        and ".." not in path.parts
        and "\x00" not in name
    )


def _inspect_docx(content: bytes) -> None:
    try:
        archive = zipfile.ZipFile(io.BytesIO(content))
        members = archive.infolist()
    except (zipfile.BadZipFile, ValueError, OSError) as exc:
        raise UnsafeUploadError(MISMATCH) from exc
    names = [member.filename for member in members]
    if len(members) > DOCX_MAX_MEMBERS or len(set(names)) != len(names):
        raise UnsafeUploadError(ARCHIVE_LIMITS)
    total = 0
    for member in members:
        if not _safe_member_name(member.filename):
            raise UnsafeUploadError(ARCHIVE_LIMITS)
        if member.filename.lower().endswith(DOCX_FORBIDDEN_SUFFIXES):
            raise UnsafeUploadError(ACTIVE_CONTENT)
        total += member.file_size
        ratio = member.file_size / max(member.compress_size, 1)
        if (
            member.file_size > DOCX_MAX_MEMBER_BYTES
            or total > DOCX_MAX_TOTAL_BYTES
            or (member.file_size > 1024 and ratio > DOCX_MAX_RATIO)
        ):
            raise UnsafeUploadError(ARCHIVE_LIMITS)
    if not DOCX_REQUIRED <= set(names):
        raise UnsafeUploadError(MISMATCH)
    from defusedxml import DefusedXmlException
    from defusedxml.ElementTree import fromstring

    for name in XML_MEMBERS:
        try:
            # Declared sizes were bounded above; the read is capped again defensively.
            with archive.open(name) as handle:
                data = handle.read(DOCX_MAX_MEMBER_BYTES + 1)
            fromstring(data, forbid_dtd=True, forbid_entities=True, forbid_external=True)
        except (DefusedXmlException, zipfile.BadZipFile, ValueError, OSError) as exc:
            raise UnsafeUploadError(ACTIVE_CONTENT) from exc
        except Exception as exc:  # noqa: BLE001 - any XML parse error means malformed
            raise UnsafeUploadError(MISMATCH) from exc


def _inspect_text(content: bytes) -> None:
    if b"\x00" in content:
        raise UnsafeUploadError(MISMATCH)
    try:
        content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise UnsafeUploadError(MISMATCH) from exc
