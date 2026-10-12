"""Safe file intake checks for document ingestion."""

from __future__ import annotations

from pathlib import Path

ALLOWED_EXTENSIONS = {".pdf", ".docx", ".md", ".txt", ".csv", ".jpg", ".jpeg", ".png"}
# Text formats stay small. Scans are larger: a 300 dpi grayscale page is about 0.3-0.6 MB, so
# 12 MB holds a realistic letter bundle; pages, pixels and OCR time are bounded separately.
MAX_FILE_SIZE_BYTES = 2_000_000
MAX_SCAN_FILE_SIZE_BYTES = 12_000_000
SCAN_EXTENSIONS = frozenset({".pdf", ".jpg", ".jpeg", ".png"})
IMAGE_EXTENSIONS = frozenset({".jpg", ".jpeg", ".png"})


class UnsafeUploadError(ValueError):
    """Raised when a document upload violates intake policy."""


def max_upload_bytes(suffix: str) -> int:
    """The compressed input limit of one upload of this type."""
    return MAX_SCAN_FILE_SIZE_BYTES if suffix.lower() in SCAN_EXTENSIONS else MAX_FILE_SIZE_BYTES


def safe_filename(name: str) -> str:
    candidate = Path(name)
    if (
        candidate.name != name
        or ".." in candidate.parts
        or name.strip() in {"", ".", ".."}
        or name != name.strip()
        or any(ord(char) < 32 or ord(char) == 127 for char in name)
        or any(char in name for char in ("\\", ":"))
        or len(name.encode("utf-8")) > 255
    ):
        raise UnsafeUploadError("unsafe file name")
    return candidate.name


def validate_document_path(path: Path, base_dir: Path, max_size: int | None = None) -> Path:
    resolved_base = base_dir.resolve()
    resolved_path = path.resolve()
    if resolved_base not in resolved_path.parents and resolved_path != resolved_base:
        raise UnsafeUploadError("path traversal blocked")
    if resolved_path.suffix.lower() not in ALLOWED_EXTENSIONS:
        raise UnsafeUploadError("file extension is not allowed")
    limit = max_upload_bytes(resolved_path.suffix) if max_size is None else max_size
    if resolved_path.stat().st_size > limit:
        raise UnsafeUploadError("file exceeds size limit")
    return resolved_path
