"""Document loaders for local deterministic ingestion."""

from __future__ import annotations

import csv
from pathlib import Path

from docx import Document
from pypdf import PdfReader


def load_text_file(path: Path) -> str:
    data = path.read_bytes()
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("latin-1", errors="replace")


def load_csv_file(path: Path) -> str:
    with path.open("r", encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    rendered = []
    for row in rows:
        rendered.append("; ".join(f"{key}: {value}" for key, value in row.items()))
    return "\n".join(rendered)


def load_pdf_file(path: Path) -> str:
    reader = PdfReader(str(path))
    pages = [page.extract_text() or "" for page in reader.pages]
    return "\n".join(pages)


def load_docx_file(path: Path) -> str:
    document = Document(str(path))
    return "\n".join(paragraph.text for paragraph in document.paragraphs)


def load_document_text(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".csv":
        return load_csv_file(path)
    if suffix == ".pdf":
        return load_pdf_file(path)
    if suffix == ".docx":
        return load_docx_file(path)
    if suffix in {".md", ".txt"}:
        return load_text_file(path)
    raise ValueError(f"unsupported document type: {suffix}")
