"""FastAPI application factory."""
# FastAPI dependency defaults are intentional route declarations.
# ruff: noqa: B008

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import PlainTextResponse
from sqlalchemy.orm import Session

from ragops import __version__
from ragops.api.schemas import (
    CitationApi,
    QueryApiRequest,
    QueryApiResponse,
    QueryMetricsApi,
)
from ragops.auth.dependencies import admin_dependency, identity_dependency
from ragops.auth.identity import AuthenticatedUserContext
from ragops.config.settings import Settings
from ragops.finops.service import BudgetState
from ragops.governance.audit import AuditLogger
from ragops.knowledge.service import (
    KnowledgeAuthorizationError,
    KnowledgeManagementService,
    LocalDocumentBlobStore,
)
from ragops.modeling.router import (
    ComplexitySignals,
    FallbackPolicyError,
    LLMModelRouter,
    ModelSpec,
    RoutingClass,
    TenantModelPolicy,
)
from ragops.monitoring.metrics import MetricsRegistry
from ragops.persistence.database import open_engine
from ragops.storage.json_store import JsonRepository
from ragops.storage.models import QueryUser
from ragops.workers.queue import IngestionMessage, InMemoryIngestionQueue
from ragops.workers.worker import IngestionWorker
from ragops.workflows.state_machine import QueryRequest, RAGWorkflow


def _workflow(settings: Settings) -> RAGWorkflow:
    repository = JsonRepository(settings.data_dir)
    metrics = MetricsRegistry()
    audit = AuditLogger(settings.evidence_dir / "audit-events.jsonl")
    return RAGWorkflow(repository, audit_logger=audit, metrics=metrics)


def resolve_authenticated_identity(
    settings: Settings,
    trusted: AuthenticatedUserContext,
    request: QueryApiRequest,
) -> AuthenticatedUserContext:
    """Apply legacy client identity only under explicit development config."""
    if settings.identity_provider != "development":
        return trusted
    if settings.environment not in {"local", "development", "test", "demo"}:
        return trusted
    if not (request.user_id and request.tenant_id and request.role):
        return trusted
    return AuthenticatedUserContext(
        user_id=request.user_id,
        tenant_id=request.tenant_id,
        roles=(request.role,),
        display_name=trusted.display_name,
        email=trusted.email,
        issuer="development-legacy-explicit",
        subject=request.user_id,
    )


_POLICY_NAME_SETS = ("allowed_providers", "allowed_models", "high_risk_models")
_POLICY_FIELDS = frozenset(
    {*_POLICY_NAME_SETS, "default_tier", "max_routing_tier", "cost_ceiling_eur", "fallback_enabled"}
)


def _policy_from_payload(tenant_id: str, payload: dict[str, object]) -> TenantModelPolicy:
    """Map an admin payload onto the router's existing policy fields; reject anything else."""
    unknown = sorted(set(payload) - _POLICY_FIELDS)
    if unknown:
        raise HTTPException(status_code=422, detail=f"unknown policy fields: {', '.join(unknown)}")
    names: dict[str, frozenset[str]] = {}
    for field in _POLICY_NAME_SETS:
        value = payload.get(field, [])
        if not isinstance(value, list) or not all(
            isinstance(item, str) and item.strip() for item in value
        ):
            raise HTTPException(status_code=422, detail=f"{field} must be a list of names")
        names[field] = frozenset(item.strip() for item in value)
    tiers: dict[str, RoutingClass] = {}
    for field, default in (("default_tier", "STANDARD"), ("max_routing_tier", "COMPLEX")):
        try:
            tiers[field] = RoutingClass(str(payload.get(field, default)))
        except ValueError:
            raise HTTPException(status_code=422, detail=f"{field} is invalid") from None
    ceiling = payload.get("cost_ceiling_eur")
    if ceiling is not None and (
        isinstance(ceiling, bool) or not isinstance(ceiling, int | float) or ceiling < 0
    ):
        raise HTTPException(
            status_code=422, detail="cost_ceiling_eur must be a non-negative number"
        )
    fallback = payload.get("fallback_enabled", True)
    if not isinstance(fallback, bool):
        raise HTTPException(status_code=422, detail="fallback_enabled must be a boolean")
    return TenantModelPolicy(
        tenant_id,
        allowed_providers=names["allowed_providers"],
        allowed_models=names["allowed_models"],
        default_tier=tiers["default_tier"],
        max_routing_tier=tiers["max_routing_tier"],
        high_risk_models=names["high_risk_models"],
        cost_ceiling_eur=None if ceiling is None else float(ceiling),
        fallback_enabled=fallback,
    )


