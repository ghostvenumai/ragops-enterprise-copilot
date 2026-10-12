"""Structured document-intelligence API: authentication, tenancy, roles, review integrity."""

from __future__ import annotations

import shutil
from collections.abc import Iterator
from pathlib import Path
from uuid import UUID, uuid4

import pytest

pytest.importorskip("alembic", reason="Install declared persistence extra; product gate blocks")

from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from scripts import intelligence_fixtures as corpus
from scripts.ocr_fixtures import native_pdf
from sqlalchemy import create_engine, select, update
from sqlalchemy.orm import Session
from tests.security.ocr_helpers import ScriptedEngine
from tests.security.oidc_helpers import AUDIENCE, ISSUER, bearer

from ragops.api.app import create_app
from ragops.config.settings import Settings
from ragops.ingestion.pages import DocumentExtractor
from ragops.knowledge.service import KnowledgeManagementService, LocalDocumentBlobStore
from ragops.persistence.database import transaction
from ragops.persistence.models import Document, DocumentFact, IntelligenceReviewEvent, Tenant
from ragops.vector.index import DeterministicVectorIndex
from ragops.workers.worker import IngestionWorker

pytestmark = pytest.mark.skipif(shutil.which("pdftoppm") is None, reason="poppler missing")
BASE = "/v1/intelligence"


@pytest.fixture
def setup(keys, tmp_path: Path, monkeypatch) -> Iterator[dict[str, object]]:
    url = f"sqlite:///{tmp_path / 'di-api.db'}"
    engine = create_engine(url)
    with engine.begin() as connection:
        cfg = Config("alembic.ini")
        cfg.attributes["connection"] = connection
        command.upgrade(cfg, "head")
    collections: dict[str, tuple[str, str]] = {}
    with transaction(engine) as session:
        for tenant in ("tenant-alpha", "tenant-beta"):
            session.add(Tenant(id=tenant, name=tenant))
            session.flush()
            service = KnowledgeManagementService(
                session, tenant, "admin", LocalDocumentBlobStore(tmp_path / "data" / "blobs")
            )
            workspace = service.create_workspace("W")
            collection = service.create_collection(workspace.id, "C")
            collections[tenant] = (str(workspace.id), str(collection.id))
    monkeypatch.setenv("RAGOPS_DATABASE_URL", url)
    monkeypatch.setenv("RAGOPS_ENV", "test")
    settings = Settings(
        environment="test",
        identity_provider="oidc",
        oidc_issuer=ISSUER,
        oidc_audience=AUDIENCE,
        oidc_public_key=keys[1],
        data_dir=tmp_path / "data",
        evidence_dir=tmp_path / "evidence",
    )
    with TestClient(create_app(settings), raise_server_exceptions=False) as client:
        yield {
            "client": client,
            "engine": engine,
            "collections": collections,
            "alpha": bearer(keys[0], "tenant-alpha", ["admin"]),
            "compliance": bearer(keys[0], "tenant-alpha", ["compliance"]),
            "viewer": bearer(keys[0], "tenant-alpha", ["viewer"]),
            "beta": bearer(keys[0], "tenant-beta", ["admin"]),
        }
    engine.dispose()


def ingest(setup, key: str, tenant: str = "tenant-alpha", headers: str = "alpha") -> dict:
    workspace, collection = setup["collections"][tenant]
    response = setup["client"].post(
        "/v1/documents/upload",
        data={
            "workspace_id": workspace,
            "collection_id": collection,
            "title": key,
            "logical_document_key": key,
        },
        files={"file": (f"{key}.pdf", native_pdf(corpus.by_key(key).pages), "application/pdf")},
        headers=setup[headers],
    )
    assert response.status_code == 202, response.text
    accepted = response.json()
    with Session(setup["engine"]) as session:
        IngestionWorker(
            session,
            vector_index=DeterministicVectorIndex(64),
            extractor=DocumentExtractor(engine=ScriptedEngine()),
        ).process(UUID(accepted["job_id"]), tenant)
        session.commit()
    return accepted


