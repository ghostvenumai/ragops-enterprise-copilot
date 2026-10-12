"""Image uploads, page reports and chunk provenance through the authenticated API.

The page report and the chunk regions are tenant- and access-level-scoped, carry no document
text and are 404 for every other tenant or an insufficient role.
"""

from __future__ import annotations

import os
import shutil
from collections.abc import Iterator
from pathlib import Path

import pytest

pytest.importorskip("alembic", reason="Install declared persistence extra; product gate blocks")

from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from PIL import Image
from scripts.ocr_fixtures import image_bytes, scan_image
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from tests.security.ocr_helpers import ScriptedEngine, words
from tests.security.oidc_helpers import AUDIENCE, ISSUER, bearer

from ragops.api.app import create_app
from ragops.config.settings import Settings
from ragops.ingestion.pages import DocumentExtractor
from ragops.knowledge.service import KnowledgeManagementService, LocalDocumentBlobStore
from ragops.persistence.database import transaction
from ragops.persistence.models import Tenant
from ragops.vector.index import DeterministicVectorIndex
from ragops.workers.worker import IngestionWorker

pytestmark = pytest.mark.skipif(shutil.which("pdftoppm") is None, reason="poppler missing")
SENSITIVE_TEXT = "Kundennummer 99887766 Mahnbetrag 4.321,00"


@pytest.fixture
def setup(keys, tmp_path: Path, monkeypatch) -> Iterator[dict[str, object]]:
    url = f"sqlite:///{tmp_path / 'ocr-api.db'}"
    engine = create_engine(url)
    with engine.begin() as connection:
        cfg = Config("alembic.ini")
        cfg.attributes["connection"] = connection
        command.upgrade(cfg, "head")
    with transaction(engine) as session:
        for tenant in ("tenant-alpha", "tenant-beta"):
            session.add(Tenant(id=tenant, name=tenant))
        session.flush()
        service = KnowledgeManagementService(
            session, "tenant-alpha", "admin", LocalDocumentBlobStore(tmp_path / "data" / "blobs")
        )
        workspace = service.create_workspace("W")
        collection = service.create_collection(workspace.id, "C")
        ids = (str(workspace.id), str(collection.id))
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
            "workspace": ids[0],
            "collection": ids[1],
            "alpha": bearer(keys[0], "tenant-alpha", ["admin"]),
            "viewer": bearer(keys[0], "tenant-alpha", ["viewer"]),
            "beta": bearer(keys[0], "tenant-beta", ["admin"]),
        }
    engine.dispose()


def upload(setup, name: str, content: bytes, mime: str, key: str = "scan"):
    return setup["client"].post(
        "/v1/documents/upload",
        data={
            "workspace_id": setup["workspace"],
            "collection_id": setup["collection"],
            "title": "Bescheid",
            "logical_document_key": key,
        },
        files={"file": (name, content, mime)},
        headers=setup["alpha"],
    )


def run_worker(setup, job_id: str, ocr: ScriptedEngine) -> None:
    from uuid import UUID

    with Session(setup["engine"]) as session:
        IngestionWorker(
            session,
            vector_index=DeterministicVectorIndex(64),
            extractor=DocumentExtractor(engine=ocr),
        ).process(UUID(job_id), "tenant-alpha")
        session.commit()


