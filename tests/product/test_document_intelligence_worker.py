"""Document intelligence inside the existing ingestion worker (SQLite + Alembic head).

Native PDFs and scripted-OCR images go through the real worker: page extraction, chunking and
then the analysis in the same transaction. Checks: persisted classifications and facts with
page, span, chunk and box provenance; review-required OCR; person attribution from configured
aliases; conservative claim grouping; idempotent reprocessing; no unsafe aggregation.
"""

from __future__ import annotations

import shutil
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import pytest

pytest.importorskip("alembic", reason="Install declared persistence extra; product gate blocks")

from alembic import command
from alembic.config import Config
from scripts import intelligence_fixtures as corpus
from scripts.ocr_fixtures import image_bytes, native_pdf, scan_image
from sqlalchemy import create_engine, func, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session
from tests.security.ocr_helpers import ScriptedEngine, words

from ragops.ingestion.pages import DocumentExtractor
from ragops.intelligence.queries import IntelligenceReader, IntelligenceReviewer
from ragops.knowledge.service import KnowledgeManagementService, LocalDocumentBlobStore
from ragops.persistence.database import transaction
from ragops.persistence.models import (
    ClaimGroup,
    ClaimGroupMember,
    DocumentClassification,
    DocumentFact,
    DocumentIntelligenceRun,
    DocumentPersonLink,
    Tenant,
)
from ragops.vector.index import DeterministicVectorIndex
from ragops.workers.worker import IngestionWorker

pytestmark = pytest.mark.skipif(shutil.which("pdftoppm") is None, reason="poppler missing")
TENANT, OTHER = "tenant-di", "tenant-other"


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    database = create_engine(f"sqlite:///{tmp_path / 'di.db'}")
    with database.begin() as connection:
        cfg = Config("alembic.ini")
        cfg.attributes["connection"] = connection
        command.upgrade(cfg, "head")
    with transaction(database) as session:
        for tenant in (TENANT, OTHER):
            session.add(Tenant(id=tenant, name=tenant))
    yield database
    database.dispose()


class Corpus:
    def __init__(self, engine: Engine, tmp_path: Path) -> None:
        self.engine, self.tmp_path = engine, tmp_path
        self.collections: dict[str, tuple[UUID, UUID]] = {}
        self.versions: dict[str, tuple[UUID, UUID]] = {}

    def _collection(self, session: Session, tenant: str) -> tuple[UUID, UUID]:
        if tenant not in self.collections:
            service = self._service(session, tenant)
            workspace = service.create_workspace(f"W-{tenant}")
            collection = service.create_collection(workspace.id, "Post")
            self.collections[tenant] = (workspace.id, collection.id)
        return self.collections[tenant]

    def _service(self, session: Session, tenant: str) -> KnowledgeManagementService:
        return KnowledgeManagementService(
            session, tenant, "tester", LocalDocumentBlobStore(self.tmp_path / "blobs")
        )

    def ingest(
        self,
        key: str,
        content: bytes,
        name: str,
        mime: str,
        *,
        tenant: str = TENANT,
        ocr: ScriptedEngine | None = None,
    ) -> UUID:
        with transaction(self.engine) as session:
            workspace, collection = self._collection(session, tenant)
            result = self._service(session, tenant).upload(
                workspace, collection, key, key, name, mime, content
            )
            job = IngestionWorker(session).create_job(tenant, result.document.id, result.version.id)
            job_id = job.id
            self.versions[key] = (result.document.id, result.version.id)
        self.process(job_id, tenant, ocr)
        return self.versions[key][1]

    def process(self, job_id: UUID, tenant: str, ocr: ScriptedEngine | None = None) -> None:
        with Session(self.engine) as session:
            IngestionWorker(
                session,
                vector_index=DeterministicVectorIndex(64),
                extractor=DocumentExtractor(engine=ocr or ScriptedEngine()),
            ).process(job_id, tenant)
            session.commit()

    def reprocess(self, key: str, tenant: str = TENANT) -> None:
        document, version = self.versions[key]
        with transaction(self.engine) as session:
            job_id = IngestionWorker(session).create_job(tenant, document, version).id
        self.process(job_id, tenant)

    def letter(self, key: str, tenant: str = TENANT) -> UUID:
        return self.ingest(
            key,
            native_pdf(corpus.by_key(key).pages),
            f"{key}.pdf",
            "application/pdf",
            tenant=tenant,
        )


@pytest.fixture
def docs(engine: Engine, tmp_path: Path) -> Corpus:
    return Corpus(engine, tmp_path)


