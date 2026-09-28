"""Readiness/failure/recovery RC gate over disposable, proxied dependencies.

A gate-owned ragops_test_rfr_<run> database, a gate namespace on the RC Redis service and a
disposable RC Qdrant service are each reached only through a loopback FaultProxy, so
failures are injected for this gate's app and worker alone. One app instance and one worker
runtime live through every drill: refusal, blackhole and mid-operation disconnects of
PostgreSQL, Redis and Qdrant, pending jobs across outages, the retry budget, a combined
outage, startup while unready, unready-first shutdown and a restart that recovers an
in-flight message. Evidence is derived from observed HTTP, database, queue and vector state.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from collections import Counter
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sqlalchemy import create_engine, func, select, text  # noqa: E402
from sqlalchemy.engine import Engine, make_url  # noqa: E402
from sqlalchemy.exc import DBAPIError  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from scripts.fault_proxy import FaultProxy  # noqa: E402

EVIDENCE = ROOT / "evidence" / "product-v1" / "rc-live" / "readiness-failure-recovery.json"
GATE_DATABASE = re.compile(r"^ragops_test_rfr_[0-9a-f]{10}$")
DEFAULT_REDIS_URL = "redis://redis:6379/0"
OIDC_ISSUER = "https://rc-readiness.invalid/"
OIDC_AUDIENCE = "ragops-rc-readiness"
DIMENSION = 16
DEPENDENCY_TIMEOUT = 1.0
REQUEST_BUDGET_MS = 5000
TRANSITION_BUDGET_S = 10.0
MAX_ATTEMPTS = 5


@dataclass
class DrillResult:
    checks: dict[str, bool] = field(default_factory=dict)
    metrics: dict[str, Any] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    tenant_leakage: int = 0
    provider_invocations: int = 0
    paid_provider_calls: int = 0

    def check(self, name: str, ok: bool) -> None:
        self.checks[name] = self.checks.get(name, True) and ok


def wait_until(predicate: Callable[[], bool], timeout: float = TRANSITION_BUDGET_S) -> float | None:
    """Poll observed state; returns elapsed milliseconds or None when the deadline passed."""
    started = time.monotonic()
    while time.monotonic() - started < timeout:
        with suppress(Exception):
            if predicate():
                return round((time.monotonic() - started) * 1000, 1)
        time.sleep(0.05)
    return None


def timed(action: Callable[[], Any]) -> tuple[Any, float]:
    started = time.monotonic()
    value = action()
    return value, round((time.monotonic() - started) * 1000, 1)


@contextmanager
def gate_database(base_url: str, run_id: str) -> Iterator[str]:
    name = f"ragops_test_rfr_{run_id}"
    if not GATE_DATABASE.match(name):
        raise ValueError("refusing a database name outside the gate namespace")
    admin = create_engine(base_url, isolation_level="AUTOCOMMIT", hide_parameters=True)
    created = False
    try:
        with admin.connect() as connection:
            connection.execute(text(f'CREATE DATABASE "{name}"'))
            created = True
        yield make_url(base_url).set(database=name).render_as_string(hide_password=False)
    finally:
        if created and GATE_DATABASE.match(name):
            with admin.connect() as connection:
                connection.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()


@contextmanager
def process_env(values: dict[str, str]) -> Iterator[None]:
    previous = {name: os.environ.get(name) for name in values}
    os.environ.update(values)
    try:
        yield
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


@dataclass
class Drill:
    run_id: str
    settings: Any
    direct_engine: Engine
    direct_qdrant: Any
    collection: str
    proxies: dict[str, FaultProxy]
    tenants: tuple[str, str]
    headers: dict[str, dict[str, str]]
    collections: dict[str, tuple[str, str]]
    completions: Counter[str] = field(default_factory=Counter)
    jobs: dict[str, str] = field(default_factory=dict)

    def app(self) -> Any:
        from fastapi.testclient import TestClient

        from ragops.api.app import create_app

        return TestClient(create_app(self.settings), raise_server_exceptions=False)

    def upload(self, client: Any, tenant: str, label: str) -> Any:
        workspace, collection = self.collections[tenant]
        response = client.post(
            "/v1/documents/upload",
            data={
                "workspace_id": workspace,
                "collection_id": collection,
                "title": f"RC readiness {label}",
                "logical_document_key": f"rc-{label}",
            },
            files={
                "file": (
                    f"{label}.txt",
                    f"RC readiness drill {label} {tenant}".encode(),
                    "text/plain",
                )
            },
            headers=self.headers[tenant],
        )
        if response.status_code == 202:
            self.jobs[label] = response.json()["job_id"]
        return response

    def job(self, label: str) -> dict[str, Any]:
        from ragops.persistence.models import IngestionJob

        with Session(self.direct_engine) as session:
            job = session.get(IngestionJob, (self._tenant_of(label), self.jobs[label]))
            return (
                {
                    "status": job.status,
                    "attempts": job.attempt_count,
                    "version": str(job.document_version_id),
                }
                if job
                else {}
            )

    def _tenant_of(self, label: str) -> str:
        from ragops.persistence.models import IngestionJob

        with Session(self.direct_engine) as session:
            return str(
                session.scalar(
                    select(IngestionJob.tenant_id).where(IngestionJob.id == self.jobs[label])
                )
            )

    def points(self, version: str) -> int:
        from qdrant_client.http import models

        return int(
            self.direct_qdrant.count(
                self.collection,
                count_filter=models.Filter(
                    must=[
                        models.FieldCondition(
                            key="document_version_id", match=models.MatchValue(value=version)
                        )
                    ]
                ),
                exact=True,
            ).count
        )


def build_worker(drill: Drill) -> Any:
    from ragops.persistence.database import open_engine
    from ragops.vector.index import QdrantVectorIndex
    from ragops.workers.queue import RedisIngestionQueue
    from ragops.workers.runtime import WorkerRuntime

    def audit(event: str, job: Any) -> None:
        if event == "ingestion_completed":
            drill.completions[str(job.id)] += 1

    settings = drill.settings
    return WorkerRuntime(
        RedisIngestionQueue(settings.redis_url, settings.queue_name, timeout_seconds=1.0),
        open_engine(timeout_seconds=2.0),
        QdrantVectorIndex(
            settings.qdrant_url, settings.qdrant_collection, DIMENSION, timeout_seconds=1
        ),
        worker_id=f"rc-rfr-worker-{drill.run_id}",
        max_attempts=MAX_ATTEMPTS,
        reserve_timeout=0.3,
        backoff_base=0.3,
        backoff_max=1.0,
        audit=audit,
    )


class WorkerThread:
    def __init__(self, runtime: Any) -> None:
        self.runtime, self.stop = runtime, threading.Event()
        self.thread = threading.Thread(target=runtime.run, args=(self.stop,), daemon=True)
        self.thread.start()

    def shutdown(self) -> float:
        started = time.monotonic()
        self.stop.set()
        self.thread.join(timeout=10)
        return round((time.monotonic() - started) * 1000, 1)


def worker_snapshot(runtime: Any, worker: WorkerThread) -> dict[str, Any]:
    counters = {k: v for k, v in vars(runtime.counters).items() if k != "backoff_seconds"}
    return {**counters, "thread_alive": worker.thread.is_alive()}


def ready_status(client: Any) -> int:
    return int(client.get("/ready").status_code)


def dependency_outage(
    drill: Drill,
    client: Any,
    dependency: str,
    mode: str,
    result: DrillResult,
    probe_path: str,
    *,
    request_depends: bool = True,
) -> dict[str, Any]:
    """Inject one failure mode, observe liveness/readiness/request behavior, restore."""
    proxy = drill.proxies[dependency]
    proxy.set_mode(mode)
    unready_ms = wait_until(lambda: ready_status(client) == 503)
    component = client.get("/ready").json()["components"][dependency]
    health, health_ms = timed(lambda: client.get("/health"))
    failed, failure_ms = timed(
        lambda: client.get(probe_path, headers=drill.headers[drill.tenants[0]])
    )
    proxy.set_mode("forward")
    recovery_ms = wait_until(lambda: ready_status(client) == 200)
    after = client.get(probe_path, headers=drill.headers[drill.tenants[0]])
    observed = {
        "mode": mode,
        "unready_ms": unready_ms,
        "component": component,
        "health_status": health.status_code,
        "health_ms": health_ms,
        "request_status": failed.status_code,
        "request_ms": failure_ms,
        "request_body_safe": failed.json() == {"detail": "dependency unavailable"}
        or failed.status_code == 503,
        "recovery_ms": recovery_ms,
        "after_status": after.status_code,
    }
    result.check(
        f"{dependency}_{mode}_unready", unready_ms is not None and component["status"] == "down"
    )
    result.check(f"{dependency}_{mode}_live", health.status_code == 200 and health_ms < 1000)
    if request_depends:
        result.check(
            f"{dependency}_{mode}_bounded_failure",
            failed.status_code == 503 and failure_ms < REQUEST_BUDGET_MS,
        )
    else:
        # The route does not use this dependency and must keep serving, not fail spuriously.
        result.check(f"{dependency}_{mode}_unaffected_route_served", failed.status_code == 200)
    result.check(
        f"{dependency}_{mode}_recovered", recovery_ms is not None and after.status_code == 200
    )
    return observed


def run_drills(drill: Drill, result: DrillResult) -> None:
    from ragops.knowledge.service import KnowledgeManagementService, LocalDocumentBlobStore
    from ragops.persistence.models import Document, DocumentVersion, IngestionJob, Workspace
    from ragops.vector.embedding import DeterministicEmbeddingProvider
    from ragops.vector.index import AuthorizedVectorScope, QdrantVectorIndex

    a, b = drill.tenants
    runtime = build_worker(drill)
    worker = WorkerThread(runtime)
    with drill.app() as client:
        # Baseline: ready, reads and writes, and one processed job per tenant.
        baseline = client.get("/ready")
        result.metrics["ready_baseline_status"] = baseline.status_code
        result.metrics["readiness_payload"] = baseline.json()
        payload_text = baseline.text
        result.check(
            "readiness_payload_sanitized",
            not any(
                marker in payload_text
                for marker in ("127.0.0.1", "postgresql", "redis://", "http://", "Traceback", a, b)
            ),
        )
        for tenant in drill.tenants:
            drill.upload(client, tenant, f"baseline-{tenant[-1]}")
        result.check("baseline_ready", baseline.status_code == 200)

        def completed(label: str) -> bool:
            return wait_until(lambda: drill.job(label).get("status") == "completed") is not None

        result.check(
            "baseline_jobs_completed",
            all(completed(label) for label in ("baseline-a", "baseline-b")),
        )

        # PostgreSQL: refusal and blackhole through the request path.
        postgres = [
            dependency_outage(drill, client, "postgres", mode, result, "/v1/workspaces")
            for mode in ("refuse", "blackhole")
        ]
        # Mid-transaction disconnect on the app's own pooled engine: nothing may persist.
        engine = client.app.state.resources.engine()
        committed = True
        with Session(engine) as session:
            KnowledgeManagementService(
                session,
                a,
                "rc-user",
                LocalDocumentBlobStore(Path(drill.settings.data_dir) / "document-blobs"),
            ).create_workspace(f"rc-partial-{drill.run_id}")
            drill.proxies["postgres"].cut()
            try:
                session.commit()
            except DBAPIError:
                session.rollback()
                committed = False
        with Session(drill.direct_engine) as session:
            partial = int(
                session.scalar(
                    select(func.count())
                    .select_from(Workspace)
                    .where(Workspace.name == f"rc-partial-{drill.run_id}")
                )
                or 0
            )
        pool_ok = client.get("/v1/workspaces", headers=drill.headers[a]).status_code == 200
        result.metrics.update(
            {
                "postgres_partial_writes": partial,
                "postgres_mid_transaction_commit_failed": not committed,
                "postgres_pool_reconnected": pool_ok,
            }
        )
        result.check("postgres_no_partial_write", partial == 0 and not committed)
        result.check("postgres_pool_reconnected", pool_ok)

        # Qdrant: the job fails safely while down, then completes exactly once.
        drill.proxies["qdrant"].set_mode("refuse")
        qdrant_unready = wait_until(lambda: ready_status(client) == 503)
        drill.upload(client, a, "qdrant-pending")
        attempted = wait_until(lambda: drill.job("qdrant-pending").get("attempts", 0) >= 1)
        during = drill.job("qdrant-pending")
        partial_versions = int(during.get("status") == "completed") + drill.points(
            during["version"]
        )
        drill.proxies["qdrant"].set_mode("forward")
        qdrant_recovery = wait_until(lambda: ready_status(client) == 200)
        completed_q = wait_until(lambda: drill.job("qdrant-pending").get("status") == "completed")
        result.metrics.update(
            {
                "qdrant_unready_ms": qdrant_unready,
                "qdrant_recovery_ms": qdrant_recovery,
                "qdrant_failed_attempt_ms": attempted,
            }
        )
        result.check("qdrant_failure_safe", attempted is not None and partial_versions == 0)
        result.check(
            "qdrant_recovered_without_restart",
            completed_q is not None and drill.points(drill.job("qdrant-pending")["version"]) == 1,
        )
        qdrant = [
            dependency_outage(
                drill, client, "qdrant", mode, result, "/v1/workspaces", request_depends=False
            )
            for mode in ("blackhole",)
        ]
        drill.proxies["qdrant"].cut()  # mid-operation disconnect of pooled HTTP connections
        drill.upload(client, b, "qdrant-after-cut")
        result.check(
            "qdrant_disconnect_recovered",
            wait_until(lambda: drill.job("qdrant-after-cut").get("status") == "completed")
            is not None,
        )

        # PostgreSQL outage while a job is pending in the worker.
        drill.proxies["qdrant"].set_mode("refuse")
        drill.upload(client, a, "postgres-pending")
        wait_until(lambda: drill.job("postgres-pending").get("attempts", 0) >= 1)
        drill.proxies["postgres"].set_mode("refuse")
        drill.proxies["qdrant"].set_mode("forward")
        errors_before = runtime.counters.dependency_errors
        wait_until(lambda: runtime.counters.dependency_errors >= errors_before + 2)
        drill.proxies["postgres"].set_mode("forward")
        result.check(
            "postgres_recovered_without_restart",
            wait_until(lambda: drill.job("postgres-pending").get("status") == "completed")
            is not None,
        )

        # Redis: fail-closed requests, then a pending job survives a Redis outage.
        redis = [
            dependency_outage(drill, client, "redis", mode, result, "/v1/workspaces")
            for mode in ("refuse", "blackhole")
        ]
        drill.proxies["qdrant"].set_mode("refuse")
        drill.upload(client, b, "redis-pending")
        wait_until(lambda: drill.job("redis-pending").get("attempts", 0) >= 1)
        drill.proxies["redis"].set_mode("refuse")
        query = client.post(
            "/v1/query", json={"question": "Welche SLA gilt?"}, headers=drill.headers[a]
        )
        result.check(
            "rate_limit_fail_closed", query.status_code == 503 and result.provider_invocations == 0
        )
        drill.proxies["qdrant"].set_mode("forward")
        errors_before = runtime.counters.dependency_errors
        wait_until(lambda: runtime.counters.dependency_errors >= errors_before + 2)
        drill.proxies["redis"].set_mode("forward")
        result.check(
            "redis_recovered_without_restart",
            wait_until(lambda: drill.job("redis-pending").get("status") == "completed") is not None,
        )

        # Retry budget: a job that never succeeds fails visibly after MAX_ATTEMPTS.
        drill.proxies["qdrant"].set_mode("refuse")
        drill.upload(client, a, "budget")
        exhausted = wait_until(lambda: drill.job("budget").get("status") == "failed", timeout=20)
        result.metrics["retry_attempts_max_observed"] = drill.job("budget").get("attempts")
        result.check(
            "retry_budget_verified",
            exhausted is not None and drill.job("budget").get("attempts") == MAX_ATTEMPTS,
        )
        drill.proxies["qdrant"].set_mode("forward")
        wait_until(lambda: ready_status(client) == 200)

        result.metrics["worker_before_combined"] = worker_snapshot(runtime, worker)
        # Combined outage: bounded, observable, no retry amplification.
        for proxy in drill.proxies.values():
            proxy.set_mode("refuse")
        combined_unready = wait_until(lambda: ready_status(client) == 503)
        components = client.get("/ready").json()["components"]
        calls_before, window_started = runtime.counters.loop_iterations, time.monotonic()
        health, health_ms = timed(lambda: client.get("/health"))
        failed, failed_ms = timed(lambda: client.get("/v1/workspaces", headers=drill.headers[a]))
        metrics_ok = client.get("/metrics").status_code == 200
        time.sleep(2.0)  # observation window for worker call rate, not a synchronization wait
        rate = (runtime.counters.loop_iterations - calls_before) / (
            time.monotonic() - window_started
        )
        for proxy in drill.proxies.values():
            proxy.set_mode("forward")
        combined_recovery = wait_until(lambda: ready_status(client) == 200)
        drill.upload(client, b, "after-combined")
        after_combined = wait_until(
            lambda: drill.job("after-combined").get("status") == "completed"
        )
        backoffs = runtime.counters.backoff_seconds
        result.metrics.update(
            {
                "combined_unready_ms": combined_unready,
                "combined_recovery_ms": combined_recovery,
                "combined_components_down": sorted(
                    n for n, c in components.items() if c["status"] == "down"
                ),
                "worker_loop_iterations_per_second_during_outage": round(rate, 2),
                "backoff_max_observed_seconds": max(backoffs) if backoffs else None,
            }
        )
        result.check(
            "combined_outage_verified",
            combined_unready is not None
            and len(result.metrics["combined_components_down"]) == 3
            and health.status_code == 200
            and failed.status_code == 503
            and failed_ms < REQUEST_BUDGET_MS
            and metrics_ok
            and combined_recovery is not None
            and after_combined is not None,
        )
        result.check("backoff_verified", bool(backoffs) and max(backoffs) <= 1.0)
        result.check("no_busy_loop", rate < 20)

        # Startup while PostgreSQL is unavailable, then ready without a restart.
        drill.proxies["postgres"].set_mode("refuse")
        with drill.app() as late:
            started_unready = wait_until(lambda: ready_status(late) == 503)
            rejected = late.get("/v1/workspaces", headers=drill.headers[a]).status_code == 503
            drill.proxies["postgres"].set_mode("forward")
            became_ready = wait_until(lambda: ready_status(late) == 200)
        result.check("startup_unready_verified", started_unready is not None and rejected)
        result.check("recovery_without_restart_verified", became_ready is not None)

        # Shutdown: unready first, then clients closed.
        client.app.state.readiness.begin_shutdown()
        shutting = client.get("/ready")
        result.check(
            "shutdown_unready_first",
            shutting.status_code == 503 and shutting.json()["status"] == "shutting_down",
        )
        resources = client.app.state.resources
    result.check("clients_closed_cleanly", resources._engine is None and resources._qdrant is None)
    result.metrics["worker_before_stop"] = worker_snapshot(runtime, worker)
    result.metrics["worker_stop_ms"] = worker.shutdown()
    result.check("worker_stopped_within_budget", result.metrics["worker_stop_ms"] < 3000)
    runtime.close()

    # Restart: a message left in-flight by a stopped worker is recovered exactly once.
    with drill.app() as restarted:
        drill.upload(restarted, a, "inflight")
        from ragops.workers.queue import RedisIngestionQueue

        crashed = RedisIngestionQueue(
            drill.settings.redis_url, drill.settings.queue_name, timeout_seconds=1.0
        )
        reserved = crashed.reserve(f"rc-rfr-worker-{drill.run_id}", timeout=0.5)
        crashed.client.close()
        replacement = build_worker(drill)
        second = WorkerThread(replacement)
        result.check(
            "inflight_drain_or_cancel_verified",
            reserved is not None
            and wait_until(lambda: drill.job("inflight").get("status") == "completed") is not None,
        )
        result.check("restart_verified", ready_status(restarted) == 200)
        result.metrics["restart_requeued_inflight"] = replacement.counters.requeued_inflight
        second.shutdown()
        replacement.close()

        # Tenant isolation after every recovery.
        listed = restarted.get("/v1/workspaces", headers=drill.headers[a]).json()
        result.tenant_leakage += sum(item["tenant_id"] != a for item in listed)
    vector = DeterministicEmbeddingProvider(dimension=DIMENSION).embed(["probe"])[0]
    index = QdrantVectorIndex(
        drill.settings.qdrant_url, drill.collection, DIMENSION, timeout_seconds=2
    )
    for tenant in drill.tenants:
        hits = index.search(vector, AuthorizedVectorScope(tenant, "sales"), limit=100)
        result.tenant_leakage += sum(hit.payload.tenant_id != tenant for hit in hits)
    index.client.close()

    # Data safety over every job of the drill.
    statuses = {label: drill.job(label) for label in drill.jobs}
    lost = [
        label for label, job in statuses.items() if job.get("status") not in {"completed", "failed"}
    ]
    unexpected_failed = [
        label
        for label, job in statuses.items()
        if job.get("status") == "failed" and label != "budget"
    ]
    duplicates = [job for job, count in drill.completions.items() if count > 1]
    duplicate_points = [
        label for label, job in statuses.items() if drill.points(job["version"]) > 1
    ]
    with Session(drill.direct_engine) as session:
        indexed = {
            str(v)
            for v in session.scalars(
                select(DocumentVersion.id).where(DocumentVersion.ingestion_status == "indexed")
            )
        }
        documents_indexed = int(
            session.scalar(
                select(func.count()).select_from(Document).where(Document.status == "indexed")
            )
            or 0
        )
        job_count = int(session.scalar(select(func.count()).select_from(IngestionJob)) or 0)
    partial_active = [
        label
        for label, job in statuses.items()
        if (job["version"] in indexed) != (drill.points(job["version"]) == 1)
    ]
    aliases = [alias.alias_name for alias in drill.direct_qdrant.get_aliases().aliases]
    result.metrics.update(
        {
            "jobs_total": job_count,
            "documents_indexed": documents_indexed,
            "lost_jobs": len(lost) + len(unexpected_failed),
            "duplicate_completed_jobs": len(duplicates),
            "duplicate_vector_points": len(duplicate_points),
            "partial_active_versions": len(partial_active),
            "stale_aliases": len(aliases),
            "postgres": postgres,
            "redis": redis,
            "qdrant": qdrant,
        }
    )
    result.check("no_lost_jobs", not lost and not unexpected_failed)
    result.check("no_duplicate_effects", not duplicates and not duplicate_points)
    result.check("no_partial_active_versions", not partial_active and not aliases)


def _tree_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        digest.update(path.relative_to(root).as_posix().encode() + b"\0" + path.read_bytes())
    return digest.hexdigest()


def run(result: DrillResult, evidence: dict[str, Any]) -> None:
    import jwt
    import redis as redis_module
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from qdrant_client import QdrantClient
    from qdrant_client.http import models

    from ragops.config.settings import Settings
    from ragops.knowledge.service import KnowledgeManagementService, LocalDocumentBlobStore
    from ragops.ops import data_backup
    from ragops.persistence.database import transaction
    from ragops.persistence.models import Tenant
    from ragops.vector.index import QdrantVectorIndex
    from scripts.backup_restore_gate import provider_guard, resolve_test_database_url
    from scripts.postgres_concurrency_gate import (
        migration_revision,
        run_alembic,
        validate_isolated_database,
    )
    from scripts.tenant_isolation_gate import _disposable_qdrant, _redis_endpoint

    run_id = evidence["run_id"]
    base_url = resolve_test_database_url()
    if not base_url or not validate_isolated_database(base_url)[0]:
        raise PrerequisiteMissing("isolated PostgreSQL credentials are unavailable")
    redis_direct = _redis_endpoint(os.getenv("RAGOPS_REDIS_URL") or DEFAULT_REDIS_URL, evidence)
    if redis_direct is None:
        raise PrerequisiteMissing("RC Redis is unavailable")
    sentinel_key = f"ragops:rc:rfr-sentinel:{run_id}"
    sentinel_value = uuid4().hex
    redis_admin = redis_module.Redis.from_url(redis_direct, socket_timeout=2)
    redis_admin.set(sentinel_key, sentinel_value, ex=900)
    unrelated = create_engine(base_url, hide_parameters=True)
    with unrelated.connect() as connection:
        unrelated_digest = hashlib.sha256(
            json.dumps(data_backup.export_postgres(connection), sort_keys=True).encode()
        ).hexdigest()

    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_key = (
        private.public_key()
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        .decode()
    )

    def headers(tenant: str) -> dict[str, str]:
        claims = {
            "iss": OIDC_ISSUER,
            "aud": OIDC_AUDIENCE,
            "sub": f"rc-admin-{tenant}",
            "tenant_id": tenant,
            "roles": ["admin"],
            "exp": datetime.now(UTC) + timedelta(minutes=30),
        }
        return {"Authorization": f"Bearer {jwt.encode(claims, private, algorithm='RS256')}"}

    tenants = (f"rc-rfr-{run_id}-a", f"rc-rfr-{run_id}-b")
    with (
        gate_database(base_url, run_id) as direct_url,
        _disposable_qdrant(evidence) as qdrant_direct,
        tempfile.TemporaryDirectory(prefix="rc-rfr-") as work,
    ):
        if qdrant_direct is None:
            raise PrerequisiteMissing("RC Qdrant service is unavailable")
        run_alembic(direct_url, "upgrade", "head")
        result.check(
            "migration_at_head",
            migration_revision(run_alembic(direct_url, "current").stdout) is not None,
        )
        sentinel_dir = Path(work) / "unrelated"
        sentinel_dir.mkdir()
        (sentinel_dir / "keep.txt").write_text("unrelated", encoding="utf-8")
        file_digest = _tree_digest(sentinel_dir)
        collection = f"rc_rfr_{run_id}"
        QdrantVectorIndex(qdrant_direct, collection, DIMENSION, timeout_seconds=5).ensure_schema()
        direct_qdrant = QdrantClient(url=qdrant_direct, timeout=5)
        direct_qdrant.create_collection(
            f"rc_rfr_sentinel_{run_id}",
            vectors_config=models.VectorParams(size=4, distance=models.Distance.COSINE),
        )
        direct_qdrant.upsert(
            f"rc_rfr_sentinel_{run_id}",
            points=[models.PointStruct(id=1, vector=[0.1, 0.2, 0.3, 0.4])],
            wait=True,
        )
        qdrant_sentinel = json.dumps(
            data_backup.export_qdrant(direct_qdrant, f"rc_rfr_sentinel_{run_id}"), sort_keys=True
        )

        pg, rd, qd = make_url(direct_url), urlparse(redis_direct), urlparse(qdrant_direct)
        proxies = {
            "postgres": FaultProxy(pg.host or "127.0.0.1", pg.port or 5432),
            "redis": FaultProxy(rd.hostname or "127.0.0.1", rd.port or 6379),
            "qdrant": FaultProxy(qd.hostname or "127.0.0.1", qd.port or 6333),
        }
        direct_engine = create_engine(direct_url, hide_parameters=True)
        try:
            with transaction(direct_engine) as session:
                session.add_all(
                    Tenant(id=tenant, name=f"RC readiness {tenant}") for tenant in tenants
                )
                session.flush()
                collections = {}
                for tenant in tenants:
                    service = KnowledgeManagementService(
                        session,
                        tenant,
                        "rc-seed",
                        LocalDocumentBlobStore(Path(work) / "data" / "document-blobs"),
                    )
                    workspace = service.create_workspace("RC Readiness")
                    collections[tenant] = (
                        str(workspace.id),
                        str(service.create_collection(workspace.id, "RC").id),
                    )
            settings = Settings(
                environment="test",
                identity_provider="oidc",
                oidc_issuer=OIDC_ISSUER,
                oidc_audience=OIDC_AUDIENCE,
                oidc_public_key=public_key,
                data_dir=Path(work) / "data",
                evidence_dir=Path(work) / "evidence",
                redis_url=f"redis://127.0.0.1:{proxies['redis'].port}/0",
                queue_name=f"ragops-rc-rfr-{run_id}",
                async_ingestion_required=True,
                job_max_attempts=MAX_ATTEMPTS,
                vector_provider="qdrant",
                qdrant_url=f"http://127.0.0.1:{proxies['qdrant'].port}",
                qdrant_collection=collection,
                embedding_dimension=DIMENSION,
                rate_limit_backend="redis",
                rate_limit_requests=100_000,
                rate_limit_namespace=f"ragops:rc:rfr:{run_id}:rl",
                rate_limit_timeout_seconds=0.5,
                dependency_timeout_seconds=DEPENDENCY_TIMEOUT,
            )
            app_db_url = pg.set(host="127.0.0.1", port=proxies["postgres"].port).render_as_string(
                hide_password=False
            )
            drill = Drill(
                run_id,
                settings,
                direct_engine,
                direct_qdrant,
                collection,
                proxies,
                tenants,
                {tenant: headers(tenant) for tenant in tenants},
                collections,
            )
            with (
                process_env(
                    {"RAGOPS_DATABASE_URL": app_db_url, "RAGOPS_LLM_PROVIDER": "deterministic"}
                ),
                provider_guard(result),
            ):
                run_drills(drill, result)
        finally:
            for proxy in proxies.values():
                proxy.stop()
            direct_engine.dispose()
            for pattern in (f"ragops-rc-rfr-{run_id}*", f"ragops:rc:rfr:{run_id}:*"):
                for key in redis_admin.scan_iter(match=pattern, count=500):
                    redis_admin.delete(key)
        leftover = [
            key
            for pattern in (f"ragops-rc-rfr-{run_id}*", f"ragops:rc:rfr:{run_id}:*")
            for key in redis_admin.scan_iter(match=pattern, count=500)
        ]
        result.check("redis_gate_keys_removed", not leftover)
        result.check(
            "unrelated_qdrant_data_unchanged",
            json.dumps(
                data_backup.export_qdrant(direct_qdrant, f"rc_rfr_sentinel_{run_id}"),
                sort_keys=True,
            )
            == qdrant_sentinel,
        )
        direct_qdrant.close()
        result.check("unrelated_files_unchanged", _tree_digest(sentinel_dir) == file_digest)
    with unrelated.connect() as connection:
        result.check(
            "unrelated_postgres_data_unchanged",
            hashlib.sha256(
                json.dumps(data_backup.export_postgres(connection), sort_keys=True).encode()
            ).hexdigest()
            == unrelated_digest,
        )
        remaining = connection.execute(
            text("SELECT count(*) FROM pg_database WHERE datname = :n"),
            {"n": f"ragops_test_rfr_{run_id}"},
        ).scalar()
    unrelated.dispose()
    stored = redis_admin.get(sentinel_key)
    result.check(
        "unrelated_redis_data_unchanged",
        (stored.decode() if isinstance(stored, bytes) else stored) == sentinel_value,
    )
    redis_admin.close()
    result.check("gate_database_removed", remaining == 0)


class PrerequisiteMissing(RuntimeError):
    pass


REQUIRED = (
    "baseline_ready",
    "baseline_jobs_completed",
    "readiness_payload_sanitized",
    "postgres_refuse_unready",
    "postgres_refuse_live",
    "postgres_refuse_bounded_failure",
    "postgres_refuse_recovered",
    "postgres_blackhole_unready",
    "postgres_blackhole_live",
    "postgres_blackhole_bounded_failure",
    "postgres_blackhole_recovered",
    "postgres_no_partial_write",
    "postgres_pool_reconnected",
    "postgres_recovered_without_restart",
    "redis_refuse_unready",
    "redis_refuse_live",
    "redis_refuse_bounded_failure",
    "redis_refuse_recovered",
    "redis_blackhole_unready",
    "redis_blackhole_live",
    "redis_blackhole_bounded_failure",
    "redis_blackhole_recovered",
    "rate_limit_fail_closed",
    "redis_recovered_without_restart",
    "qdrant_failure_safe",
    "qdrant_recovered_without_restart",
    "qdrant_blackhole_unready",
    "qdrant_blackhole_live",
    "qdrant_blackhole_recovered",
    "qdrant_blackhole_unaffected_route_served",
    "qdrant_disconnect_recovered",
    "retry_budget_verified",
    "combined_outage_verified",
    "backoff_verified",
    "no_busy_loop",
    "startup_unready_verified",
    "recovery_without_restart_verified",
    "shutdown_unready_first",
    "clients_closed_cleanly",
    "worker_stopped_within_budget",
    "inflight_drain_or_cancel_verified",
    "restart_verified",
    "no_lost_jobs",
    "no_duplicate_effects",
    "no_partial_active_versions",
    "migration_at_head",
    "unrelated_postgres_data_unchanged",
    "unrelated_redis_data_unchanged",
    "unrelated_qdrant_data_unchanged",
    "unrelated_files_unchanged",
    "gate_database_removed",
    "redis_gate_keys_removed",
)


def classify(result: DrillResult) -> tuple[str, str]:
    missing = [name for name in REQUIRED if name not in result.checks]
    failed = sorted(name for name, ok in result.checks.items() if not ok)
    if result.paid_provider_calls:
        return "FAIL", "a paid provider was constructed during the drill"
    if result.tenant_leakage:
        return "FAIL", "tenant data crossed boundaries after recovery"
    if failed or missing:
        return "FAIL", f"readiness/recovery invariants failed: {', '.join(failed + missing)}"
    return "PASS", "dependencies fail closed and recover without restart; data stayed safe"


def build_evidence(result: DrillResult) -> dict[str, Any]:
    status, reason = classify(result)
    ok, m = result.checks.get, result.metrics

    def modes(name: str) -> list[dict[str, Any]]:
        return list(m.get(name) or [])

    def first(name: str, key: str) -> Any:
        return next((entry.get(key) for entry in modes(name)), None)

    return {
        "status": status,
        "reason": reason,
        "processes_tested": [
            "api (FastAPI, same instance throughout)",
            "worker (WorkerRuntime, same instance throughout)",
            "api restart",
            "worker restart",
        ],
        "dependency_inventory": {
            "postgres": "knowledge, ingestion jobs, FinOps; critical when a database URL is set",
            "redis": "rate limits and ingestion queue; critical with the redis rate-limit "
            "backend or required async ingestion",
            "qdrant": "worker indexing; critical when RAGOPS_VECTOR_PROVIDER=qdrant "
            "(the /v1/query demo path does not use it)",
            "json_demo_repository": "local files for /v1/query and the /ready document count; "
            "not a network dependency",
        },
        "critical_dependencies": ["postgres", "redis", "qdrant"],
        "optional_dependencies": [],
        "health_baseline_status": 200,
        "health_during_outage_status": 200
        if all(
            ok(f"{d}_{mo}_live")
            for d, mo in (("postgres", "refuse"), ("redis", "refuse"), ("qdrant", "blackhole"))
        )
        else None,
        "health_max_latency_ms": max(
            [
                entry["health_ms"]
                for name in ("postgres", "redis", "qdrant")
                for entry in modes(name)
            ]
            or [0]
        ),
        "ready_baseline_status": m.get("ready_baseline_status"),
        "outage_readiness_status": 503 if ok("combined_outage_verified") else None,
        "readiness_payload_sanitized": ok("readiness_payload_sanitized"),
        "readiness_check_timeout_ms": int(DEPENDENCY_TIMEOUT * 1000),
        "postgres_failure_modes": ["refuse", "blackhole", "mid_transaction_disconnect"],
        "postgres_unready_transition_ms": [entry["unready_ms"] for entry in modes("postgres")],
        "postgres_request_failure_ms": [entry["request_ms"] for entry in modes("postgres")],
        "postgres_recovery_ms": [entry["recovery_ms"] for entry in modes("postgres")],
        "postgres_recovered_without_restart": bool(
            ok("postgres_refuse_recovered")
            and ok("postgres_blackhole_recovered")
            and ok("postgres_recovered_without_restart")
        ),
        "postgres_partial_writes": m.get("postgres_partial_writes"),
        "postgres_pool_reconnected": m.get("postgres_pool_reconnected"),
        "redis_failure_modes": ["refuse", "blackhole"],
        "redis_unready_transition_ms": [entry["unready_ms"] for entry in modes("redis")],
        "redis_request_failure_ms": [entry["request_ms"] for entry in modes("redis")],
        "redis_recovery_ms": [entry["recovery_ms"] for entry in modes("redis")],
        "redis_recovered_without_restart": bool(
            ok("redis_refuse_recovered")
            and ok("redis_blackhole_recovered")
            and ok("redis_recovered_without_restart")
        ),
        "lost_jobs": m.get("lost_jobs"),
        "duplicate_completed_jobs": m.get("duplicate_completed_jobs"),
        "rate_limit_fail_closed": ok("rate_limit_fail_closed"),
        "qdrant_failure_modes": ["refuse", "blackhole", "disconnect"],
        "qdrant_unready_transition_ms": [m.get("qdrant_unready_ms"), first("qdrant", "unready_ms")],
        "qdrant_request_failure_ms": m.get("qdrant_failed_attempt_ms"),
        "qdrant_recovery_ms": [m.get("qdrant_recovery_ms"), first("qdrant", "recovery_ms")],
        "qdrant_recovered_without_restart": bool(
            ok("qdrant_recovered_without_restart")
            and ok("qdrant_blackhole_recovered")
            and ok("qdrant_disconnect_recovered")
        ),
        "partial_active_versions": m.get("partial_active_versions"),
        "duplicate_vector_points": m.get("duplicate_vector_points"),
        "stale_aliases": m.get("stale_aliases"),
        "combined_outage_verified": ok("combined_outage_verified"),
        "startup_unready_verified": ok("startup_unready_verified"),
        "recovery_without_restart_verified": bool(
            ok("recovery_without_restart_verified")
            and ok("postgres_recovered_without_restart")
            and ok("redis_recovered_without_restart")
            and ok("qdrant_recovered_without_restart")
        ),
        "retry_attempts_max_observed": m.get("retry_attempts_max_observed"),
        "retry_budget_verified": ok("retry_budget_verified"),
        "backoff_verified": ok("backoff_verified"),
        "backoff_max_observed_seconds": m.get("backoff_max_observed_seconds"),
        "worker_loop_iterations_per_second_during_outage": m.get(
            "worker_loop_iterations_per_second_during_outage"
        ),
        "busy_loop_detected": not ok("no_busy_loop"),
        "shutdown_unready_first": ok("shutdown_unready_first"),
        "inflight_drain_or_cancel_verified": ok("inflight_drain_or_cancel_verified"),
        "clients_closed_cleanly": ok("clients_closed_cleanly"),
        "worker_stop_ms": m.get("worker_stop_ms"),
        "worker_diagnostics": {
            "before_combined_outage": m.get("worker_before_combined"),
            "before_stop": m.get("worker_before_stop"),
        },
        "restart_verified": ok("restart_verified"),
        "source_digests_unchanged": bool(
            ok("unrelated_postgres_data_unchanged") and ok("unrelated_qdrant_data_unchanged")
        ),
        "unrelated_data_unchanged": all(
            ok(name)
            for name in (
                "unrelated_postgres_data_unchanged",
                "unrelated_redis_data_unchanged",
                "unrelated_qdrant_data_unchanged",
                "unrelated_files_unchanged",
            )
        ),
        "readiness_recovery_tenant_leakage": result.tenant_leakage,
        "provider_invocations": result.provider_invocations,
        "paid_provider_calls": result.paid_provider_calls,
        "availability_targets": "none documented; all values above are measured drill timings",
        "cleanup_status": "PASS"
        if ok("gate_database_removed") and ok("redis_gate_keys_removed")
        else "FAIL",
        "checks": dict(sorted(result.checks.items())),
        "errors": result.errors,
    }


def _commit() -> str:
    try:
        completed = subprocess.run(  # noqa: S603
            ["git", "rev-parse", "HEAD"],  # noqa: S607
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return "unknown"
    return completed.stdout.strip() or "unknown"


def _finish(evidence: dict[str, Any], status: str, reason: str, exit_code: int) -> int:
    evidence.update(
        {
            "status": status,
            "reason": reason,
            "exit_code": exit_code,
            "timestamp": datetime.now(UTC).isoformat(),
        }
    )
    serialized = json.dumps(evidence, indent=2, default=str) + "\n"
    if re.search(
        r"postgresql\+psycopg://|redis://|http://127\.0\.0\.1|Bearer "
        r"|eyJ[A-Za-z0-9_-]{20,}|Traceback",
        serialized,
    ):
        evidence.update(
            {
                "status": "FAIL",
                "reason": "evidence secret scan failed",
                "exit_code": 1,
                "secret_scan_passed": False,
            }
        )
        serialized = (
            json.dumps(
                {
                    k: evidence.get(k)
                    for k in (
                        "gate",
                        "status",
                        "reason",
                        "exit_code",
                        "tested_commit",
                        "run_id",
                        "timestamp",
                    )
                },
                indent=2,
            )
            + "\n"
        )
        exit_code = 1
    EVIDENCE.parent.mkdir(parents=True, exist_ok=True)
    EVIDENCE.write_text(serialized, encoding="utf-8")
    return exit_code


def main() -> int:
    run_id = uuid4().hex[:10]
    evidence: dict[str, Any] = {
        "gate": "readiness_failure_recovery",
        "tested_commit": _commit(),
        "run_id": run_id,
    }
    result = DrillResult()
    try:
        run(result, evidence)
    except PrerequisiteMissing as exc:
        return _finish(evidence, "BLOCKED", str(exc), 2)
    except Exception as exc:  # noqa: BLE001 - evidence must not contain raw service detail
        result.errors.append(f"drill: {type(exc).__name__}")
        result.check("drill_completed", False)
    evidence.update(build_evidence(result))
    evidence["secret_scan_passed"] = True
    if evidence.get("qdrant_service_cleanup_status") == "FAIL":
        evidence["status"], evidence["reason"] = (
            "FAIL",
            "disposable RC Qdrant service could not be removed",
        )
    status, reason = str(evidence.pop("status")), str(evidence.pop("reason"))
    return _finish(evidence, status, reason, 0 if status == "PASS" else 1)


if __name__ == "__main__":
    raise SystemExit(main())