def test_png_upload_page_report_and_chunk_provenance(setup) -> None:
    client = setup["client"]
    content = image_bytes(scan_image([SENSITIVE_TEXT], size=(900, 600)), "PNG")
    response = upload(setup, "Bescheid.png", content, "image/png")
    assert response.status_code == 202, response.text
    accepted = response.json()
    run_worker(setup, accepted["job_id"], ScriptedEngine(words(SENSITIVE_TEXT)))
    jobs = client.get("/v1/ingestion/jobs", headers=setup["alpha"]).json()
    assert jobs[0]["status"] == "completed" and jobs[0]["review_required"] is False
    assert jobs[0]["pages_total"] == 1 and jobs[0]["pages_review"] == 0
    base = f"/v1/documents/{accepted['document_id']}/versions/{accepted['version_id']}"
    pages = client.get(f"{base}/pages", headers=setup["alpha"])
    assert pages.status_code == 200
    report = pages.json()
    assert report["review_required"] is False and len(report["pages"]) == 1
    page = report["pages"][0]
    assert page["page_number"] == 1 and page["classification"] == "scanned_image"
    assert page["extraction_method"] == "ocr" and page["quality_state"] == "accepted"
    assert page["coordinate_space"] == "page-normalized-v1"
    assert (page["width"], page["height"], page["unit"]) == (900, 600, "px")
    provenance = client.get(f"{base}/chunks/0/provenance", headers=setup["alpha"])
    assert provenance.status_code == 200
    body = provenance.json()
    assert body["page_number"] == 1 and body["source_name"] == "Bescheid.png"
    assert body["chunk_id"] == f"{accepted['version_id']}:0"
    assert body["regions"] and all(
        0 <= r["box"][0] < r["box"][2] <= 1 and 0 <= r["box"][1] < r["box"][3] <= 1
        for r in body["regions"]
    )
    for text in (
        pages.text,
        provenance.text,
        client.get("/v1/ingestion/jobs", headers=setup["alpha"]).text,
    ):
        assert "99887766" not in text and "4.321,00" not in text


def test_other_tenants_and_insufficient_roles_get_404(setup) -> None:
    client = setup["client"]
    content = image_bytes(scan_image(["Vertraulich"], size=(900, 600)), "PNG")
    accepted = upload(setup, "geheim.png", content, "image/png").json()
    run_worker(setup, accepted["job_id"], ScriptedEngine(words("Vertraulicher Bescheid Text")))
    base = f"/v1/documents/{accepted['document_id']}/versions/{accepted['version_id']}"
    for path in (f"{base}/pages", f"{base}/chunks/0/provenance"):
        assert client.get(path, headers=setup["beta"]).status_code == 404
    with transaction(setup["engine"]) as session:
        from uuid import UUID

        from ragops.persistence.models import Document

        document = session.get(Document, ("tenant-alpha", UUID(accepted["document_id"])))
        document.access_level = "confidential"
    for path in (f"{base}/pages", f"{base}/chunks/0/provenance"):
        assert client.get(path, headers=setup["viewer"]).status_code == 404
        assert client.get(path, headers=setup["alpha"]).status_code == 200
    other_version = f"/v1/documents/{accepted['document_id']}/versions/{accepted['document_id']}"
    assert client.get(f"{other_version}/pages", headers=setup["alpha"]).status_code == 404
    assert client.get(f"{base}/chunks/999/provenance", headers=setup["alpha"]).status_code == 404


def test_review_required_is_visible_in_the_job_list(setup) -> None:
    content = image_bytes(scan_image(["Unscharf"], size=(900, 600)), "PNG")
    accepted = upload(setup, "unscharf.png", content, "image/png").json()
    native = image_bytes(scan_image(["Zweites"], size=(900, 601)), "PNG")
    assert upload(setup, "zweit.png", native, "image/png", key="zwei").status_code == 202
    run_worker(setup, accepted["job_id"], ScriptedEngine(words("kaum lesbar", confidence=10)))
    job = next(
        job
        for job in setup["client"].get("/v1/ingestion/jobs", headers=setup["alpha"]).json()
        if job["job_id"] == accepted["job_id"]
    )
    assert job["status"] == "failed" and job["error_code"] == "ocr_review_required"
    assert job["review_required"] is True and job["pages_review"] == 1


def test_scan_sized_uploads_pass_while_other_routes_keep_their_limit(setup) -> None:
    noise = Image.frombytes("L", (2400, 2400), os.urandom(2400 * 2400))
    content = image_bytes(noise, "PNG")
    assert 3_000_000 < len(content) < 12_000_000
    assert upload(setup, "gross.png", content, "image/png").status_code == 202
    too_big = setup["client"].post(
        "/v1/query",
        content=b'{"question": "' + b"x" * 4_000_000 + b'"}',
        headers={**setup["alpha"], "content-type": "application/json"},
    )
    assert too_big.status_code == 413
    huge = upload(setup, "riesig.png", content + b"\x00" * 9_000_000, "image/png", key="h")
    assert huge.status_code == 413