def make_persons(setup, headers: str = "alpha") -> dict[str, str]:
    client, out = setup["client"], {}
    for label, aliases in corpus.PERSONS.items():
        created = client.post(f"{BASE}/persons", json={"label": label}, headers=setup[headers])
        assert created.status_code == 201, created.text
        out[label] = created.json()["id"]
        for alias in aliases:
            added = client.post(
                f"{BASE}/persons/{out[label]}/aliases",
                json={"kind": "name", "value": alias},
                headers=setup[headers],
            )
            assert added.status_code == 201, added.text
    return out


def fact_of(setup, document_id: str, fact_type: str, headers: str = "alpha") -> dict:
    detail = setup["client"].get(f"{BASE}/documents/{document_id}", headers=setup[headers])
    assert detail.status_code == 200, detail.text
    return next(fact for fact in detail.json()["facts"] if fact["fact_type"] == fact_type)


def test_every_route_requires_authentication(setup) -> None:
    client = setup["client"]
    some = str(uuid4())
    for method, path in (
        ("GET", f"{BASE}/documents"),
        ("GET", f"{BASE}/documents/{some}"),
        ("GET", f"{BASE}/creditors"),
        ("GET", f"{BASE}/claims"),
        ("GET", f"{BASE}/deadlines"),
        ("GET", f"{BASE}/findings"),
        ("GET", f"{BASE}/facts/{some}"),
        ("GET", f"{BASE}/persons"),
        ("POST", f"{BASE}/persons"),
        ("POST", f"{BASE}/reviews"),
        ("POST", f"{BASE}/documents/{some}/reprocess"),
    ):
        assert client.request(method, path).status_code == 401, path


def test_structured_queries_answer_from_postgres_with_sources(setup) -> None:
    client = setup["client"]
    ids = make_persons(setup)
    accepted = {key: ingest(setup, key) for key in ("invoice_c1", "inkasso_c1", "court_c2")}
    listed = client.get(
        f"{BASE}/documents?classification=debt_collection_letter", headers=setup["alpha"]
    )
    assert [d["document_id"] for d in listed.json()] == [accepted["inkasso_c1"]["document_id"]]
    mine = client.get(f"{BASE}/documents?person_id={ids['Person A']}", headers=setup["alpha"])
    assert {d["title"] for d in mine.json()} == {"invoice_c1", "inkasso_c1"}
    creditors = client.get(f"{BASE}/creditors", headers=setup["alpha"]).json()
    names = {item["creditor"] for item in creditors}
    assert names == {"versandhaus lindenhof kg", "fernwärme beispieltal ag"}
    deadlines = client.get(f"{BASE}/deadlines", headers=setup["alpha"]).json()
    assert {item["date"] for item in deadlines} >= {"2026-01-26", "2026-03-10"}
    total = fact_of(setup, accepted["inkasso_c1"]["document_id"], "stated_total_claim")
    source = client.get(f"{BASE}/facts/{total['id']}", headers=setup["alpha"]).json()
    assert source["amount"] == "445.00" and source["currency"] == "EUR"
    assert source["source"]["page_number"] == 1
    assert source["source"]["document_version_id"] == accepted["inkasso_c1"]["version_id"]
    assert source["source"]["raw_value"] == "445,00 EUR"
    assert source["assertion"] == "document_stated"


def test_cross_tenant_access_is_indistinguishable_from_missing(setup) -> None:
    client = setup["client"]
    accepted = ingest(setup, "inkasso_c1")
    total = fact_of(setup, accepted["document_id"], "stated_total_claim")
    beta = setup["beta"]
    assert (
        client.get(f"{BASE}/documents/{accepted['document_id']}", headers=beta).status_code == 404
    )
    assert client.get(f"{BASE}/facts/{total['id']}", headers=beta).status_code == 404
    assert client.get(f"{BASE}/documents", headers=beta).json() == []
    assert client.get(f"{BASE}/claims", headers=beta).json()["claims"] == []
    assert client.get(f"{BASE}/creditors", headers=beta).json() == []
    review = client.post(
        f"{BASE}/reviews",
        json={
            "entity_type": "fact",
            "entity_id": total["id"],
            "action": "confirm",
            "expected_version": total["review_version"],
            "reason_category": "verified_against_source",
        },
        headers=beta,
    )
    assert review.status_code == 404


