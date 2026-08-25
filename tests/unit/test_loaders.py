from __future__ import annotations

from pathlib import Path

import pytest

from ragops.ingestion.loaders import (
    load_csv_file,
    load_document_text,
    load_docx_file,
    load_pdf_file,
    load_text_file,
)
from ragops.ingestion.pipeline import parse_front_matter

DATA_DIR = Path("data/synthetic/documents")


def test_load_pdf_file_extracts_real_text_via_pdf_parser() -> None:
    text = load_pdf_file(DATA_DIR / "beta_product_guide.pdf")

    assert "document_id" in text
    assert "DOC-BETA" in text


def test_load_docx_file_extracts_real_text_via_docx_parser() -> None:
    text = load_docx_file(DATA_DIR / "alpha_compliance_policy.docx")

    assert "DOC-ALPHA-COMPLIANCE-V2" in text
    metadata, body = parse_front_matter(text)
    assert metadata["document_id"] == "DOC-ALPHA-COMPLIANCE-V2"
    assert body.startswith("Synthetic document.")


def test_load_document_text_dispatches_by_suffix(tmp_path: Path) -> None:
    md_path = tmp_path / "note.md"
    md_path.write_text("hello", encoding="utf-8")
    assert load_document_text(md_path) == load_text_file(md_path)

    csv_path = tmp_path / "table.csv"
    csv_path.write_text("a,b\n1,2\n", encoding="utf-8")
    assert load_document_text(csv_path) == load_csv_file(csv_path)

    pdf_text = load_document_text(DATA_DIR / "beta_product_guide.pdf")
    assert pdf_text == load_pdf_file(DATA_DIR / "beta_product_guide.pdf")

    docx_text = load_document_text(DATA_DIR / "alpha_compliance_policy.docx")
    assert docx_text == load_docx_file(DATA_DIR / "alpha_compliance_policy.docx")


def test_load_document_text_rejects_unsupported_suffix(tmp_path: Path) -> None:
    unsupported = tmp_path / "file.xyz"
    unsupported.write_text("data", encoding="utf-8")

    with pytest.raises(ValueError, match="unsupported document type"):
        load_document_text(unsupported)
