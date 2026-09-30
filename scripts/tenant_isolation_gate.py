"""Live tenant-isolation gate across persistence, API, ingestion, vector and FinOps paths.

Every cross-tenant attempt counts as passed only when it fails with the product's
expected denial; every path also has a same-tenant positive control, so an
unavailable service cannot pass as "isolated". PASS requires zero leakage and a
clean removal of the gate's own synthetic tenants. BLOCKED is reserved for missing
external prerequisites. Evidence never contains credentials, tokens, document
content or embeddings.
"""
# ruff: noqa: S603

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from functools import partial
from pathlib import Path
from typing import Any, Protocol
from uuid import UUID, uuid4

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sqlalchemy import create_engine, func, select, text  # noqa: E402
from sqlalchemy.engine import Engine  # noqa: E402
from sqlalchemy.exc import IntegrityError  # noqa: E402

from ragops.finops.reservation import BudgetReservationService, ReservationError  # noqa: E402
from ragops.finops.service import FinOpsService  # noqa: E402
from ragops.knowledge.service import (  # noqa: E402
    KnowledgeAuthorizationError,
    KnowledgeManagementService,
    LocalDocumentBlobStore,
)
from ragops.persistence.database import transaction  # noqa: E402
from ragops.persistence.models import (  # noqa: E402
    Base,
    Document,
    DocumentVersion,
    IngestionJob,
    KnowledgeCollection,
    ModelConfiguration,
    ProviderConfiguration,
    Tenant,
    TenantBudget,
    TenantOwned,
    UsageRecord,
    User,
    Workspace,
)
from ragops.persistence.repository import TenantRepository  # noqa: E402
from ragops.vector.embedding import DeterministicEmbeddingProvider  # noqa: E402
from ragops.vector.index import AuthorizedVectorScope, VectorHit, VectorPayload  # noqa: E402
from ragops.workers.queue import IngestionMessage  # noqa: E402
from ragops.workers.worker import IngestionWorker  # noqa: E402

EVIDENCE = ROOT / "evidence" / "product-v1" / "rc-live" / "tenant-isolation.json"
RC_TENANT_PREFIX = "rc-ti"
VECTOR_DIMENSION = 32
OIDC_ISSUER = "https://rc-tenant-isolation.invalid/"
OIDC_AUDIENCE = "ragops-rc-tenant-isolation"
ADMIN_CROSS_TENANT_POLICY = (
    "tenant_bound_admin; shared in-process model catalog is managed by the admin role"
)
SHARED_CONTENT = b"Synthetic RC tenant-isolation document; identical bytes in both tenants."


class Queue(Protocol):
    def enqueue(self, message: IngestionMessage) -> None: ...
    def dequeue(self) -> IngestionMessage | None: ...


class VectorStore(Protocol):
    def upsert_chunks(self, vectors: list[tuple[list[float], VectorPayload]]) -> int: ...
    def search(
        self, vector: list[float], scope: AuthorizedVectorScope, limit: int = 10
    ) -> list[VectorHit]: ...


@dataclass
class Probe:
    """Records boundary checks; a denied cross-tenant attempt is the passing outcome."""

    checks: dict[str, bool] = field(default_factory=dict)
    cross_tenant_attempts: int = 0
    unauthorized_access_count: int = 0
    vector_tenant_leakage: int = 0
    vector_scope_leakage: int = 0

    def check(self, name: str, ok: bool) -> None:
        self.checks[name] = self.checks.get(name, True) and ok

    def denied(
        self, name: str, action: Callable[[], object], *expected: type[BaseException]
    ) -> None:
        self.cross_tenant_attempts += 1
        try:
            action()
        except expected:
            self.check(name, True)
            return
        self.unauthorized_access_count += 1
        self.check(name, False)

    def foreign(self, name: str, leaked: int) -> None:
        """A cross-tenant read that must return nothing of the other tenant."""
        self.cross_tenant_attempts += 1
        if leaked:
            self.unauthorized_access_count += 1
        self.check(name, leaked == 0)


@dataclass
class World:
    tenant_a: str
    tenant_b: str
    workspace_a: UUID
    workspace_a2: UUID
    collection_a: UUID
    collection_a2: UUID
    document_a: UUID
    version_a: UUID
    workspace_b: UUID
    collection_b: UUID
    document_b: UUID
    version_b: UUID
    job_a: UUID | None = None

    def a_identifiers(self) -> set[str]:
        values = {
            self.tenant_a,
            self.workspace_a,
            self.workspace_a2,
            self.collection_a,
            self.collection_a2,
            self.document_a,
            self.version_a,
            self.job_a,
        }
        return {str(value) for value in values if value is not None}