def persons(engine: Engine, tenant: str = TENANT) -> dict[str, UUID]:
    out: dict[str, UUID] = {}
    with transaction(engine) as session:
        reviewer = IntelligenceReviewer(session, tenant, "reviewer", ("admin",))
        for label, aliases in corpus.PERSONS.items():
            person = reviewer.create_person(label)
            for alias in aliases:
                reviewer.add_alias(person["id"], "name", alias)
            out[label] = UUID(person["id"])
    return out


def reader(session: Session, tenant: str = TENANT, role: str = "admin") -> IntelligenceReader:
    return IntelligenceReader(session, tenant, role)  # type: ignore[arg-type]


def test_native_letter_is_classified_with_page_span_and_chunk_provenance(engine, docs) -> None:
    version = docs.letter("inkasso_c1")
    with Session(engine) as session:
        run = session.scalar(select(DocumentIntelligenceRun))
        assert run is not None and run.document_version_id == version
        assert run.status == "completed" and run.extractor_version == "rules-de-v1"
        primary = session.scalar(
            select(DocumentClassification).where(DocumentClassification.is_primary.is_(True))
        )
        assert primary is not None and primary.document_class == "debt_collection_letter"
        assert primary.taxonomy_version == "doc-taxonomy-v1" and primary.evidence
        total = session.scalar(
            select(DocumentFact).where(DocumentFact.fact_type == "stated_total_claim")
        )
        assert total is not None and total.amount == Decimal("445.00") and total.currency == "EUR"
        assert total.page_number == 1 and total.chunk_index == 0
        assert total.extraction_method == "native_text" and total.regions == []
        assert total.assertion == "document_stated" and total.review_state == "unreviewed"
        assert total.char_end > total.char_start


def test_scanned_letter_facts_carry_ocr_boxes(engine, docs) -> None:
    lines = corpus.by_key("reminder_c1").pages[0]
    ocr = ScriptedEngine(words("\n".join(lines)))
    content = image_bytes(scan_image(list(lines), size=(1240, 1754)), "PNG")
    docs.ingest("scan", content, "scan.png", "image/png", ocr=ocr)
    with Session(engine) as session:
        balance = session.scalar(
            select(DocumentFact).where(DocumentFact.fact_type == "stated_remaining_balance")
        )
        assert balance is not None and balance.extraction_method == "ocr"
        assert balance.ocr_line is not None and len(balance.regions) == 1
        box = balance.regions[0]["box"]
        assert 0 <= box[0] < box[2] <= 1 and 0 <= box[1] < box[3] <= 1
        assert balance.quality_state == "accepted"


def test_low_quality_ocr_is_analysed_but_never_verified(engine, docs) -> None:
    lines = corpus.by_key("reminder_c1").pages[0]
    ocr = ScriptedEngine(words("\n".join(lines), confidence=20.0))
    content = image_bytes(scan_image(list(lines), size=(1240, 1754)), "PNG")
    docs.ingest("blurry", content, "blurry.png", "image/png", ocr=ocr)
    with Session(engine) as session:
        facts = session.scalars(select(DocumentFact)).all()
        assert facts and all(fact.quality_state == "review_required" for fact in facts)
        assert all(fact.review_state == "review_required" for fact in facts)
        run = session.scalar(select(DocumentIntelligenceRun))
        assert run is not None and run.review_required is True


def test_person_attribution_and_ambiguity(engine, docs) -> None:
    ids = persons(engine)
    for key in ("invoice_c1", "invoice_c2_same_amount", "shared_address_ambiguous"):
        docs.letter(key)
    docs.letter("similar_name_not_matched")
    with Session(engine) as session:
        view = reader(session)
        a_docs = {item["title"] for item in view.documents(person_id=str(ids["Person A"]))}
        b_docs = {item["title"] for item in view.documents(person_id=str(ids["Person B"]))}
        assert a_docs == {"invoice_c1"} and b_docs == {"invoice_c2_same_amount"}
        status = {item["title"]: item["debtor_status"] for item in view.documents()}
        assert status["shared_address_ambiguous"] == "ambiguous"
        assert status["similar_name_not_matched"] == "unmatched"
        links = session.scalars(select(DocumentPersonLink)).all()
        shared = docs.versions["shared_address_ambiguous"][1]
        shared_links = [link for link in links if link.document_version_id == shared]
        # Both addressees are proposals for review as debtor, never identified debtors.
        assert {link.state for link in shared_links if link.role == "debtor"} == {"review_required"}
        assert {link.person_id for link in shared_links if link.role == "debtor"} == set(
            ids.values()
        )
        # Being an addressee is recorded separately and does not make anyone the debtor.
        assert {link.state for link in shared_links if link.role == "addressee"} == {"evidenced"}


