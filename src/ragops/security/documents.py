"""Structural inspection of uploaded documents before they are persisted.

Nothing in an upload is ever executed, rendered or resolved. These checks reject files that
are truncated, encrypted, carry active content, exceed archive safety limits or declare XML
entities, so only plain, parseable documents reach storage and indexing.
"""

from __future__ import annotations

import io
import re
import warnings
import zipfile
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any

from ragops.security.upload import UnsafeUploadError

MISMATCH = "file content does not match its type"
ACTIVE_CONTENT = "file is encrypted or contains active content"
ARCHIVE_LIMITS = "archive exceeds safety limits"
IMAGE_LIMITS_EXCEEDED = "image exceeds safety limits"

# Active content and references to external files or remote resources. Plain /URI links are
# inert annotations here: nothing in an upload is ever resolved or followed.
PDF_FORBIDDEN_NAMES = frozenset(
    {
        "/JavaScript",
        "/JS",
        "/Launch",
        "/EmbeddedFile",
        "/EmbeddedFiles",
        "/RichMedia",
        "/XFA",
        "/SubmitForm",
        "/ImportData",
        "/GoToR",
        "/GoToE",
        "/Encrypt",
    }
)
PDF_ACTIVE = re.compile(
    rb"/(JavaScript|JS|Launch|EmbeddedFiles?|RichMedia|XFA|SubmitForm|ImportData|GoToR|GoToE"
    rb"|Encrypt)\b|/FS\s*/URL\b"
)
PDF_NAME_ESCAPE = re.compile(rb"#([0-9A-Fa-f]{2})")
PDF_MAX_PAGES = 1000
PDF_MAX_OBJECTS = 200_000


@dataclass(frozen=True)
class ImageLimits:
    """Bounds checked from the image header before any pixel is decoded."""

    max_pixels: int = 40_000_000
    max_side: int = 20_000
    modes: frozenset[str] = frozenset({"1", "L", "LA", "P", "RGB", "RGBA", "CMYK"})


IMAGE_LIMITS = ImageLimits()
IMAGE_FORMATS = {".png": "PNG", ".jpg": "JPEG", ".jpeg": "JPEG"}
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
    elif suffix in IMAGE_FORMATS:
        inspect_image(suffix, content)
    elif suffix == ".docx":
        _inspect_docx(content)
    else:
        _inspect_text(content)


def inspect_image(suffix: str, content: bytes, limits: ImageLimits = IMAGE_LIMITS) -> None:
    """A single-frame PNG/JPEG within the pixel limits that decodes completely."""
    from PIL import Image, UnidentifiedImageError

    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(content)) as image:
                if image.format != IMAGE_FORMATS[suffix]:
                    raise UnsafeUploadError(MISMATCH)
                width, height = image.size
                if (
                    width < 1
                    or height < 1
                    or max(width, height) > limits.max_side
                    or width * height > limits.max_pixels
                ):
                    raise UnsafeUploadError(IMAGE_LIMITS_EXCEEDED)
                if getattr(image, "n_frames", 1) != 1 or image.mode not in limits.modes:
                    raise UnsafeUploadError(MISMATCH)
                image.load()  # a truncated or corrupt pixel stream fails here
    except UnsafeUploadError:
        raise
    except (Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise UnsafeUploadError(IMAGE_LIMITS_EXCEEDED) from exc
    except (UnidentifiedImageError, OSError, ValueError, SyntaxError, EOFError) as exc:
        raise UnsafeUploadError(MISMATCH) from exc


def _unescaped_names(content: bytes) -> bytes:
    return PDF_NAME_ESCAPE.sub(lambda match: bytes([int(match.group(1), 16)]), content)


def _forbidden_objects(reader: Any) -> bool:
    """Walk every reachable object, including those inside compressed object streams."""
    from pypdf.generic import ArrayObject, DictionaryObject, IndirectObject, NameObject

    stack: list[Any] = [reader.trailer]
    seen: set[tuple[int, int]] = set()
    visited = 0
    while stack:
        item = stack.pop()
        visited += 1
        if visited > PDF_MAX_OBJECTS:
            raise UnsafeUploadError(ARCHIVE_LIMITS)
        if isinstance(item, IndirectObject):
            key = (item.idnum, item.generation)
            if key in seen:
                continue
            seen.add(key)
            stack.append(item.get_object())
        elif isinstance(item, DictionaryObject):
            for name, value in item.items():
                if name in PDF_FORBIDDEN_NAMES or (
                    isinstance(value, NameObject) and value in PDF_FORBIDDEN_NAMES
                ):
                    return True
                if name == "/FS" and isinstance(value, NameObject) and value == "/URL":
                    return True
                stack.append(value)
        elif isinstance(item, ArrayObject):
            stack.extend(item)
        elif isinstance(item, NameObject) and item in PDF_FORBIDDEN_NAMES:
            return True
    return False


def _inspect_pdf(content: bytes) -> None:
    if not content.startswith(b"%PDF-") or b"%%EOF" not in content[-1024:]:
        raise UnsafeUploadError(MISMATCH)
    if PDF_ACTIVE.search(content) or PDF_ACTIVE.search(_unescaped_names(content)):
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
    if reader.is_encrypted or not 1 <= pages <= PDF_MAX_PAGES:
        raise UnsafeUploadError(ACTIVE_CONTENT if reader.is_encrypted else MISMATCH)
    try:
        forbidden = _forbidden_objects(reader)
    except UnsafeUploadError:
        raise
    except Exception as exc:  # noqa: BLE001 - any unreadable object graph is malformed
        raise UnsafeUploadError(MISMATCH) from exc
    if forbidden:
        raise UnsafeUploadError(ACTIVE_CONTENT)


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