def rc_tenants(run_id: str) -> tuple[str, str]:
    return f"{RC_TENANT_PREFIX}-{run_id}-a", f"{RC_TENANT_PREFIX}-{run_id}-b"


def seed(engine: Engine, tenant_a: str, tenant_b: str, blobs: LocalDocumentBlobStore) -> World:
    with transaction(engine) as session:
        session.add_all(
            [Tenant(id=tenant_a, name="RC isolation A"), Tenant(id=tenant_b, name="RC isolation B")]
        )
        session.flush()
        a = KnowledgeManagementService(session, tenant_a, "rc-user-a", blobs)
        workspace_a = a.create_workspace("RC Workspace A")
        workspace_a2 = a.create_workspace("RC Workspace A2")
        collection_a = a.create_collection(workspace_a.id, "RC Collection A")
        collection_a2 = a.create_collection(workspace_a2.id, "RC Collection A2")
        upload_a = a.upload(
            workspace_a.id,
            collection_a.id,
            "RC Document A",
            "rc-shared-key",
            "rc-a.txt",
            "text/plain",
            SHARED_CONTENT,
        )
        b = KnowledgeManagementService(session, tenant_b, "rc-user-b", blobs)
        workspace_b = b.create_workspace("RC Workspace B")
        collection_b = b.create_collection(workspace_b.id, "RC Collection B")
        # Same logical key and identical bytes: tenant-scoped dedupe must not collide.
        upload_b = b.upload(
            workspace_b.id,
            collection_b.id,
            "RC Document B",
            "rc-shared-key",
            "rc-b.txt",
            "text/plain",
            SHARED_CONTENT,
        )
        return World(
            tenant_a,
            tenant_b,
            workspace_a.id,
            workspace_a2.id,
            collection_a.id,
            collection_a2.id,
            upload_a.document.id,
            upload_a.version.id,
            workspace_b.id,
            collection_b.id,
            upload_b.document.id,
            upload_b.version.id,
        )


def check_persistence(
    engine: Engine, world: World, blobs: LocalDocumentBlobStore, probe: Probe
) -> None:
    a_ids = world.a_identifiers()
    with transaction(engine) as session:
        repository = TenantRepository(session, world.tenant_b)
        for model, record_id in (
            (Workspace, world.workspace_a),
            (KnowledgeCollection, world.collection_a),
            (Document, world.document_a),
            (DocumentVersion, world.version_a),
        ):
            probe.foreign(
                "repository_read_isolation", int(repository.get(model, record_id) is not None)
            )
        listed: list[TenantOwned] = [
            *repository.list(Workspace, limit=200),
            *repository.list(Document, limit=200),
        ]
        probe.foreign("repository_read_isolation", sum(str(item.id) in a_ids for item in listed))
        b = KnowledgeManagementService(session, world.tenant_b, "rc-user-b", blobs)
        probe.denied(
            "repository_read_isolation",
            lambda: b.get_document(world.document_a),
            KnowledgeAuthorizationError,
        )
        probe.denied(
            "repository_read_isolation",
            lambda: b.versions(world.document_a),
            KnowledgeAuthorizationError,
        )
        visible: list[TenantOwned] = [
            *b.list_workspaces(limit=200),
            *b.list_collections(world.workspace_a, limit=200),
            *b.list_documents(limit=200),
        ]
        probe.foreign("repository_read_isolation", sum(str(item.id) in a_ids for item in visible))
        probe.check(
            "repository_positive_control", [d.id for d in b.list_documents()] == [world.document_b]
        )

    with transaction(engine) as session:
        b = KnowledgeManagementService(session, world.tenant_b, "rc-user-b", blobs)
        repository = TenantRepository(session, world.tenant_b)
        denied = KnowledgeAuthorizationError
        probe.denied(
            "repository_write_isolation",
            lambda: b.change_status(world.document_a, "validating"),
            denied,
        )
        probe.denied(
            "repository_write_isolation",
            lambda: b.update_metadata(world.document_a, {"rc": "b"}),
            denied,
        )
        probe.denied(
            "repository_write_isolation",
            lambda: b.create_collection(world.workspace_a, "RC Forged"),
            denied,
        )
        probe.denied(
            "repository_write_isolation",
            lambda: b.upload(
                world.workspace_a,
                world.collection_a,
                "RC Forged",
                "rc-forged",
                "rc-forged.txt",
                "text/plain",
                b"forged",
            ),
            denied,
        )
        probe.denied(
            "repository_write_isolation",
            lambda: b.upload(
                world.workspace_b,
                world.collection_a,
                "RC Forged",
                "rc-forged",
                "rc-forged.txt",
                "text/plain",
                b"forged",
            ),
            denied,
        )
        probe.denied(
            "repository_write_isolation",
            lambda: repository.add(
                Workspace(tenant_id=world.tenant_a, name="RC Forged", slug="rc-forged")
            ),
            ValueError,
        )

    def forged_relationship() -> None:
        # Direct identifier guessing below the service layer: composite tenant FKs.
        with transaction(engine) as forged:
            forged.add(
                Document(
                    tenant_id=world.tenant_b,
                    workspace_id=world.workspace_a,
                    collection_id=world.collection_a,
                    title="RC Forged",
                    logical_document_key="rc-forged",
                    created_by="rc-user-b",
                )
            )
            forged.flush()

    probe.denied("repository_write_isolation", forged_relationship, IntegrityError)
    with transaction(engine) as session:
        document = session.get(Document, (world.tenant_a, world.document_a))
        probe.check(
            "repository_write_isolation",
            document is not None
            and document.status == "uploaded"
            and not document.document_metadata,
        )