def test_aliases_added_later_reattribute_existing_documents(engine, docs) -> None:
    docs.letter("invoice_c1")
    ids = persons(engine)
    with Session(engine) as session:
        listed = reader(session).documents(person_id=str(ids["Person A"]))
        assert [item["title"] for item in listed] == ["invoice_c1"]


def test_claim_groups_are_conservative(engine, docs) -> None:
    persons(engine)
    for letter in corpus.LETTERS:
        docs.letter(letter.key)
    with Session(engine) as session:
        groups = session.scalars(select(ClaimGroup)).all()
        members = session.scalars(select(ClaimGroupMember)).all()
        key_of = {version: key for key, (_, version) in docs.versions.items()}
        found: dict[str, set[str]] = {}
        for group in groups:
            found[str(group.id)] = {
                key_of[m.document_version_id] for m in members if m.claim_group_id == group.id
            }
        expected = {frozenset(value) for value in corpus.EXPECTED_CLAIMS.values()}
        assert {frozenset(value) for value in found.values()} == expected
        # Same amount (400 EUR) with different creditor or invoice number stays separate.
        c1 = next(v for v in found.values() if "invoice_c1" in v)
        assert "invoice_c2_same_amount" not in c1 and "invoice_c3_same_creditor_amount" not in c1


def test_claims_summary_never_double_counts(engine, docs) -> None:
    persons(engine)
    for key in ("invoice_c1", "reminder_c1", "inkasso_c1"):
        docs.letter(key)
    with Session(engine) as session:
        summary = reader(session).claims()
    (claim,) = summary["claims"]
    assert len(claim["documents"]) == 3
    assert claim["potentially_current"]["amount"] == corpus.C1_LATEST_TOTAL
    assert claim["potentially_current"]["as_of"] == "2026-02-24"
    roles = {(a["fact_type"], a["role"]) for d in claim["documents"] for a in d["amounts"]}
    assert ("principal_amount", "historical") in roles
    assert summary["consolidated_total"] == {"EUR": corpus.C1_LATEST_TOTAL}
    assert summary["complete"] is True
    assert corpus.C1_NAIVE_SUM not in str(summary)


def test_unresolved_items_make_the_summary_partial(engine, docs) -> None:
    persons(engine)
    for key in ("invoice_c1", "inkasso_c1", "enforcement_notice"):
        docs.letter(key)
    with Session(engine) as session:
        summary = reader(session).claims()
    assert summary["complete"] is False and summary["consolidated_total"] is None
    assert any(item["reason"] == "no_claim_identifier" for item in summary["unresolved"])


def test_paid_claim_is_settled_with_zero_remaining(engine, docs) -> None:
    persons(engine)
    for key in ("invoice_c4", "receipt_c4_paid"):
        docs.letter(key)
    with Session(engine) as session:
        (claim,) = reader(session).claims()["claims"]
    assert claim["settled"] is True and claim["potentially_current"]["amount"] == "0.00"


def test_reprocessing_is_idempotent(engine, docs) -> None:
    persons(engine)
    for key in ("invoice_c1", "inkasso_c1"):
        docs.letter(key)

    def counts() -> tuple[int, ...]:
        with Session(engine) as session:
            return tuple(
                session.scalar(select(func.count()).select_from(model)) or 0
                for model in (
                    DocumentFact,
                    DocumentClassification,
                    DocumentIntelligenceRun,
                    DocumentPersonLink,
                    ClaimGroup,
                    ClaimGroupMember,
                )
            )

    before = counts()
    docs.reprocess("inkasso_c1")
    docs.reprocess("inkasso_c1")
    assert counts() == before
    with Session(engine) as session:
        assert (
            session.scalar(
                select(func.count())
                .select_from(DocumentFact)
                .where(DocumentFact.status != "active")
            )
            == 0
        )


def test_tenants_never_share_groups_or_persons(engine, docs) -> None:
    persons(engine)
    docs.letter("invoice_c1")
    docs.letter("reminder_c1", tenant=OTHER)
    with Session(engine) as session:
        assert len(reader(session).claims()["claims"]) == 1
        other = reader(session, OTHER)
        assert other.persons() == []
        (claim,) = other.claims()["claims"]
        assert [d["title"] for d in claim["documents"]] == ["reminder_c1"]
        groups = session.scalars(select(ClaimGroup)).all()
        assert {group.tenant_id for group in groups} == {TENANT, OTHER}
