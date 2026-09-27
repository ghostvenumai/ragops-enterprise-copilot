"""Safe file intake checks for document ingestion."""

from __future__ import annotations

from pathlib import Path

ALLOWED_EXTENSIONS = {".pdf", ".docx", ".md", ".txt", ".csv"}
MAX_FILE_SIZE_BYTES = 2_000_000


class UnsafeUploadError(ValueError):
    """Raised when a document upload violates intake policy."""


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


def validate_document_path(path: Path, base_dir: Path, max_size: int = MAX_FILE_SIZE_BYTES) -> Path:
    resolved_base = base_dir.resolve()
    resolved_path = path.resolve()
    if resolved_base not in resolved_path.parents and resolved_path != resolved_base:
        raise UnsafeUploadError("path traversal blocked")
    if resolved_path.suffix.lower() not in ALLOWED_EXTENSIONS:
        raise UnsafeUploadError("file extension is not allowed")
    if resolved_path.stat().st_size > max_size:
        raise UnsafeUploadError("file exceeds size limit")
    return resolved_path