def check_lifecycle(
    engine: Engine, world: World, blobs: LocalDocumentBlobStore, probe: Probe
) -> None:
    with transaction(engine) as session:
        a = KnowledgeManagementService(session, world.tenant_a, "rc-user-a", blobs)
        result = a.upload(
            world.workspace_a,
            world.collection_a,
            "RC Document A",
            "rc-shared-key",
            "rc-a-v2.txt",
            "text/plain",
            SHARED_CONTENT + b" v2",
        )
        world.version_a = result.version.id
    with transaction(engine) as session:
        versions_a = session.scalars(
            select(DocumentVersion).where(
                DocumentVersion.tenant_id == world.tenant_a,
                DocumentVersion.document_id == world.document_a,
            )
        ).all()
        version_b = session.get(DocumentVersion, (world.tenant_b, world.version_b))
        document_b = session.get(Document, (world.tenant_b, world.document_b))
        probe.check(
            "lifecycle_positive_control",
            sorted(v.ingestion_status for v in versions_a) == ["superseded", "uploaded"],
        )
        probe.check(
            "lifecycle_isolation",
            version_b is not None
            and version_b.valid_to is None
            and version_b.superseded_by is None
            and version_b.ingestion_status == "uploaded"
            and document_b is not None
            and document_b.status == "uploaded",
        )


def check_ingestion(engine: Engine, world: World, queue: Queue, probe: Probe) -> None:
    with transaction(engine) as session:
        worker = IngestionWorker(session, worker_id="rc-tenant-isolation")
        job = worker.create_job(world.tenant_a, world.document_a, world.version_a)
        world.job_a = job_id = job.id
        correlation = job.correlation_id
        probe.denied(
            "ingestion_isolation",
            lambda: worker.create_job(world.tenant_b, world.document_a, world.version_a),
            ValueError,
        )
        probe.denied(
            "ingestion_isolation", lambda: worker.process(job_id, world.tenant_b), ValueError
        )
        probe.denied(
            "ingestion_isolation", lambda: worker.cancel(job_id, world.tenant_b), ValueError
        )
        probe.denied(
            "ingestion_isolation", lambda: worker.retry(job_id, world.tenant_b), ValueError
        )
    # The queue is a shared transport; tenant ownership is enforced when a worker claims a job.
    queue.enqueue(IngestionMessage(job_id, world.tenant_a, correlation))
    queue.enqueue(IngestionMessage(job_id, world.tenant_b, correlation))
    delivered = [message for message in (queue.dequeue(), queue.dequeue()) if message is not None]
    probe.check("ingestion_queue_delivery", len(delivered) == 2)
    for message in delivered:
        with transaction(engine) as session:
            worker = IngestionWorker(session, worker_id="rc-tenant-isolation")
            if message.tenant_id == world.tenant_a:
                processed = worker.process(message.job_id, message.tenant_id)
                probe.check("ingestion_positive_control", processed.status == "completed")
            else:
                probe.denied(
                    "ingestion_isolation",
                    partial(worker.process, message.job_id, message.tenant_id),
                    ValueError,
                )
    with transaction(engine) as session:
        owned = session.scalars(select(IngestionJob).where(IngestionJob.id == job_id)).all()
        probe.check(
            "ingestion_isolation",
            len(owned) == 1
            and owned[0].tenant_id == world.tenant_a
            and owned[0].status == "completed",
        )
        visible_to_b = session.scalars(
            select(IngestionJob.id).where(IngestionJob.tenant_id == world.tenant_b)
        ).all()
        probe.foreign("ingestion_isolation", int(job_id in visible_to_b))


