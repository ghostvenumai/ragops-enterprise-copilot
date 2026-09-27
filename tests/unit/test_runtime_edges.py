from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from ragops.config.settings import Settings
from ragops.ingestion.loaders import load_document_text, load_text_file
from ragops.ingestion.normalization import chunk_text, normalize_text, tokenize
from ragops.ingestion.pipeline import ingest_directory, parse_date, parse_front_matter
from ragops.llm.providers import (
    AzureOpenAIProvider,
    DeterministicTestProvider,
    OpenAIProvider,
    provider_from_env,
)
from ragops.security.upload import UnsafeUploadError, safe_filename, validate_document_path
from ragops.storage.models import Citation


def test_deterministic_provider_handles_evidence_and_abstention() -> None:
    provider = DeterministicTestProvider()
    refusal = provider.generate("unknown", [], "")
    assert refusal.abstained
    assert refusal.usage.total_tokens > 0

    citation = Citation(
        source_id="DOC-1#1",
        title="Synthetic source",
        tenant_id="tenant-alpha",
        snippet="synthetic evidence",
        score=1.0,
    )
    response = provider.generate("known", [citation], citation.snippet)
    assert not response.abstained
    assert "[DOC-1#1]" in response.text
    assert "synthetic evidence" not in response.text
    assert "verfügbaren synthetischen Evidenz" in response.text
    assert "Vollständigkeit der Quellen" in response.text
    assert response.used_source_ids == ("DOC-1#1",)


def test_deterministic_provider_covers_safe_synthetic_documents() -> None:
    chunks = ingest_directory(Path("data/synthetic/documents"))
    safe_titles = {chunk.metadata.title for chunk in chunks if not chunk.has_prompt_injection}

    assert safe_titles <= DeterministicTestProvider._GERMAN_SUMMARIES.keys()


def test_provider_selection_requires_explicit_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("RAGOPS_LLM_PROVIDER", raising=False)
    assert isinstance(provider_from_env(), DeterministicTestProvider)

    monkeypatch.setenv("RAGOPS_LLM_PROVIDER", "openai")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_MODEL", raising=False)
    with pytest.raises(RuntimeError):
        provider_from_env()

    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-test-key")
    monkeypatch.setenv("OPENAI_MODEL", "synthetic-model")
    openai_provider = provider_from_env()
    assert isinstance(openai_provider, OpenAIProvider)
    assert openai_provider.model == "synthetic-model"

    monkeypatch.setenv("RAGOPS_LLM_PROVIDER", "azure_openai")
    for name in (
        "AZURE_OPENAI_API_KEY",
        "AZURE_OPENAI_ENDPOINT",
        "AZURE_OPENAI_DEPLOYMENT",
        "AZURE_OPENAI_API_VERSION",
    ):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(RuntimeError):
        provider_from_env()

    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "synthetic-test-key")
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://example.invalid")
    monkeypatch.setenv("AZURE_OPENAI_DEPLOYMENT", "synthetic-deployment")
    monkeypatch.setenv("AZURE_OPENAI_API_VERSION", "2026-01-01")
    azure_provider = provider_from_env()
    assert isinstance(azure_provider, AzureOpenAIProvider)
    assert azure_provider.model == "synthetic-deployment"


def test_settings_and_ingestion_edge_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RAGOPS_ENV", "test")
    monkeypatch.setenv("RAGOPS_DATA_DIR", "data/synthetic")
    monkeypatch.setenv("RAGOPS_EVIDENCE_DIR", str(tmp_path))
    monkeypatch.setenv("RAGOPS_MAX_INPUT_CHARS", "123")
    settings = Settings.from_env()
    assert settings.environment == "test"
    assert settings.max_input_chars == 123

    assert normalize_text(" A\x00  B ") == "A B"
    assert tokenize("Verfügbarkeit und Maßnahmen") == ["verfügbarkeit", "und", "maßnahmen"]
    assert tokenize("Compliance-Richtlinien") == ["compliance", "richtlinien"]
    assert chunk_text("") == []
    with pytest.raises(ValueError):
        chunk_text("text", chunk_size=0)
    with pytest.raises(ValueError):
        chunk_text("text", chunk_size=10, overlap=10)
    assert parse_front_matter("---\nbroken")[0] == {}
    assert parse_date(None, date(2026, 1, 1)) == date(2026, 1, 1)

    manifest = tmp_path / "manifest.json"
    chunks = ingest_directory(Path("data/synthetic/documents"), manifest)
    assert chunks
    assert json.loads(manifest.read_text(encoding="utf-8"))


def test_loader_and_upload_edge_paths(tmp_path: Path) -> None:
    latin = tmp_path / "latin.txt"
    latin.write_bytes("synthetisch: gr\xfc\xdfe".encode("latin-1"))
    assert "gruesse" not in load_text_file(latin)
    assert "gr\u00fc\u00dfe" in load_text_file(latin)

    unsupported = tmp_path / "payload.exe"
    unsupported.write_text("synthetic", encoding="utf-8")
    with pytest.raises(ValueError):
        load_document_text(unsupported)
    with pytest.raises(UnsafeUploadError):
        validate_document_path(unsupported, tmp_path)
    with pytest.raises(UnsafeUploadError):
        validate_document_path(latin, tmp_path, max_size=1)
    assert safe_filename("contract.md") == "contract.md"
    with pytest.raises(UnsafeUploadError):
        safe_filename("")