def test_access_levels_and_roles_are_enforced(setup) -> None:
    client = setup["client"]
    accepted = ingest(setup, "inkasso_c1")
    total = fact_of(setup, accepted["document_id"], "stated_total_claim")
    viewer = setup["viewer"]
    # A viewer reads internal documents but cannot review, manage persons or reprocess.
    assert (
        client.get(f"{BASE}/documents/{accepted['document_id']}", headers=viewer).status_code == 200
    )
    body = {
        "entity_type": "fact",
        "entity_id": total["id"],
        "action": "confirm",
        "expected_version": total["review_version"],
        "reason_category": "verified_against_source",
    }
    assert client.post(f"{BASE}/reviews", json=body, headers=viewer).status_code == 403
    assert client.post(f"{BASE}/persons", json={"label": "X"}, headers=viewer).status_code == 403
    reprocess = f"{BASE}/documents/{accepted['document_id']}/reprocess"
    assert client.post(reprocess, headers=viewer).status_code == 403
    # Above the caller's access level the document does not exist for the caller.
    with transaction(setup["engine"]) as session:
        session.execute(
            update(Document)
            .where(Document.id == UUID(accepted["document_id"]))
            .values(access_level="restricted")
        )
    assert (
        client.get(f"{BASE}/documents/{accepted['document_id']}", headers=viewer).status_code == 404
    )
    assert client.get(f"{BASE}/facts/{total['id']}", headers=viewer).status_code == 404
    assert client.get(f"{BASE}/documents", headers=viewer).json() == []
    assert client.get(f"{BASE}/claims", headers=viewer).json()["claims"] == []
    # Compliance may review restricted documents.
    assert client.post(f"{BASE}/reviews", json=body, headers=setup["compliance"]).status_code == 200


def test_correction_keeps_the_source_fact_and_records_an_event(setup) -> None:
    client = setup["client"]
    accepted = ingest(setup, "inkasso_c1")
    total = fact_of(setup, accepted["document_id"], "stated_total_claim")
    response = client.post(
        f"{BASE}/reviews",
        json={
            "entity_type": "fact",
            "entity_id": total["id"],
            "action": "correct",
            "expected_version": total["review_version"],
            "reason_category": "extraction_error",
            "value": {"amount": "455.00", "currency": "EUR"},
        },
        headers=setup["alpha"],
    )
    assert response.status_code == 200, response.text
    assert response.json()["review_version"] == total["review_version"] + 1
    after = client.get(f"{BASE}/facts/{total['id']}", headers=setup["alpha"]).json()
    assert after["amount"] == "445.00" and after["review_state"] == "corrected"
    assert after["effective"]["amount"] == "455.00" and after["effective"]["origin"] == "user"
    with Session(setup["engine"]) as session:
        fact = session.get(DocumentFact, {"tenant_id": "tenant-alpha", "id": UUID(total["id"])})
        assert fact is not None and str(fact.amount) == "445.00"
        event = session.scalar(select(IntelligenceReviewEvent))
        assert event is not None and event.actor_subject and event.entity_version == 2
        assert event.previous_state["review_state"] == "unreviewed"
        assert event.new_state["corrected_amount"] == "455.00"


def test_stale_and_forged_reviews_are_rejected(setup) -> None:
    client = setup["client"]
    accepted = ingest(setup, "inkasso_c1")
    total = fact_of(setup, accepted["document_id"], "stated_total_claim")
    body = {
        "entity_type": "fact",
        "entity_id": total["id"],
        "action": "confirm",
        "expected_version": total["review_version"],
        "reason_category": "verified_against_source",
    }
    assert client.post(f"{BASE}/reviews", json=body, headers=setup["alpha"]).status_code == 200
    assert client.post(f"{BASE}/reviews", json=body, headers=setup["alpha"]).status_code == 409
    forged = dict(body, expected_version=2, source={"page_number": 9, "raw_value": "9.999,00 EUR"})
    assert client.post(f"{BASE}/reviews", json=forged, headers=setup["alpha"]).status_code == 422
    unknown = dict(body, entity_id=str(uuid4()), expected_version=1)
    assert client.post(f"{BASE}/reviews", json=unknown, headers=setup["alpha"]).status_code == 404
    bad = dict(
        body, expected_version=2, action="correct", value={"amount": "-1", "currency": "EUR"}
    )
    assert client.post(f"{BASE}/reviews", json=bad, headers=setup["alpha"]).status_code == 422
    mixed = dict(
        body, expected_version=2, action="correct", value={"amount": "1", "currency": "eu"}
    )
    assert client.post(f"{BASE}/reviews", json=mixed, headers=setup["alpha"]).status_code == 422