def _payload(
    tenant: str, workspace: UUID, collection: UUID, document: UUID, version: UUID
) -> VectorPayload:
    now = datetime.now(UTC)
    return VectorPayload(
        tenant_id=tenant,
        workspace_id=str(workspace),
        collection_id=str(collection),
        document_id=str(document),
        document_version_id=str(version),
        chunk_id=f"{version}:0",
        access_level="internal",
        document_status="indexed",
        version_status="indexed",
        content_hash="rc-identical-chunk",
        chunk_index=0,
        source_name="rc-tenant-isolation",
        created_at=now.isoformat(),
        valid_from=(now - timedelta(minutes=1)).isoformat(),
    )


def check_vectors(index: VectorStore, world: World, probe: Probe) -> None:
    vector = DeterministicEmbeddingProvider(dimension=VECTOR_DIMENSION).embed(
        ["RC identical synthetic chunk for both tenants"]
    )[0]
    document_a2, version_a2 = uuid4(), uuid4()
    points = [
        (
            vector,
            _payload(
                world.tenant_a,
                world.workspace_a,
                world.collection_a,
                world.document_a,
                world.version_a,
            ),
        ),
        (
            vector,
            _payload(
                world.tenant_a, world.workspace_a2, world.collection_a2, document_a2, version_a2
            ),
        ),
        (
            vector,
            _payload(
                world.tenant_b,
                world.workspace_b,
                world.collection_b,
                world.document_b,
                world.version_b,
            ),
        ),
    ]
    probe.check("vector_upsert", index.upsert_chunks(points) == 3)

    def search(tenant: str, workspaces: frozenset[str] = frozenset()) -> list[VectorHit]:
        return index.search(
            vector, AuthorizedVectorScope(tenant, "sales", workspace_ids=workspaces), limit=20
        )

    hits_a, hits_b = search(world.tenant_a), search(world.tenant_b)
    guessed = search(world.tenant_b, frozenset({str(world.workspace_a)}))
    scoped = search(world.tenant_a, frozenset({str(world.workspace_a)}))
    for tenant, hits in (
        (world.tenant_a, hits_a),
        (world.tenant_b, hits_b),
        (world.tenant_b, guessed),
    ):
        leaked = sum(hit.payload.tenant_id != tenant for hit in hits)
        probe.vector_tenant_leakage += leaked
        probe.foreign("vector_tenant_isolation", leaked)
    probe.vector_scope_leakage += sum(
        hit.payload.workspace_id != str(world.workspace_a) for hit in scoped
    )
    probe.check("vector_scope_isolation", probe.vector_scope_leakage == 0)
    probe.check(
        "vector_positive_control",
        (len(hits_a), len(hits_b), len(scoped), len(guessed)) == (2, 1, 1, 0),
    )


def check_finops(engine: Engine, world: World, probe: Probe) -> None:
    today = date.today()
    start = today.replace(day=1)
    end = (start + timedelta(days=32)).replace(day=1)
    with transaction(engine) as session:
        budgets: dict[str, TenantBudget] = {}
        for tenant in (world.tenant_a, world.tenant_b):
            user = User(tenant_id=tenant, issuer=OIDC_ISSUER, subject=f"rc-user-{tenant}")
            provider = ProviderConfiguration(
                tenant_id=tenant, name="rc-provider", kind="deterministic"
            )
            session.add_all([user, provider])
            session.flush()
            model = ModelConfiguration(
                tenant_id=tenant,
                provider_id=provider.id,
                name="rc-model",
                input_price=Decimal("0"),
                output_price=Decimal("0"),
            )
            budget = TenantBudget(
                tenant_id=tenant,
                period_start=start,
                period_end=end,
                budget_amount=Decimal("100"),
                enforcement_mode="hard_limit",
            )
            session.add_all([model, budget])
            session.flush()
            budgets[tenant] = budget
            if tenant == world.tenant_a:
                session.add(
                    UsageRecord(
                        tenant_id=tenant,
                        user_id=user.id,
                        model_id=model.id,
                        correlation_id=uuid4(),
                        input_tokens=10,
                        output_tokens=5,
                        cost=Decimal("40"),
                        latency_ms=1.0,
                        endpoint="rc-gate",
                        workflow="rc-tenant-isolation",
                    )
                )
        session.flush()
        spend_a = FinOpsService(session, world.tenant_a).spend(start, end)
        spend_b = FinOpsService(session, world.tenant_b).spend(start, end)
        probe.check("finops_positive_control", spend_a == Decimal("40"))
        probe.foreign("finops_isolation", int(spend_b != 0))
        decision_b = FinOpsService(session, world.tenant_b).preflight(
            budgets[world.tenant_b], Decimal("1")
        )
        probe.check("finops_isolation", decision_b.spend == 0)
        service_a = BudgetReservationService(session, world.tenant_a)
        service_b = BudgetReservationService(session, world.tenant_b)
        reservation_a = service_a.reserve(
            budgets[world.tenant_a].id, Decimal("10"), "rc-shared-idempotency"
        )
        reservation_b = service_b.reserve(
            budgets[world.tenant_b].id, Decimal("10"), "rc-shared-idempotency"
        )
        probe.check("finops_isolation", reservation_a.id != reservation_b.id)
        budget_a_id = budgets[world.tenant_a].id
        probe.denied(
            "finops_isolation",
            lambda: service_b.reserve(budget_a_id, Decimal("1"), "rc-cross"),
            ReservationError,
        )
        probe.denied("finops_isolation", lambda: service_b.commit(reservation_a), ReservationError)
        probe.denied("finops_isolation", lambda: service_b.release(reservation_a), ReservationError)
        probe.check("finops_isolation", reservation_a.status == "reserved")


