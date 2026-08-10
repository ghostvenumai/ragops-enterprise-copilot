from __future__ import annotations

from pathlib import Path

import pytest

from ragops.ingestion.pipeline import ingest_directory
from ragops.llm.providers import DeterministicTestProvider
from ragops.retrieval.hybrid import HybridRetriever
from ragops.storage.models import QueryUser

DATA_DIR = Path("data/synthetic/documents")


@pytest.mark.parametrize(
    ("question", "tenant_id", "role", "expected_document_id"),
    [
        (
            "Welche Kanäle und Consent-Regeln nennt der Marketing Campaign Guide?",
            "tenant-alpha",
            "sales",
            "DOC-ALPHA-MARKETING-GUIDE",
        ),
        (
            "Welche Kampagnen sind 2026 mit wie vielen qualified Leads geplant?",
            "tenant-alpha",
            "sales",
            "DOC-ALPHA-MARKETING-CAMPAIGNS",
        ),
        (
            "Wie schützt der Copilot Mandantendaten und wann verweigert er Antworten?",
            "tenant-alpha",
            "support",
            "DOC-ALPHA-SECURITY-OVERVIEW",
        ),
        (
            "Welche Schritte umfasst das Marketing Playbook für Account-Kampagnen?",
            "tenant-beta",
            "sales",
            "DOC-BETA-MARKETING-PLAYBOOK",
        ),
        (
            "Wie lange dauert die Standard-Provisionierung einer Bestellung?",
            "tenant-beta",
            "operations",
            "DOC-BETA-ORDER-FULFILLMENT",
        ),
        (
            "Welche Lieferfristen gelten für Hardware-Sendungen?",
            "tenant-gamma",
            "operations",
            "DOC-GAMMA-LOGISTICS-POLICY",
        ),
        (
            "Welche Regeln gelten für Event-Leads und Webinar-Follow-ups?",
            "tenant-gamma",
            "sales",
            "DOC-GAMMA-MARKETING-EVENTS",
        ),
    ],
)
def test_themed_documents_are_retrievable(
    question: str, tenant_id: str, role: str, expected_document_id: str
) -> None:
    chunks = ingest_directory(DATA_DIR)
    retriever = HybridRetriever(chunks)
    user = QueryUser(user_id="u1", tenant_id=tenant_id, role=role)  # type: ignore[arg-type]

    results = retriever.search(question, user, top_k=5)

    assert expected_document_id in {result.chunk.document_id for result in results}


def test_themed_documents_have_german_summaries() -> None:
    chunks = ingest_directory(DATA_DIR)
    themed_titles = {
        chunk.metadata.title
        for chunk in chunks
        if chunk.document_id
        in {
            "DOC-ALPHA-MARKETING-GUIDE",
            "DOC-ALPHA-MARKETING-CAMPAIGNS",
            "DOC-ALPHA-SECURITY-OVERVIEW",
            "DOC-BETA-MARKETING-PLAYBOOK",
            "DOC-BETA-ORDER-FULFILLMENT",
            "DOC-GAMMA-MARKETING-EVENTS",
            "DOC-GAMMA-LOGISTICS-POLICY",
        }
    }
    assert themed_titles <= DeterministicTestProvider._GERMAN_SUMMARIES.keys()


def test_gamma_injection_document_is_flagged() -> None:
    chunks = ingest_directory(DATA_DIR)
    injection = next(chunk for chunk in chunks if chunk.document_id == "DOC-GAMMA-INJECTION-001")
    assert injection.has_prompt_injection
