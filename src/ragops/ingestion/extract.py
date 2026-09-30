"""Text extraction from uploaded document bytes for the ingestion worker.

Uploads were validated and structurally inspected before they were stored; extraction
reads text only and never executes, renders or resolves anything inside a document.
"""

from __future__ import annotations

import csv
import io
from pathlib import PurePath


class DocumentExtractionError(ValueError):
    """The stored document cannot be turned into text."""


def extract_segments(filename: str, content: bytes) -> list[tuple[int | None, str]]:
    """Return ``(page_number, text)`` segments; only PDF segments carry a page number."""
    suffix = PurePath(filename).suffix.lower()
    try:
        if suffix == ".pdf":
            from pypdf import PdfReader

            reader = PdfReader(io.BytesIO(content), strict=True)
            return [
                (number, page.extract_text() or "")
                for number, page in enumerate(reader.pages, start=1)
            ]
        if suffix == ".docx":
            from docx import Document

            document = Document(io.BytesIO(content))
            return [(None, "\n".join(paragraph.text for paragraph in document.paragraphs))]
        if suffix == ".csv":
            rows = csv.DictReader(io.StringIO(content.decode("utf-8"), newline=""))
            return [
                (
                    None,
                    "\n".join(
                        "; ".join(f"{key}: {value}" for key, value in row.items()) for row in rows
                    ),
                )
            ]
        if suffix in {".md", ".txt"}:
            return [(None, content.decode("utf-8"))]
    except DocumentExtractionError:
        raise
    except Exception as exc:  # noqa: BLE001 - parser errors vary by library and version
        raise DocumentExtractionError("document text could not be extracted") from exc
    raise DocumentExtractionError("unsupported document type")