@contextmanager
def api_database_environment(database_url: str) -> Iterator[None]:
    names = ("RAGOPS_DATABASE_URL", "RAGOPS_DATABASE_URL_FILE", "RAGOPS_ENV")
    previous = {name: os.environ.get(name) for name in names}
    os.environ.pop("RAGOPS_DATABASE_URL_FILE", None)
    os.environ["RAGOPS_DATABASE_URL"] = database_url
    os.environ["RAGOPS_ENV"] = "test"
    try:
        yield
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def check_api(database_url: str, world: World, workdir: Path, probe: Probe) -> None:
    """Drive the real FastAPI routes with ephemeral RS256 tokens through OIDC validation."""
    import jwt
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from fastapi.testclient import TestClient

    from ragops.api.app import create_app
    from ragops.config.settings import Settings

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_key = (
        key.public_key()
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        .decode()
    )

    def headers(tenant: str, roles: list[str]) -> dict[str, str]:
        claims = {
            "iss": OIDC_ISSUER,
            "aud": OIDC_AUDIENCE,
            "sub": f"rc-user-{tenant}",
            "tenant_id": tenant,
            "roles": roles,
            "exp": datetime.now(UTC) + timedelta(minutes=5),
        }
        return {"Authorization": f"Bearer {jwt.encode(claims, key, algorithm='RS256')}"}

    evidence_dir = workdir / "api-evidence"
    evidence_dir.mkdir()
    (evidence_dir / "audit-events.jsonl").write_text(
        "".join(
            json.dumps(
                {"event_id": str(uuid4()), "tenant_id": tenant, "user_id": f"rc-user-{tenant}"}
            )
            + "\n"
            for tenant in (world.tenant_a, world.tenant_b)
        ),
        encoding="utf-8",
    )
    settings = Settings(
        environment="test",
        identity_provider="oidc",
        oidc_issuer=OIDC_ISSUER,
        oidc_audience=OIDC_AUDIENCE,
        oidc_public_key=public_key,
        data_dir=workdir / "api-data",
        evidence_dir=evidence_dir,
    )
    a_admin, b_admin = headers(world.tenant_a, ["admin"]), headers(world.tenant_b, ["admin"])
    b_user = headers(world.tenant_b, ["sales"])
    a_ids = world.a_identifiers()
    document_a, job_a = world.document_a, world.job_a

    with api_database_environment(database_url):
        client = TestClient(create_app(settings))
        # Positive controls: the same routes work for the owning tenant.
        created_budget = client.post(
            "/v1/admin/budgets", json={"budget_amount": 50}, headers=a_admin
        )
        policy = client.put(f"/v1/admin/model-policies/{world.tenant_a}", json={}, headers=a_admin)
        positive = (
            str(world.workspace_a) in client.get("/v1/workspaces", headers=a_admin).text,
            client.get(f"/v1/documents/{document_a}/versions", headers=a_admin).status_code,
            client.get(f"/v1/ingestion/jobs/{job_a}", headers=a_admin).status_code,
            created_budget.status_code,
            policy.status_code,
            len(client.get("/v1/audit-events", headers=a_admin).json()),
        )
        probe.check("api_positive_control", positive == (True, 200, 200, 201, 200, 1))
        budget_a = str(created_budget.json().get("id", "missing"))

        def leaked(body: str) -> int:
            return sum(identifier in body for identifier in a_ids)

        for path in (
            "/v1/workspaces",
            f"/v1/collections?workspace_id={world.workspace_a}",
            "/v1/ingestion/jobs",
            "/v1/admin/budgets",
            "/v1/admin/model-policies",
            "/v1/audit-events",
        ):
            response = client.get(path, headers=b_admin)
            probe.check("api_list_isolation", response.status_code == 200)
            probe.foreign("api_list_isolation", leaked(response.text))

        direct: tuple[tuple[str, str, dict[str, Any] | None, set[int]], ...] = (
            ("GET", f"/v1/documents/{document_a}/versions", None, {404}),
            ("GET", f"/v1/ingestion/jobs/{job_a}", None, {404}),
            ("POST", f"/v1/ingestion/jobs/{job_a}/cancel", None, {404}),
            # Contract: retry maps every unavailable job, including foreign ones, to 409.
            ("POST", f"/v1/ingestion/jobs/{job_a}/retry", None, {409}),
            ("POST", f"/v1/documents/{document_a}/reindex", None, {404}),
            ("PATCH", f"/v1/admin/budgets/{budget_a}", {"budget_amount": 1}, {404}),
            ("PUT", f"/v1/admin/model-policies/{world.tenant_a}", {}, {404}),
        )
        for method, path, body, expected in direct:
            response = client.request(method, path, json=body, headers=b_admin)
            probe.foreign(
                "api_direct_id_isolation",
                int(response.status_code not in expected) + leaked(response.text),
            )
        upload = client.post(
            "/v1/documents/upload",
            data={
                "workspace_id": str(world.workspace_a),
                "collection_id": str(world.collection_a),
                "title": "RC Forged",
                "logical_document_key": "rc-forged-api",
            },
            files={"file": ("rc-forged.txt", b"forged", "text/plain")},
            headers=b_admin,
        )
        # Contract: an unavailable target collection surfaces as 400 "resource not found".
        probe.foreign(
            "api_direct_id_isolation", int(upload.status_code != 400) + leaked(upload.text)
        )
        for method, path in (("GET", "/v1/admin/model-policies"), ("POST", "/v1/admin/models")):
            response = client.request(
                method, path, json={} if method == "POST" else None, headers=b_user
            )
            probe.foreign("admin_requires_role", int(response.status_code != 403))
        after = client.get(f"/v1/ingestion/jobs/{job_a}", headers=a_admin).json()
        probe.check("api_direct_id_isolation", after.get("status") == "completed")