def _policy_view(policy: TenantModelPolicy) -> dict[str, object]:
    return {
        "tenant_id": policy.tenant_id,
        "allowed_providers": sorted(policy.allowed_providers),
        "allowed_models": sorted(policy.allowed_models),
        "high_risk_models": sorted(policy.high_risk_models),
        "default_tier": policy.default_tier.value,
        "max_routing_tier": policy.max_routing_tier.value,
        "cost_ceiling_eur": policy.cost_ceiling_eur,
        "fallback_enabled": policy.fallback_enabled,
    }


def _int_payload(value: object, default: int = 0) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int | float | str):
        try:
            return int(value)
        except ValueError:
            return default
    return default


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    settings.validate_identity_configuration()
    settings.validate_async_configuration()
    settings.validate_vector_configuration()
    settings.validate_model_configuration()
    settings.require_supported_runtime()
    app = FastAPI(title="RAGOps Enterprise Copilot", version=__version__)
    workflow = _workflow(settings)
    ingestion_queue = InMemoryIngestionQueue()
    ingestion_metrics = {"jobs_total": 0, "failures_total": 0, "retries_total": 0}
    model_catalog = [
        ModelSpec(
            "deterministic",
            "deterministic-ragops-v1",
            "Demo deterministic",
            routing_tier=RoutingClass.SIMPLE,
        ),
    ]
    model_policies: dict[str, TenantModelPolicy] = {}
    budget_records: dict[str, dict[str, object]] = {}
    identity = identity_dependency(settings)
    admin = admin_dependency(settings)

    def admin_or_development(
        trusted: AuthenticatedUserContext = Depends(identity),  # noqa: B008
    ) -> AuthenticatedUserContext:
        if settings.identity_provider == "development":
            return trusted
        return admin(trusted)

    @app.get("/v1/admin/providers")
    def admin_providers(
        _identity: AuthenticatedUserContext = Depends(admin),  # noqa: B008
    ) -> list[dict[str, object]]:
        return [
            {
                "provider_id": provider,
                "enabled": True,
                "health": "unknown" if provider != "deterministic" else "healthy",
            }
            for provider in sorted({model.provider_id for model in model_catalog})
        ]

    @app.post("/v1/admin/providers", status_code=201)
    def create_admin_provider(
        payload: dict[str, object],
        _identity: AuthenticatedUserContext = Depends(admin),  # noqa: B008
    ) -> dict[str, object]:
        provider_id = str(payload.get("provider_id", "")).strip()
        if not provider_id or any(model.provider_id == provider_id for model in model_catalog):
            raise HTTPException(
                status_code=400, detail="provider_id is required and must be unique"
            )
        # New entries carry no adapter or cost metadata yet: not routable until enabled.
        model_catalog.append(ModelSpec(provider_id, "default", provider_id, enabled=False))
        return {"provider_id": provider_id, "enabled": False}

    @app.patch("/v1/admin/providers/{provider_id}")
    def update_admin_provider(
        provider_id: str,
        payload: dict[str, object],
        _identity: AuthenticatedUserContext = Depends(admin),  # noqa: B008
    ) -> dict[str, object]:
        if not any(model.provider_id == provider_id for model in model_catalog):
            raise HTTPException(status_code=404, detail="provider not found")
        return {"provider_id": provider_id, "enabled": bool(payload.get("enabled", True))}

    @app.get("/v1/admin/models")
    def admin_models(
        _identity: AuthenticatedUserContext = Depends(admin),  # noqa: B008
    ) -> list[dict[str, object]]:
        return [
            {
                "provider_id": model.provider_id,
                "model_id": model.model_id,
                "display_name": model.display_name,
                "enabled": model.enabled,
                "routing_tier": model.routing_tier.value,
                "high_risk_allowed": model.high_risk_allowed,
            }
            for model in model_catalog
        ]

    @app.post("/v1/admin/models", status_code=201)
    def create_admin_model(
        payload: dict[str, object],
        _identity: AuthenticatedUserContext = Depends(admin),  # noqa: B008
    ) -> dict[str, object]:
        provider_id = str(payload.get("provider_id", "")).strip()
        model_id = str(payload.get("model_id", "")).strip()
        if not provider_id or not model_id:
            raise HTTPException(status_code=400, detail="provider_id and model_id are required")
        model = ModelSpec(
            provider_id, model_id, str(payload.get("display_name", model_id)), enabled=False
        )
        if any(m.provider_id == provider_id and m.model_id == model_id for m in model_catalog):
            raise HTTPException(status_code=409, detail="model already exists")
        model_catalog.append(model)
        return {"provider_id": provider_id, "model_id": model_id, "enabled": False}

    @app.patch("/v1/admin/models/{provider_id}/{model_id}")
    def update_admin_model(
        provider_id: str,
        model_id: str,
        payload: dict[str, object],
        _identity: AuthenticatedUserContext = Depends(admin),  # noqa: B008
    ) -> dict[str, object]:
        enabled = payload.get("enabled")
        if not isinstance(enabled, bool):
            raise HTTPException(status_code=422, detail="enabled must be a boolean")
        for index, model in enumerate(model_catalog):
            if model.provider_id == provider_id and model.model_id == model_id:
                # replace() keeps cost, capability and priority metadata intact.
                model_catalog[index] = replace(model, enabled=enabled)
                return {
                    "provider_id": provider_id,
                    "model_id": model_id,
                    "enabled": model_catalog[index].enabled,
                }
        raise HTTPException(status_code=404, detail="model not found")

    @app.get("/v1/admin/model-policies")
    def list_model_policies(
        identity_context: AuthenticatedUserContext = Depends(admin),  # noqa: B008
    ) -> list[dict[str, object]]:
        return [
            _policy_view(policy)
            for tenant, policy in model_policies.items()
            if tenant == identity_context.tenant_id
        ]

    @app.put("/v1/admin/model-policies/{tenant_id}")
    def put_model_policy(
        tenant_id: str,
        payload: dict[str, object],
        identity_context: AuthenticatedUserContext = Depends(admin),  # noqa: B008
    ) -> dict[str, object]:
        if tenant_id != identity_context.tenant_id:
            raise HTTPException(status_code=404, detail="policy not found")
        model_policies[tenant_id] = _policy_from_payload(tenant_id, payload)
        return {"tenant_id": tenant_id, "updated": True}

    @app.get("/v1/admin/budgets")
    def list_finops_budgets(
        identity_context: AuthenticatedUserContext = Depends(admin),
    ) -> list[dict[str, object]]:  # noqa: B008
        return [
            value
            for value in budget_records.values()
            if value.get("tenant_id") == identity_context.tenant_id
        ]

    @app.post("/v1/admin/budgets", status_code=201)
    def create_finops_budget(
        payload: dict[str, object], identity_context: AuthenticatedUserContext = Depends(admin)
    ) -> dict[str, object]:  # noqa: B008
        amount = float(str(payload.get("budget_amount", 0)))
        if amount <= 0 or str(payload.get("currency", "EUR")) != "EUR":
            raise HTTPException(status_code=400, detail="invalid budget")
        budget_id = str(len(budget_records) + 1)
        record = {
            "id": budget_id,
            "tenant_id": identity_context.tenant_id,
            "budget_amount": amount,
            "currency": "EUR",
            "enforcement_mode": str(payload.get("enforcement_mode", "optimize")),
        }
        budget_records[budget_id] = record
        return record

    @app.patch("/v1/admin/budgets/{budget_id}")
    def update_finops_budget(
        budget_id: str,
        payload: dict[str, object],
        identity_context: AuthenticatedUserContext = Depends(admin),
    ) -> dict[str, object]:  # noqa: B008
        record = budget_records.get(budget_id)
        if record is None or record.get("tenant_id") != identity_context.tenant_id:
            raise HTTPException(status_code=404, detail="budget not found")
        if "budget_amount" in payload and float(str(payload["budget_amount"])) <= 0:
            raise HTTPException(status_code=400, detail="invalid budget")
        record.update(
            {key: payload[key] for key in ("budget_amount", "enforcement_mode") if key in payload}
        )
        return record

    @app.patch("/v1/admin/quotas/{quota_id}")
    def update_finops_quota(
        quota_id: str,
        _payload: dict[str, object],
        _identity: AuthenticatedUserContext = Depends(admin),
    ) -> dict[str, object]:  # noqa: B008
        if not quota_id:
            raise HTTPException(status_code=404, detail="quota not found")
        return {"id": quota_id, "status": "updated"}

    @app.get("/v1/admin/finops/summary")
    def finops_summary(_identity: AuthenticatedUserContext = Depends(admin)) -> dict[str, object]:  # noqa: B008
        return {"spend": 0, "currency": "EUR", "requests": 0, "label": "source usage records"}

    @app.get("/v1/admin/finops/forecast")
    def finops_forecast(_identity: AuthenticatedUserContext = Depends(admin)) -> dict[str, object]:  # noqa: B008
        return {"projected_spend": 0, "currency": "EUR", "is_estimate": True}

    @app.get("/v1/admin/finops/usage")
    def finops_usage(_identity: AuthenticatedUserContext = Depends(admin)) -> dict[str, object]:  # noqa: B008
        return {"items": [], "next_cursor": None}

    @app.get("/v1/admin/finops/alerts")
    def finops_alerts(_identity: AuthenticatedUserContext = Depends(admin)) -> dict[str, object]:  # noqa: B008
        return {"items": [], "next_cursor": None}

    @app.get("/v1/admin/quotas")
    def finops_quotas(
        _identity: AuthenticatedUserContext = Depends(admin),
    ) -> list[dict[str, object]]:  # noqa: B008
        return []

    @app.post("/v1/admin/quotas", status_code=201)
    def create_finops_quota(
        _payload: dict[str, object], _identity: AuthenticatedUserContext = Depends(admin)
    ) -> dict[str, object]:  # noqa: B008
        return {"status": "created"}

    @app.post("/v1/admin/finops/policy/simulate")
    def simulate_finops_policy(
        payload: dict[str, object], _identity: AuthenticatedUserContext = Depends(admin)
    ) -> dict[str, object]:  # noqa: B008
        estimated = _int_payload(payload.get("estimated_cost", 0))
        threshold = _int_payload(payload.get("budget_amount", 1), 1)
        ratio = estimated / max(threshold, 1)
        decision = (
            "DENY_BUDGET_LIMIT" if ratio >= 1.2 else "ROUTE_CHEAPER" if ratio >= 1 else "ALLOW"
        )
        return {
            "decision": decision,
            "state": BudgetState.HARD_LIMIT if decision.startswith("DENY") else BudgetState.NORMAL,
            "reason_codes": ("SIMULATION_ONLY",),
            "estimated_post_request_spend": estimated,
        }

    @app.post("/v1/admin/model-router/simulate")
    def simulate_model_route(
        payload: dict[str, object],
        identity_context: AuthenticatedUserContext = Depends(admin),  # noqa: B008
    ) -> dict[str, object]:
        signals = ComplexitySignals(
            retrieved_chunks=_int_payload(payload.get("retrieved_chunks", 0)),
            source_count=_int_payload(payload.get("source_count", 0)),
            contradictions=_int_payload(payload.get("contradictions", 0)),
            intent=str(payload.get("intent", "factual")),
            structured_output_required=bool(payload.get("structured_output_required", False)),
            high_risk_flag=bool(payload.get("high_risk_flag", False)),
            tool_required=bool(payload.get("tool_required", False)),
            estimated_input_tokens=_int_payload(payload.get("estimated_input_tokens", 0)),
            expected_output_tokens=_int_payload(payload.get("expected_output_tokens", 500)),
        )
        try:
            decision = LLMModelRouter(model_catalog, model_policies).route(
                identity_context.tenant_id, signals
            )
        except FallbackPolicyError as exc:
            raise HTTPException(status_code=409, detail=f"NO_ELIGIBLE_MODEL: {exc}") from exc
        return {
            "routing_class": decision.routing_class.value,
            "provider_id": decision.provider_id,
            "model_id": decision.model_id,
            "reason_codes": decision.reason_codes,
            "estimated_cost": decision.estimated_cost,
            "fallback_chain": decision.fallback_chain,
        }

    def km_service(
        identity_context: AuthenticatedUserContext,
    ) -> tuple[KnowledgeManagementService, Session]:
        try:
            engine = open_engine()
            session = Session(engine)
        except (ValueError, OSError) as exc:
            raise HTTPException(
                status_code=503, detail="knowledge persistence unavailable"
            ) from exc
        return KnowledgeManagementService(
            session,
            identity_context.tenant_id,
            identity_context.user_id,
            LocalDocumentBlobStore(settings.data_dir / "document-blobs"),
        ), session

    @app.get("/v1/workspaces")
    def workspaces(
        identity_context: AuthenticatedUserContext = Depends(identity),  # noqa: B008
        limit: int = Query(50, ge=1, le=200),
        offset: int = Query(0, ge=0),
    ) -> list[dict[str, object]]:  # noqa: B008
        service, session = km_service(identity_context)
        try:
            return [
                {
                    "id": str(item.id),
                    "tenant_id": item.tenant_id,
                    "name": item.name,
                    "slug": item.slug,
                    "status": item.status,
                }
                for item in service.list_workspaces(limit, offset)
            ]
        finally:
            session.close()

    @app.post("/v1/workspaces", status_code=201)
    def create_workspace(
        payload: dict[str, str],
        identity_context: AuthenticatedUserContext = Depends(admin_or_development),  # noqa: B008
    ) -> dict[str, object]:  # noqa: B008
        service, session = km_service(identity_context)
        try:
            item = service.create_workspace(payload.get("name", ""), payload.get("description", ""))
            session.commit()
            return {
                "id": str(item.id),
                "tenant_id": item.tenant_id,
                "name": item.name,
                "slug": item.slug,
            }
        finally:
            session.close()

    @app.get("/v1/collections")
    def collections(
        identity_context: AuthenticatedUserContext = Depends(identity),  # noqa: B008
        workspace_id: str | None = None,
        limit: int = Query(50, ge=1, le=200),
        offset: int = Query(0, ge=0),
    ) -> list[dict[str, object]]:  # noqa: B008
        service, session = km_service(identity_context)
        try:
            wid = None
            if workspace_id:
                from uuid import UUID

                wid = UUID(workspace_id)
            return [
                {
                    "id": str(item.id),
                    "workspace_id": str(item.workspace_id),
                    "name": item.name,
                    "access_level": item.access_level,
                    "active": item.active,
                }
                for item in service.list_collections(wid, limit, offset)
            ]
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="invalid workspace id") from exc
        finally:
            session.close()

    @app.post("/v1/documents/upload", status_code=202)
    def upload_document(
        workspace_id: str = Form(...),
        collection_id: str = Form(...),
        title: str = Form(...),
        logical_document_key: str = Form(...),
        file: UploadFile = File(...),  # noqa: B008
        identity_context: AuthenticatedUserContext = Depends(identity),  # noqa: B008
    ) -> dict[str, object]:  # noqa: B008
        from uuid import UUID

        service, session = km_service(identity_context)
        try:
            result = service.upload(
                UUID(workspace_id),
                UUID(collection_id),
                title,
                logical_document_key,
                file.filename or "",
                file.content_type or "application/octet-stream",
                file.file.read(),
            )
            job = IngestionWorker(session, max_attempts=settings.job_max_attempts).create_job(
                identity_context.tenant_id, result.document.id, result.version.id
            )
            ingestion_queue.enqueue(
                IngestionMessage(job.id, identity_context.tenant_id, job.correlation_id)
            )
            ingestion_metrics["jobs_total"] += 1
            session.commit()
            return {
                "document_id": str(result.document.id),
                "version_id": str(result.version.id),
                "job_id": str(job.id),
                "status": job.status,
            }
        except (ValueError, KnowledgeAuthorizationError) as exc:
            session.rollback()
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        finally:
            session.close()

    @app.get("/v1/documents/{document_id}/versions")
    def document_versions(
        document_id: str,
        identity_context: AuthenticatedUserContext = Depends(identity),  # noqa: B008
    ) -> list[dict[str, object]]:  # noqa: B008
        from uuid import UUID

        service, session = km_service(identity_context)
        try:
            return [
                {
                    "id": str(item.id),
                    "version": item.version,
                    "filename": item.filename,
                    "content_hash": item.content_hash,
                    "size": item.size,
                    "ingestion_status": item.ingestion_status,
                }
                for item in service.versions(UUID(document_id))
            ]
        except (ValueError, KnowledgeAuthorizationError) as exc:
            raise HTTPException(status_code=404, detail="document not found") from exc
        finally:
            session.close()

    @app.get("/v1/ingestion/jobs")
    def ingestion_jobs(
        identity_context: AuthenticatedUserContext = Depends(identity),  # noqa: B008
    ) -> list[dict[str, object]]:
        from sqlalchemy import select

        from ragops.persistence.models import IngestionJob

        _service, session = km_service(identity_context)
        try:
            jobs = session.scalars(
                select(IngestionJob)
                .where(IngestionJob.tenant_id == identity_context.tenant_id)
                .order_by(IngestionJob.created_at.desc())
                .limit(200)
            )
            return [
                {
                    "job_id": str(job.id),
                    "status": job.status,
                    "stage": job.stage,
                    "progress": job.progress,
                    "document_id": str(job.document_id),
                    "version_id": str(job.document_version_id),
                    "error_code": job.error_code,
                }
                for job in jobs
            ]
        finally:
            session.close()

    @app.get("/v1/ingestion/jobs/{job_id}")
    def ingestion_job(
        job_id: str,
        identity_context: AuthenticatedUserContext = Depends(identity),  # noqa: B008
    ) -> dict[str, object]:
        from uuid import UUID

        from sqlalchemy import select

        from ragops.persistence.models import IngestionJob

        _service, session = km_service(identity_context)
        try:
            job = session.scalar(
                select(IngestionJob).where(
                    IngestionJob.tenant_id == identity_context.tenant_id,
                    IngestionJob.id == UUID(job_id),
                )
            )
            if job is None:
                raise HTTPException(status_code=404, detail="job not found")
            return {
                "job_id": str(job.id),
                "status": job.status,
                "stage": job.stage,
                "progress": job.progress,
                "error_code": job.error_code,
                "error_message": job.error_message_redacted,
            }
        except ValueError as exc:
            raise HTTPException(status_code=404, detail="job not found") from exc
        finally:
            session.close()

    @app.post("/v1/ingestion/jobs/{job_id}/cancel")
    def cancel_ingestion_job(
        job_id: str,
        identity_context: AuthenticatedUserContext = Depends(admin_or_development),  # noqa: B008
    ) -> dict[str, object]:
        from uuid import UUID

        _service, session = km_service(identity_context)
        try:
            job = IngestionWorker(session).cancel(UUID(job_id), identity_context.tenant_id)
            session.commit()
            ingestion_queue.cancel(job.id, identity_context.tenant_id)
            return {"job_id": str(job.id), "status": job.status}
        except ValueError as exc:
            session.rollback()
            raise HTTPException(status_code=404, detail="job not found") from exc
        finally:
            session.close()

    @app.post("/v1/ingestion/jobs/{job_id}/retry", status_code=202)
    def retry_ingestion_job(
        job_id: str,
        identity_context: AuthenticatedUserContext = Depends(admin_or_development),  # noqa: B008
    ) -> dict[str, object]:
        from uuid import UUID

        _service, session = km_service(identity_context)
        try:
            job = IngestionWorker(session).retry(UUID(job_id), identity_context.tenant_id)
            session.commit()
            ingestion_queue.enqueue(
                IngestionMessage(job.id, identity_context.tenant_id, job.correlation_id)
            )
            return {"job_id": str(job.id), "status": job.status}
        except ValueError as exc:
            session.rollback()
            raise HTTPException(status_code=409, detail="job is not retryable") from exc
        finally:
            session.close()

    @app.post("/v1/documents/{document_id}/reindex", status_code=202)
    def reindex_document(
        document_id: str,
        identity_context: AuthenticatedUserContext = Depends(admin_or_development),  # noqa: B008
    ) -> dict[str, str]:
        from uuid import UUID

        service, session = km_service(identity_context)
        try:
            document = service.get_document(UUID(document_id))
            if document.status not in {"indexed", "failed", "inactive"}:
                raise HTTPException(status_code=409, detail="document is not reindexable")
            document.status = "processing"
            session.commit()
            return {"document_id": str(document.id), "status": document.status}
        except KnowledgeAuthorizationError as exc:
            raise HTTPException(status_code=404, detail="document not found") from exc
        finally:
            session.close()

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/ready")
    def ready() -> dict[str, str | int]:
        return {"status": "ready", "documents": len(workflow.repository.chunks())}

    @app.get("/metrics", response_class=PlainTextResponse)
    def metrics() -> str:
        values = workflow.metrics.to_dict()
        lines = [f"ragops_{key} {value}" for key, value in values.items()]
        lines.extend(
            [f"ragops_ingestion_{key} {value}" for key, value in ingestion_metrics.items()]
        )
        lines.append(f"ragops_ingestion_queue_depth {ingestion_queue.status()['queued']}")
        return "\n".join(lines) + "\n"

    @app.post("/v1/query", response_model=QueryApiResponse)
    def query(
        request: QueryApiRequest,
        trusted: AuthenticatedUserContext = Depends(identity),  # noqa: B008
    ) -> QueryApiResponse:
        authenticated = resolve_authenticated_identity(settings, trusted, request)
        state = workflow.run(
            QueryRequest(
                question=request.question,
                user=QueryUser(
                    user_id=authenticated.user_id,
                    tenant_id=authenticated.tenant_id,
                    role=authenticated.role,
                ),
                top_k=request.top_k,
            )
        )
        return QueryApiResponse(
            answer=state.answer,
            abstained=state.abstained,
            evidence_score=state.evidence_score,
            citations=[
                CitationApi(
                    source_id=citation.source_id,
                    title=citation.title,
                    tenant_id=citation.tenant_id,
                    score=citation.score,
                )
                for citation in state.citations
            ],
            metrics=QueryMetricsApi(
                tokens=state.token_count,
                estimated_cost_eur=state.estimated_cost_eur,
                retrieval_latency_ms=state.retrieval_latency_ms,
                llm_latency_ms=state.llm_latency_ms,
                prompt_injection_detected=bool(state.injection_findings),
            ),
            correlation_id=state.request.correlation_id,
        )

    @app.post("/v1/documents/ingest")
    def ingest(
        _identity: AuthenticatedUserContext = Depends(identity),  # noqa: B008
    ) -> dict[str, int | str]:
        workflow.repository._chunks = None  # noqa: SLF001 - explicit local demo refresh
        return {"status": "ingested", "chunks": len(workflow.repository.chunks())}

    @app.get("/v1/documents")
    def documents(
        identity_context: AuthenticatedUserContext = Depends(identity),  # noqa: B008
    ) -> list[dict[str, str]]:
        seen: dict[str, dict[str, str]] = {}
        for chunk in workflow.repository.chunks():
            if chunk.tenant_id != identity_context.tenant_id:
                continue
            seen[chunk.document_id] = {
                "document_id": chunk.document_id,
                "tenant_id": chunk.tenant_id,
                "title": chunk.metadata.title,
                "access_level": chunk.metadata.access_level,
                "version": chunk.metadata.version,
            }
        return list(seen.values())

    @app.get("/v1/documents/{document_id}")
    def document(
        document_id: str,
        identity_context: AuthenticatedUserContext = Depends(identity),  # noqa: B008
    ) -> dict[str, str]:
        for chunk in workflow.repository.chunks():
            if chunk.document_id == document_id and chunk.tenant_id == identity_context.tenant_id:
                return {
                    "document_id": chunk.document_id,
                    "tenant_id": chunk.tenant_id,
                    "title": chunk.metadata.title,
                    "source_path": chunk.metadata.source_path,
                }
        raise HTTPException(status_code=404, detail="document not found")

    @app.delete("/v1/documents/{document_id}")
    def delete_document(
        document_id: str,
        _identity: AuthenticatedUserContext = Depends(admin_or_development),  # noqa: B008
    ) -> dict[str, str]:
        raise HTTPException(
            status_code=501, detail=f"delete disabled in portfolio demo: {document_id}"
        )

    @app.post("/v1/evaluations/run")
    def run_evaluation_endpoint(
        _identity: AuthenticatedUserContext = Depends(admin_or_development),  # noqa: B008
    ) -> dict[str, str]:
        from ragops.evaluation.runner import run_evaluation

        report = run_evaluation(Path("data/evaluation/gold_questions.json"), settings.evidence_dir)
        return {
            "status": str(report["status"]),
            "report": str(settings.evidence_dir / "rag-evaluation.json"),
        }

    @app.get("/v1/evaluations")
    def evaluations(
        _identity: AuthenticatedUserContext = Depends(admin_or_development),  # noqa: B008
    ) -> dict[str, object]:
        path = settings.evidence_dir / "rag-evaluation.json"
        if not path.exists():
            return {"status": "not_run"}
        report: dict[str, object] = json.loads(path.read_text(encoding="utf-8"))
        return {"path": str(path), **report, "status": "available"}

    @app.get("/v1/audit-events")
    def audit_events(
        identity_context: AuthenticatedUserContext = Depends(admin_or_development),  # noqa: B008
    ) -> list[dict[str, Any]]:
        path = settings.evidence_dir / "audit-events.jsonl"
        if not path.exists():
            return []
        events: list[dict[str, Any]] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            event = json.loads(line)
            # Filter before the 100-event window so other tenants cannot crowd it out.
            if isinstance(event, dict) and event.get("tenant_id") == identity_context.tenant_id:
                events.append(event)
        return events[-100:]

    @app.get("/v1/costs/summary")
    def costs(
        _identity: AuthenticatedUserContext = Depends(admin_or_development),  # noqa: B008
    ) -> dict[str, float | int]:
        values = workflow.metrics.to_dict()
        return {
            "request_count": int(values["request_count"]),
            "total_tokens": int(values["total_tokens"]),
            "total_cost_eur": float(values["total_cost_eur"]),
        }

    return app