def test_person_alias_collision_is_rejected(setup) -> None:
    client = setup["client"]
    ids = make_persons(setup)
    clash = client.post(
        f"{BASE}/persons/{ids['Person B']}/aliases",
        json={"kind": "name", "value": " anna   BEISPIEL "},
        headers=setup["alpha"],
    )
    assert clash.status_code == 409
    duplicate = client.post(f"{BASE}/persons", json={"label": "Person A"}, headers=setup["alpha"])
    assert duplicate.status_code == 409
    foreign = client.post(
        f"{BASE}/persons/{ids['Person A']}/aliases",
        json={"kind": "name", "value": "Someone Else"},
        headers=setup["beta"],
    )
    assert foreign.status_code == 404


def test_claim_group_review_and_rejected_membership_is_not_readded(setup) -> None:
    client = setup["client"]
    make_persons(setup)
    for key in ("invoice_c1", "reminder_c1"):
        ingest(setup, key)
    (claim,) = client.get(f"{BASE}/claims", headers=setup["alpha"]).json()["claims"]
    member = claim["documents"][1]
    response = client.post(
        f"{BASE}/reviews",
        json={
            "entity_type": "claim_member",
            "entity_id": member["member_id"],
            "action": "reject",
            "expected_version": member["review_version"],
            "reason_category": "not_same_claim",
        },
        headers=setup["alpha"],
    )
    assert response.status_code == 200, response.text
    reprocess = client.post(
        f"{BASE}/documents/{member['document_id']}/reprocess", headers=setup["alpha"]
    )
    assert reprocess.status_code == 202
    with Session(setup["engine"]) as session:
        IngestionWorker(
            session,
            vector_index=DeterministicVectorIndex(64),
            extractor=DocumentExtractor(engine=ScriptedEngine()),
        ).process(UUID(reprocess.json()["job_id"]), "tenant-alpha")
        session.commit()
    (after,) = client.get(f"{BASE}/claims", headers=setup["alpha"]).json()["claims"]
    states = {d["document_id"]: d["membership"] for d in after["documents"]}
    assert states[member["document_id"]] == "rejected"


def test_assigning_a_person_needs_a_person_of_the_tenant(setup) -> None:
    client = setup["client"]
    ids = make_persons(setup)
    accepted = ingest(setup, "shared_address_ambiguous")
    detail = client.get(
        f"{BASE}/documents/{accepted['document_id']}", headers=setup["alpha"]
    ).json()
    assert detail["debtor_status"] == "ambiguous"
    body = {
        "entity_type": "document",
        "entity_id": accepted["document_id"],
        "action": "assign_person",
        "expected_version": detail["review_version"],
        "reason_category": "verified_against_source",
        "person_id": ids["Person A"],
        "role": "debtor",
    }
    assert (
        client.post(
            f"{BASE}/reviews", json=dict(body, person_id=str(uuid4())), headers=setup["alpha"]
        ).status_code
        == 404
    )
    assigned = client.post(f"{BASE}/reviews", json=body, headers=setup["alpha"])
    assert assigned.status_code == 200, assigned.text
    after = client.get(f"{BASE}/documents/{accepted['document_id']}", headers=setup["alpha"]).json()
    assert after["debtor_status"] == "identified"
    debtors = {p["person_id"]: p["state"] for p in after["persons"] if p["role"] == "debtor"}
    # The reviewer's assignment decides; the competing proposal for Person B is set aside.
    assert debtors == {ids["Person A"]: "confirmed", ids["Person B"]: "rejected"}


def test_list_responses_hold_no_unrequested_source_text(setup) -> None:
    client = setup["client"]
    ingest(setup, "inkasso_c1")
    listed = client.get(f"{BASE}/documents", headers=setup["alpha"]).text
    for line in corpus.by_key("inkasso_c1").pages[0]:
        assert line not in listed