def run_scenarios(
    engine: Engine,
    database_url: str,
    queue: Queue,
    index: VectorStore,
    workdir: Path,
    tenant_a: str,
    tenant_b: str,
) -> Probe:
    probe = Probe()
    blobs = LocalDocumentBlobStore(workdir / "blobs")
    world = seed(engine, tenant_a, tenant_b, blobs)
    check_persistence(engine, world, blobs, probe)
    check_lifecycle(engine, world, blobs, probe)
    check_ingestion(engine, world, queue, probe)
    check_vectors(index, world, probe)
    check_finops(engine, world, probe)
    check_api(database_url, world, workdir, probe)
    return probe


def unrelated_row_counts(engine: Engine, tenants: tuple[str, ...]) -> dict[str, int]:
    counts: dict[str, int] = {}
    with engine.connect() as connection:
        for table in Base.metadata.sorted_tables:
            column = table.c.get("tenant_id") if table.name != "tenants" else table.c.id
            if column is not None:
                counts[table.name] = int(
                    connection.scalar(
                        select(func.count()).select_from(table).where(column.not_in(tenants))
                    )
                    or 0
                )
    return counts


def cleanup_database(engine: Engine, tenants: tuple[str, ...]) -> bool:
    """Delete only the gate's own synthetic tenants, child tables first."""
    with transaction(engine) as session:
        for table in reversed(Base.metadata.sorted_tables):
            if "tenant_id" in table.c:
                session.execute(table.delete().where(table.c.tenant_id.in_(tenants)))
        tenants_table = Base.metadata.tables["tenants"]
        session.execute(tenants_table.delete().where(tenants_table.c.id.in_(tenants)))
    with engine.connect() as connection:
        remaining = sum(
            int(
                connection.scalar(
                    select(func.count()).select_from(table).where(table.c.tenant_id.in_(tenants))
                )
                or 0
            )
            for table in Base.metadata.sorted_tables
            if "tenant_id" in table.c
        )
    return remaining == 0


def classify(probe: Probe, cleanup_ok: bool, unrelated_intact: bool) -> tuple[str, str]:
    failed = sorted(name for name, ok in probe.checks.items() if not ok)
    if probe.unauthorized_access_count or probe.vector_tenant_leakage:
        return "FAIL", f"cross-tenant access succeeded: {', '.join(failed) or 'vector'}"
    if failed or probe.vector_scope_leakage:
        return "FAIL", f"isolation assertions failed: {', '.join(failed) or 'vector_scope'}"
    if not cleanup_ok or not unrelated_intact:
        return "FAIL", "synthetic tenant cleanup failed or touched unrelated data"
    return "PASS", "all implemented tenant boundaries denied cross-tenant access"


