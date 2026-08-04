from __future__ import annotations

from pathlib import Path

from ragops.ingestion.pipeline import ingest_directory
from ragops.retrieval.hybrid import HybridRetriever
from ragops.storage.models import QueryUser

DATA_DIR = Path("data/synthetic/documents")


def test_ingestion_extracts_metadata_and_prompt_injection_flags() -> None:
    chunks = ingest_directory(DATA_DIR)

    assert chunks
    atlas = next(chunk for chunk in chunks if chunk.document_id == "DOC-ALPHA-ATLAS-V2")
    injection = next(chunk for chunk in chunks if chunk.document_id == "DOC-ALPHA-INJECTION-001")
    pricing = next(chunk for chunk in chunks if chunk.document_id == "DOC-ALPHA-PRICING-V2")
    assert atlas.metadata.tenant_id == "tenant-alpha"
    assert atlas.metadata.version == "v2"
    assert injection.has_prompt_injection
    assert pricing.metadata.classification == "pricing"


def test_hybrid_search_enforces_tenant_and_access_filters() -> None:
    chunks = ingest_directory(DATA_DIR)
    retriever = HybridRetriever(chunks)
    alpha_user = QueryUser(user_id="u1", tenant_id="tenant-alpha", role="sales")

    results = retriever.search("Beta Atlas Product Guide", alpha_user, top_k=10)

    assert results
    assert all(result.chunk.tenant_id == "tenant-alpha" for result in results)


def test_hybrid_search_finds_current_atlas_guide() -> None:
    chunks = ingest_directory(DATA_DIR)
    retriever = HybridRetriever(chunks)
    user = QueryUser(user_id="u1", tenant_id="tenant-alpha", role="sales")

    results = retriever.search("Atlas Control Plane 99.9 SLA", user, top_k=3)

    assert any(
        "Atlas Control Plane Product Guide v2" in result.chunk.citation for result in results
    )


def test_hybrid_search_finds_database_architecture() -> None:
    chunks = ingest_directory(DATA_DIR)
    retriever = HybridRetriever(chunks)
    user = QueryUser(user_id="u1", tenant_id="tenant-alpha", role="operations")

    results = retriever.search("Welche Datenbank wird genutzt?", user, top_k=5)

    assert results
    assert results[0].chunk.document_id == "DOC-ALPHA-ARCH-V1"


def test_hybrid_search_returns_nothing_without_lexical_evidence() -> None:
    chunks = ingest_directory(DATA_DIR)
    retriever = HybridRetriever(chunks)
    user = QueryUser(user_id="u1", tenant_id="tenant-alpha", role="sales")

    results = retriever.search("Welche Kaffeemaschine steht im Büro?", user, top_k=5)

    assert results == []
