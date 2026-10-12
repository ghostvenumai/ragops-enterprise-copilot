"""PostgreSQL: document intelligence under concurrency and the 0010 migration (ENT-11.8).

Skipped unless RAGOPS_TEST_DATABASE_URL points at an isolated ragops_test_* database. Each
test creates and drops its own database and touches nothing else.
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from uuid import UUID

import pytest

pytest.importorskip("alembic", reason="Install declared persistence extra; product gate blocks")

from alembic import command
from alembic.config import Config
from scripts import intelligence_fixtures as corpus
from sqlalchemy import func, inspect, select
from sqlalchemy.orm import Session
from tests.integration.test_query_finops_postgres import database  # noqa: F401

from ragops.intelligence.queries import Conflict, IntelligenceReviewer
from ragops.intelligence.rules import analyze, lines_from_text
from ragops.intelligence.service import persist_analysis
from ragops.knowledge.service import KnowledgeManagementService, LocalDocumentBlobStore
from ragops.persistence.database import transaction
from ragops.persistence.models import (
    ClaimGroup,
    ClaimGroupMember,
    DocumentFact,
    IntelligenceReviewEvent,
    Tenant,
)

pytestmark = pytest.mark.integration
WORKERS = 10
TABLES = {
    "person_profiles",
    "person_aliases",
    "document_intelligence_runs",
    "document_classifications",
    "document_facts",
    "document_person_links",
    "claim_groups",
    "claim_group_members",
    "intelligence_review_events",
}


def seed(engine, tmp_path, keys: list[str], tenant: str = "tenant-a") -> dict[str, tuple]:
    out = {}
    with transaction(engine) as session:
        session.add(Tenant(id=tenant, name=tenant))
        session.flush()
        service = KnowledgeManagementService(
            session, tenant, "tester", LocalDocumentBlobStore(tmp_path / "blobs")
        )
        workspace = service.create_workspace("W")
        collection = service.create_collection(workspace.id, "C")
        for key in keys:
            result = service.upload(
                workspace.id, collection.id, key, key, f"{key}.txt", "text/plain", key.encode()
            )
            out[key] = (result.document.id, result.version.id)
    return out


def analyse(engine, tenant: str, document_id: UUID, version_id: UUID, key: str) -> None:
    from ragops.persistence.models import Document, DocumentVersion

    analysis = analyze(lines_from_text(corpus.by_key(key).pages))
    with transaction(engine) as session:
        document = session.get(Document, {"tenant_id": tenant, "id": document_id})
        version = session.get(DocumentVersion, {"tenant_id": tenant, "id": version_id})
        persist_analysis(session, document, version, analysis)


def test_migration_0010_upgrades_and_downgrades_cleanly(database) -> None:  # noqa: F811
    assert TABLES <= set(inspect(database).get_table_names())
    with database.begin() as connection:
        cfg = Config("alembic.ini")
        cfg.attributes["connection"] = connection
        command.downgrade(cfg, "0009_ocr_extraction")
    names = set(inspect(database).get_table_names())
    assert not TABLES & names and "document_page_extractions" in names
    with database.begin() as connection:
        cfg = Config("alembic.ini")
        cfg.attributes["connection"] = connection
        command.upgrade(cfg, "head")
    assert TABLES <= set(inspect(database).get_table_names())


def test_ten_concurrent_corrections_apply_exactly_once(database, tmp_path) -> None:  # noqa: F811
    seeded = seed(database, tmp_path, ["inkasso_c1"])
    analyse(database, "tenant-a", *seeded["inkasso_c1"], "inkasso_c1")
    with Session(database) as session:
        fact = session.scalar(
            select(DocumentFact).where(DocumentFact.fact_type == "stated_total_claim")
        )
        assert fact is not None
        fact_id, version = fact.id, fact.review_version
    barrier = threading.Barrier(WORKERS)

    def one(index: int) -> str:
        with transaction(database) as session:
            reviewer = IntelligenceReviewer(session, "tenant-a", f"reviewer-{index}", ("admin",))
            barrier.wait(timeout=10)
            try:
                reviewer.review(
                    "fact",
                    str(fact_id),
                    "correct",
                    version,
                    "extraction_error",
                    value={"amount": f"{450 + index}.00", "currency": "EUR"},
                )
            except Conflict:
                return "conflict"
        return "applied"

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        outcomes = list(pool.map(one, range(WORKERS)))
    assert outcomes.count("applied") == 1 and outcomes.count("conflict") == WORKERS - 1
    with Session(database) as session:
        events = session.scalars(select(IntelligenceReviewEvent)).all()
        assert len(events) == 1
        stored = session.get(DocumentFact, {"tenant_id": "tenant-a", "id": fact_id})
        assert stored is not None and str(stored.amount) == "445.00"
        assert stored.review_version == version + 1


def test_concurrent_analyses_create_each_claim_group_once(database, tmp_path) -> None:  # noqa: F811
    keys = ["invoice_c1", "reminder_c1", "inkasso_c1"]
    seeded = seed(database, tmp_path, keys)
    barrier = threading.Barrier(len(keys) * 3)

    def one(key: str) -> None:
        barrier.wait(timeout=10)
        analyse(database, "tenant-a", *seeded[key], key)

    with ThreadPoolExecutor(max_workers=len(keys) * 3) as pool:
        for future in [pool.submit(one, key) for key in keys * 3]:
            future.result()
    with Session(database) as session:
        assert session.scalar(select(func.count()).select_from(ClaimGroup)) == 1
        assert session.scalar(select(func.count()).select_from(ClaimGroupMember)) == 3
        facts = session.scalar(select(func.count()).select_from(DocumentFact))
    for key in keys:
        analyse(database, "tenant-a", *seeded[key], key)
    with Session(database) as session:
        assert session.scalar(select(func.count()).select_from(DocumentFact)) == facts