def result_fields(probe: Probe) -> dict[str, Any]:
    def ok(name: str) -> bool | None:
        return probe.checks.get(name)

    return {
        "repository_read_isolation": ok("repository_read_isolation"),
        "repository_write_isolation": ok("repository_write_isolation"),
        "api_direct_id_isolation": ok("api_direct_id_isolation"),
        "api_list_isolation": ok("api_list_isolation"),
        "ingestion_isolation": ok("ingestion_isolation"),
        "vector_tenant_leakage": probe.vector_tenant_leakage,
        "vector_scope_leakage": probe.vector_scope_leakage,
        "lifecycle_isolation": ok("lifecycle_isolation"),
        "finops_isolation": ok("finops_isolation"),
        "admin_requires_role": ok("admin_requires_role"),
        "admin_cross_tenant_policy": ADMIN_CROSS_TENANT_POLICY,
        "cross_tenant_attempts": probe.cross_tenant_attempts,
        "unauthorized_access_count": probe.unauthorized_access_count,
        "tenant_leakage": probe.unauthorized_access_count + probe.vector_tenant_leakage,
        "checks": dict(sorted(probe.checks.items())),
    }


def _commit() -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],  # noqa: S607
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return "unknown"
    return result.stdout.strip() or "unknown"


def _finish(evidence: dict[str, Any], status: str, reason: str, exit_code: int) -> int:
    evidence.update(
        {
            "status": status,
            "reason": reason,
            "integration_test_exit_code": exit_code,
            "timestamp": datetime.now(UTC).isoformat(),
        }
    )
    EVIDENCE.parent.mkdir(parents=True, exist_ok=True)
    EVIDENCE.write_text(json.dumps(evidence, indent=2) + "\n", encoding="utf-8")
    return exit_code


def _redis_endpoint(redis_url: str, evidence: dict[str, Any]) -> str | None:
    """Reuse the proven RC Redis discovery: a direct URL or the published rc-redis port."""
    from scripts import redis_worker_gate as redis_gate

    if not redis_url.startswith(("redis://", "rediss://")):
        return None
    if not redis_gate.is_docker_service_url(redis_url):
        return redis_url if redis_gate.wait_for_redis(redis_url, 2) else None
    command = redis_gate.compose_up_command()
    if command is None:
        return None
    started = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, check=False)
    if started.returncode != 0:
        return None
    endpoint, _ = redis_gate.published_endpoint(command)
    if endpoint is None:
        return None
    url = redis_gate.host_redis_url(endpoint[1])
    evidence["redis_endpoint_kind"] = "dynamic_loopback"
    return url if redis_gate.wait_for_redis(url, 10) else None


