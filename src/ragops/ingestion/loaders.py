"""Document loaders for local deterministic ingestion."""

from __future__ import annotations

import csv
from pathlib import Path


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


def load_document_text(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".csv":
        return load_csv_file(path)
    if suffix in {".md", ".txt", ".pdf", ".docx"}:
        return load_text_file(path)
    raise ValueError(f"unsupported document type: {suffix}")