@contextmanager
def _disposable_qdrant(evidence: dict[str, Any]) -> Iterator[str | None]:
    """Start the gate's own RC Qdrant project and always remove exactly that service."""
    from scripts import qdrant_live_gate as qdrant_gate

    project = f"{qdrant_gate.COMPOSE_PROJECT_PREFIX}-ti-{uuid4().hex[:8]}"
    base = qdrant_gate.compose_base(project)
    if base is None:
        yield None
        return
    started = subprocess.run(
        [*base, "up", "-d", "--no-deps", qdrant_gate.COMPOSE_SERVICE],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    try:
        if started.returncode != 0:
            yield None
            return
        port_result = subprocess.run(
            [*base, "port", qdrant_gate.COMPOSE_SERVICE, "6333"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        port = (
            qdrant_gate.parse_loopback_port(port_result.stdout)
            if port_result.returncode == 0
            else None
        )
        endpoint = f"http://127.0.0.1:{port}" if port else None
        if endpoint is not None:
            from qdrant_client import QdrantClient

            client = QdrantClient(url=endpoint, timeout=2)
            deadline = time.monotonic() + qdrant_gate.bounded_timeout(
                os.getenv("RAGOPS_RC_QDRANT_READY_TIMEOUT_SECONDS")
            )
            ready = False
            while time.monotonic() < deadline and not ready:
                try:
                    client.get_collections()
                    ready = True
                except Exception:  # noqa: BLE001 - readiness polling
                    time.sleep(0.5)
            client.close()
            endpoint = endpoint if ready else None
        yield endpoint
    finally:
        # "rm" leaves the project network behind; "down" on this run's own project removes
        # it too, so repeated gate runs cannot exhaust Docker's address pools.
        removed = [
            subprocess.run(
                [*base, *arguments], cwd=ROOT, capture_output=True, text=True, check=False
            ).returncode
            for arguments in (("rm", "-sf", qdrant_gate.COMPOSE_SERVICE), ("down",))
        ]
        evidence["qdrant_service_cleanup_status"] = "PASS" if not any(removed) else "FAIL"


def main() -> int:
    from scripts.postgres_concurrency_gate import (
        migration_revision,
        run_alembic,
        validate_isolated_database,
    )

    run_id = uuid4().hex[:10]
    tenant_a, tenant_b = rc_tenants(run_id)
    database_url = os.getenv("RAGOPS_TEST_DATABASE_URL", "")
    evidence: dict[str, Any] = {
        "gate": "tenant_isolation",
        "status": "BLOCKED",
        "tested_commit": _commit(),
        "timestamp": datetime.now(UTC).isoformat(),
        "postgres_isolated_database": False,
        "postgres_reachable": False,
        "migration_status": "NOT_RUN",
        "redis_reachable": False,
        "qdrant_reachable": False,
        "oidc_used": False,
        "identity_validation": "OIDCIdentityProvider with an ephemeral RS256 issuer; "
        "live Keycloak token issuance is covered by the oidc gate",
        "tenant_a_id": tenant_a,
        "tenant_b_id": tenant_b,
        "tenant_leakage": None,
        "cleanup_status": "NOT_RUN",
    }
    valid, reason, _ = validate_isolated_database(database_url)
    if not valid:
        return _finish(evidence, "BLOCKED", f"isolated PostgreSQL required: {reason}", 2)
    evidence["postgres_isolated_database"] = True
    engine = create_engine(database_url, hide_parameters=True, connect_args={"connect_timeout": 3})
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        evidence["postgres_reachable"] = True
    except Exception:  # noqa: BLE001
        engine.dispose()
        return _finish(evidence, "BLOCKED", "isolated PostgreSQL database is unreachable", 2)
    migrated = run_alembic(database_url, "upgrade", "head")
    current = run_alembic(database_url, "current")
    evidence["migration_status"] = (
        "PASS" if migrated.returncode == 0 and migration_revision(current.stdout) else "FAIL"
    )
    if evidence["migration_status"] != "PASS":
        engine.dispose()
        return _finish(evidence, "FAIL", "isolated PostgreSQL could not be migrated to head", 1)
    if (
        importlib.util.find_spec("redis") is None
        or importlib.util.find_spec("qdrant_client") is None
    ):
        engine.dispose()
        return _finish(evidence, "BLOCKED", "redis or qdrant-client package is unavailable", 2)
    redis_url = _redis_endpoint(os.getenv("RAGOPS_REDIS_URL", ""), evidence)
    if redis_url is None:
        engine.dispose()
        return _finish(evidence, "BLOCKED", "RC Redis is unavailable", 2)
    evidence["redis_reachable"] = True

    from ragops.vector.index import QdrantVectorIndex
    from ragops.workers.queue import RedisIngestionQueue

    tenants = (tenant_a, tenant_b)
    queue = RedisIngestionQueue(redis_url, name=f"ragops-rc-tenant-isolation-{run_id}")
    status, reason, exit_code = "FAIL", "tenant isolation scenario did not complete", 1
    with _disposable_qdrant(evidence) as qdrant_url, tempfile.TemporaryDirectory() as workdir:
        if qdrant_url is None:
            queue.client.delete(queue.name)
            engine.dispose()
            return _finish(evidence, "BLOCKED", "RC Qdrant service is unavailable", 2)
        evidence["qdrant_reachable"] = True
        collection = f"rc_ti_{run_id}"
        index = QdrantVectorIndex(url=qdrant_url, collection=collection, dimension=VECTOR_DIMENSION)
        before = unrelated_row_counts(engine, tenants)
        probe: Probe | None = None
        try:
            index.ensure_schema()
            probe = run_scenarios(
                engine, database_url, queue, index, Path(workdir), tenant_a, tenant_b
            )
        except Exception as exc:  # noqa: BLE001 - evidence must not contain raw service detail
            reason = f"tenant isolation scenario raised {type(exc).__name__}"
        finally:
            queue.client.delete(queue.name)
            cleanup_ok = cleanup_database(engine, tenants)
            unrelated_intact = unrelated_row_counts(engine, tenants) == before
            try:
                if index.client.collection_exists(collection):
                    index.client.delete_collection(collection)
            except Exception:  # noqa: BLE001 - container removal follows
                cleanup_ok = False
            engine.dispose()
        evidence["cleanup_status"] = "PASS" if cleanup_ok and unrelated_intact else "FAIL"
        evidence["unrelated_data_intact"] = unrelated_intact
        if probe is not None:
            evidence.update(result_fields(probe))
            status, reason = classify(probe, cleanup_ok, unrelated_intact)
            exit_code = 0 if status == "PASS" else 1
    if evidence.get("qdrant_service_cleanup_status") == "FAIL":
        status, reason, exit_code = "FAIL", "disposable RC Qdrant service could not be removed", 1
    return _finish(evidence, status, reason, exit_code)


if __name__ == "__main__":
    raise SystemExit(main())
