"""FastAPI application factory."""
# FastAPI dependency defaults are intentional route declarations.
# ruff: noqa: B008

from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from threading import Lock
from typing import Any, cast

from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, Response, UploadFile
from fastapi.responses import JSONResponse, PlainTextResponse
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import TimeoutError as RedisTimeoutError
from sqlalchemy.engine import Engine
from sqlalchemy.exc import InterfaceError, OperationalError
from sqlalchemy.exc import TimeoutError as PoolTimeoutError
from sqlalchemy.orm import Session

from ragops import __version__
from ragops.api.hardening import RequestHardening
from ragops.api.schemas import (
    CitationApi,
    QueryApiRequest,
    QueryApiResponse,
    QueryMetricsApi,
)
from ragops.auth.dependencies import admin_dependency, identity_dependency
from ragops.auth.identity import AuthenticatedUserContext
from ragops.config.settings import Settings
from ragops.finops.query_accounting import (
    AccountingError,
    BudgetLimitExceeded,
    QueryAccounting,
)
from ragops.finops.service import budget_decision, valid_amount
from ragops.governance.audit import AuditLogger
from ragops.knowledge.service import (
    KnowledgeAuthorizationError,
    KnowledgeManagementService,
    LocalDocumentBlobStore,
)
from ragops.llm.providers import LLMProvider, provider_from_env
from ragops.modeling.router import (
    ComplexitySignals,
    FallbackPolicyError,
    LLMModelRouter,
    ModelSpec,
    ProviderAdapter,
    ProviderRegistry,
    RoutingClass,
    TenantModelPolicy,
)
from ragops.monitoring.metrics import MetricsRegistry
from ragops.ops.rate_limit import (
    DeterministicRateLimiter,
    RateLimiter,
    RateLimiterUnavailable,
    RedisRateLimiter,
)
from ragops.ops.readiness import (
    DependencyCheck,
    ReadinessService,
    postgres_probe,
    qdrant_probe,
    redis_probe,
)
from ragops.persistence.database import open_engine
from ragops.persistence.models import TenantBudget
from ragops.retrieval.context import ContextLimits
from ragops.retrieval.vector_retriever import InvalidTenantScope, RetrievalError, VectorRetriever
from ragops.storage.json_store import JsonRepository
from ragops.storage.models import QueryUser
from ragops.vector.embedding import embedding_provider_for
from ragops.vector.index import QdrantVectorIndex, VectorIndex
from ragops.workers.queue import (
    IngestionMessage,
    IngestionQueue,
    InMemoryIngestionQueue,
    RedisIngestionQueue,
)
from ragops.workers.worker import IngestionWorker
from ragops.workflows.state_machine import QueryRequest, RAGWorkflow
from ragops.workflows.vector_query import (
    GenerationUnavailable,
    NoEligibleModel,
    VectorQueryService,
)


def rate_limiter_for(settings: Settings) -> RateLimiter:
    if settings.rate_limit_backend == "redis":
        return RedisRateLimiter(
            settings.effective_rate_limit_redis_url or "",
            limit=settings.rate_limit_requests,
            window_seconds=settings.rate_limit_window_seconds,
            namespace=settings.rate_limit_namespace,
            timeout_seconds=settings.rate_limit_timeout_seconds,
        )
    return DeterministicRateLimiter(
        settings.rate_limit_requests, settings.rate_limit_window_seconds
    )


class AppResources:
    """Process-wide dependency clients shared by requests and readiness probes.

    One lazily created engine (bounded timeouts, pre-ping) replaces an engine per request,
    so the same instance reconnects after an outage and pools are closed on shutdown.
    """

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._lock = Lock()
        self._engine: Engine | None = None
        self._qdrant: Any = None

    @staticmethod
    def database_configured() -> bool:
        return bool(os.getenv("RAGOPS_DATABASE_URL") or os.getenv("RAGOPS_DATABASE_URL_FILE"))

    def engine(self) -> Engine:
        with self._lock:
            if self._engine is None:
                self._engine = open_engine(timeout_seconds=self.settings.dependency_timeout_seconds)
            return self._engine

    def qdrant(self) -> Any:
        with self._lock:
            if self._qdrant is None:
                from qdrant_client import QdrantClient

                self._qdrant = QdrantClient(
                    url=self.settings.qdrant_url,
                    api_key=self.settings.qdrant_api_key,
                    timeout=max(1, round(self.settings.dependency_timeout_seconds)),
                )
            return self._qdrant

    def close(self) -> None:
        with self._lock:
            if self._engine is not None:
                self._engine.dispose()
                self._engine = None
            if self._qdrant is not None:
                self._qdrant.close()
                self._qdrant = None


def _workflow(
    settings: Settings, audit: AuditLogger, metrics: MetricsRegistry, provider: LLMProvider | None
) -> RAGWorkflow:
    """The demo workflow over the local corpus; only built in the demo query mode."""
    repository = JsonRepository(settings.data_dir)
    return RAGWorkflow(repository, provider=provider, audit_logger=audit, metrics=metrics)


def _vector_index(settings: Settings) -> QdrantVectorIndex:
    assert settings.qdrant_url  # validate_vector_configuration guarantees a URL
    return QdrantVectorIndex(
        settings.qdrant_url,
        settings.qdrant_collection,
        settings.embedding_dimension,
        api_key=settings.qdrant_api_key,
        timeout_seconds=max(1, round(settings.dependency_timeout_seconds)),
    )


_RETRIEVAL_DETAILS = {
    "tenant_violation": "retrieval integrity violation",
    "embedding_contract": "retrieval misconfigured",
    "incomplete_chunk": "retrieval integrity violation",
}


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


def _money_payload(value: object) -> Decimal:
    """Parse a monetary payload value exactly; floats go through their decimal repr."""
    if isinstance(value, bool) or not isinstance(value, int | float | str):
        raise HTTPException(status_code=422, detail="monetary values must be numbers")
    try:
        amount = Decimal(str(value))
    except InvalidOperation:
        raise HTTPException(status_code=422, detail="monetary values must be numbers") from None
    if not valid_amount(amount):
        raise HTTPException(status_code=422, detail="monetary values must be finite and >= 0")
    return amount


ENFORCEMENT_MODES = frozenset({"monitor", "optimize", "hard_limit"})


def _budget_amount(value: object) -> float:
    amount = _money_payload(value)
    if amount <= 0:
        raise HTTPException(status_code=422, detail="budget_amount must be positive")
    return float(amount)


def _enforcement_mode(value: object) -> str:
    if value not in ENFORCEMENT_MODES:
        raise HTTPException(status_code=422, detail="unsupported enforcement_mode")
    return str(value)


def _int_payload(value: object, default: int = 0) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int | float | str):
        try:
            return int(value)
        except (ValueError, OverflowError):
            return default
    return default


def create_app(
    settings: Settings | None = None,
    *,
    vector_index: VectorIndex | None = None,
    llm_provider: LLMProvider | None = None,
) -> FastAPI:
    settings = settings or Settings.from_env()
    settings.validate_identity_configuration()
    settings.validate_async_configuration()
    settings.validate_vector_configuration()
    settings.validate_model_configuration()
    settings.validate_rate_limit_configuration()
    settings.validate_query_configuration()
    settings.require_supported_runtime()
    resources = AppResources(settings)
    # Exactly one query path per process, chosen by configuration; there is no fallback.
    vector_mode = settings.effective_query_mode == "vector"
    owned_index: QdrantVectorIndex | None = None
    if vector_mode and vector_index is None:
        vector_index = owned_index = _vector_index(settings)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        # Startup never probes: an instance may start while dependencies are down and stays
        # unready until they recover. Shutdown turns unready first, then closes clients.
        yield
        readiness.begin_shutdown()
        resources.close()
        if owned_index is not None:
            owned_index.client.close()
        for client in redis_clients:
            client.close()
        readiness.close()

    # Interactive API docs are a development aid; production serves no schema or docs.
    docs_enabled = settings.environment != "production"
    app = FastAPI(
        title="RAGOps Enterprise Copilot",
        version=__version__,
        lifespan=lifespan,
        docs_url="/docs" if docs_enabled else None,
        redoc_url="/redoc" if docs_enabled else None,
        openapi_url="/openapi.json" if docs_enabled else None,
    )
    app.add_middleware(RequestHardening, max_body_bytes=settings.max_request_bytes)
    app.state.resources = resources
    metrics_registry = MetricsRegistry()
    audit_logger = AuditLogger(settings.evidence_dir / "audit-events.jsonl")
    workflow: RAGWorkflow | None = None
    vector_query: VectorQueryService | None = None
    if vector_mode:
        assert vector_index is not None
        # The configured provider is the only adapter: routing to any other catalog entry
        # fails as unavailable instead of silently answering from another provider.
        providers = ProviderRegistry()
        providers.register(cast(ProviderAdapter, llm_provider or provider_from_env()))
        try:
            max_output_tokens = int(os.getenv("RAGOPS_LLM_MAX_OUTPUT_TOKENS", "600"))
        except ValueError:
            raise RuntimeError("RAGOPS_LLM_MAX_OUTPUT_TOKENS must be an integer") from None
        vector_query = VectorQueryService(
            VectorRetriever(vector_index, embedding_provider_for(settings)),
            # Built per query so admin catalog and policy changes apply immediately.
            lambda: LLMModelRouter(model_catalog, model_policies, providers),
            ContextLimits(settings.context_max_chunks, settings.context_max_tokens),
            audit_logger,
            metrics_registry,
            accounting=QueryAccounting(resources.engine)
            if resources.database_configured()
            else None,
            max_output_tokens=max_output_tokens,
        )
    else:
        workflow = _workflow(settings, audit_logger, metrics_registry, llm_provider)

    def demo_workflow() -> RAGWorkflow:
        if workflow is None:
            raise HTTPException(
                status_code=404, detail="demo corpus is not available in vector query mode"
            )
        return workflow

    ingestion_queue: IngestionQueue = (
        RedisIngestionQueue(
            settings.redis_url or "",
            settings.queue_name,
            timeout_seconds=max(2.0, settings.dependency_timeout_seconds * 2),
        )
        if settings.async_ingestion_required
        else InMemoryIngestionQueue()
    )
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
    app.state.model_policies = model_policies
    app.state.vector_index = vector_index
    budget_records: dict[str, dict[str, object]] = {}
    identity = identity_dependency(settings)
    admin = admin_dependency(settings)

    def platform_admin(
        trusted: AuthenticatedUserContext = Depends(admin),  # noqa: B008
    ) -> AuthenticatedUserContext:
        """The model catalog is shared by all tenants; only platform operators change it."""
        if not settings.platform_admin_tenant_id or (
            trusted.tenant_id != settings.platform_admin_tenant_id
        ):
            raise HTTPException(status_code=403, detail="platform administration required")
        return trusted

    def admin_or_development(
        trusted: AuthenticatedUserContext = Depends(identity),  # noqa: B008
    ) -> AuthenticatedUserContext:
        if settings.identity_provider == "development":
            return trusted
        return admin(trusted)

    rate_limiter = rate_limiter_for(settings)
    redis_clients = [
        client
        for client in (
            getattr(ingestion_queue, "client", None),
            getattr(rate_limiter, "client", None),
        )
        if client is not None
    ]
    database = resources.database_configured()
    readiness = ReadinessService(
        [
            DependencyCheck(
                "postgres", database, postgres_probe(resources.engine) if database else None
            ),
            DependencyCheck(
                "redis",
                settings.async_ingestion_required or settings.rate_limit_backend == "redis",
                redis_probe(lambda: redis_clients[0]) if redis_clients else None,
            ),
            DependencyCheck(
                "qdrant",
                settings.vector_provider == "qdrant",
                qdrant_probe(resources.qdrant, settings.qdrant_collection)
                if settings.vector_provider == "qdrant"
                else None,
            ),
        ],
        timeout_seconds=settings.dependency_timeout_seconds,
    )
    app.state.readiness = readiness

    def dependency_unavailable(_request: Any, _exc: Exception) -> JSONResponse:
        # Stable, detail-free service error; integrity errors are deliberately not mapped.
        return JSONResponse(
            {"detail": "dependency unavailable"}, status_code=503, headers={"Retry-After": "1"}
        )

    for error in (
        OperationalError,
        InterfaceError,
        PoolTimeoutError,
        RedisConnectionError,
        RedisTimeoutError,
    ):
        app.add_exception_handler(error, dependency_unavailable)

    def limited(
        endpoint_class: str,
        authenticate: Callable[..., AuthenticatedUserContext] = identity,
    ) -> Callable[..., AuthenticatedUserContext]:
        """Authenticate first, then count against the verified tenant/user bucket."""

        def dependency(
            response: Response,
            context: AuthenticatedUserContext = Depends(authenticate),  # noqa: B008
        ) -> AuthenticatedUserContext:
            try:
                decision = rate_limiter.check(context.tenant_id, context.user_id, endpoint_class)
            except RateLimiterUnavailable:
                # Fail closed: an unreachable limiter must not silently disable limits.
                raise HTTPException(
                    status_code=503,
                    detail="rate limiter unavailable",
                    headers={"Retry-After": "1"},
                ) from None
            headers = {
                "RateLimit-Limit": str(decision.limit),
                "RateLimit-Remaining": str(decision.remaining),
                "RateLimit-Reset": str(decision.reset_after),
            }
            if not decision.allowed:
                raise HTTPException(
                    status_code=429,
                    detail="rate limit exceeded",
                    headers={**headers, "Retry-After": str(decision.retry_after)},
                )
            response.headers.update(headers)
            return context

        return dependency

    @app.get("/v1/admin/providers")
    def admin_providers(
        _identity: AuthenticatedUserContext = Depends(limited("administration", admin)),  # noqa: B008
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
        _identity: AuthenticatedUserContext = Depends(limited("administration", platform_admin)),  # noqa: B008
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
        _identity: AuthenticatedUserContext = Depends(limited("administration", platform_admin)),  # noqa: B008
    ) -> dict[str, object]:
        if not any(model.provider_id == provider_id for model in model_catalog):
            raise HTTPException(status_code=404, detail="provider not found")
        return {"provider_id": provider_id, "enabled": bool(payload.get("enabled", True))}

    @app.get("/v1/admin/models")
    def admin_models(
        _identity: AuthenticatedUserContext = Depends(limited("administration", admin)),  # noqa: B008
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
        _identity: AuthenticatedUserContext = Depends(limited("administration", platform_admin)),  # noqa: B008
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
        _identity: AuthenticatedUserContext = Depends(limited("administration", platform_admin)),  # noqa: B008
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
        identity_context: AuthenticatedUserContext = Depends(limited("administration", admin)),  # noqa: B008
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
        identity_context: AuthenticatedUserContext = Depends(limited("administration", admin)),  # noqa: B008
    ) -> dict[str, object]:
        if tenant_id != identity_context.tenant_id:
            raise HTTPException(status_code=404, detail="policy not found")
        model_policies[tenant_id] = _policy_from_payload(tenant_id, payload)
        return {"tenant_id": tenant_id, "updated": True}

    @app.get("/v1/admin/budgets")
    def list_finops_budgets(
        identity_context: AuthenticatedUserContext = Depends(limited("administration", admin)),
    ) -> list[dict[str, object]]:  # noqa: B008
        return [
            value
            for value in budget_records.values()
            if value.get("tenant_id") == identity_context.tenant_id
        ]

    @app.post("/v1/admin/budgets", status_code=201)
    def create_finops_budget(
        payload: dict[str, object],
        identity_context: AuthenticatedUserContext = Depends(limited("administration", admin)),
    ) -> dict[str, object]:  # noqa: B008
        amount = _budget_amount(payload.get("budget_amount", 0))
        if str(payload.get("currency", "EUR")) != "EUR":
            raise HTTPException(status_code=422, detail="invalid budget")
        budget_id = str(len(budget_records) + 1)
        record = {
            "id": budget_id,
            "tenant_id": identity_context.tenant_id,
            "budget_amount": amount,
            "currency": "EUR",
            "enforcement_mode": _enforcement_mode(payload.get("enforcement_mode", "optimize")),
        }
        budget_records[budget_id] = record
        return record

    @app.patch("/v1/admin/budgets/{budget_id}")
    def update_finops_budget(
        budget_id: str,
        payload: dict[str, object],
        identity_context: AuthenticatedUserContext = Depends(limited("administration", admin)),
    ) -> dict[str, object]:  # noqa: B008
        record = budget_records.get(budget_id)
        if record is None or record.get("tenant_id") != identity_context.tenant_id:
            raise HTTPException(status_code=404, detail="budget not found")
        changes: dict[str, object] = {}
        if "budget_amount" in payload:
            changes["budget_amount"] = _budget_amount(payload["budget_amount"])
        if "enforcement_mode" in payload:
            changes["enforcement_mode"] = _enforcement_mode(payload["enforcement_mode"])
        record.update(changes)
        return record

    @app.patch("/v1/admin/quotas/{quota_id}")
    def update_finops_quota(
        quota_id: str,
        _payload: dict[str, object],
        _identity: AuthenticatedUserContext = Depends(limited("administration", admin)),
    ) -> dict[str, object]:  # noqa: B008
        if not quota_id:
            raise HTTPException(status_code=404, detail="quota not found")
        return {"id": quota_id, "status": "updated"}

    @app.get("/v1/admin/finops/summary")
    def finops_summary(
        _identity: AuthenticatedUserContext = Depends(limited("administration", admin)),
    ) -> dict[str, object]:  # noqa: B008
        return {"spend": 0, "currency": "EUR", "requests": 0, "label": "source usage records"}

    @app.get("/v1/admin/finops/forecast")
    def finops_forecast(
        _identity: AuthenticatedUserContext = Depends(limited("administration", admin)),
    ) -> dict[str, object]:  # noqa: B008
        return {"projected_spend": 0, "currency": "EUR", "is_estimate": True}

    @app.get("/v1/admin/finops/usage")
    def finops_usage(
        _identity: AuthenticatedUserContext = Depends(limited("administration", admin)),
    ) -> dict[str, object]:  # noqa: B008
        return {"items": [], "next_cursor": None}

    @app.get("/v1/admin/finops/alerts")
    def finops_alerts(
        _identity: AuthenticatedUserContext = Depends(limited("administration", admin)),
    ) -> dict[str, object]:  # noqa: B008
        return {"items": [], "next_cursor": None}

    @app.get("/v1/admin/quotas")
    def finops_quotas(
        _identity: AuthenticatedUserContext = Depends(limited("administration", admin)),
    ) -> list[dict[str, object]]:  # noqa: B008
        return []

    @app.post("/v1/admin/quotas", status_code=201)
    def create_finops_quota(
        _payload: dict[str, object],
        _identity: AuthenticatedUserContext = Depends(limited("administration", admin)),
    ) -> dict[str, object]:  # noqa: B008
        return {"status": "created"}

    @app.post("/v1/admin/finops/policy/simulate")
    def simulate_finops_policy(
        payload: dict[str, object],
        _identity: AuthenticatedUserContext = Depends(limited("administration", admin)),
    ) -> dict[str, object]:  # noqa: B008
        estimated = _money_payload(payload.get("estimated_cost", 0))
        amount = _money_payload(payload.get("budget_amount", 1))
        if amount <= 0:
            raise HTTPException(status_code=422, detail="budget_amount must be positive")
        # Same decision function as preflight, on the default hard-limit policy thresholds.
        policy = TenantBudget(
            period_start=date.today(),
            period_end=date.today(),
            budget_amount=amount,
            currency="EUR",
            warning_threshold=Decimal("0.80"),
            soft_limit_threshold=Decimal("1.00"),
            hard_limit_threshold=Decimal("1.20"),
            enforcement_mode="hard_limit",
        )
        decision = budget_decision(policy, Decimal("0"), estimated)
        return {
            "decision": decision.decision,
            "state": decision.state,
            "reason_codes": ("SIMULATION_ONLY", *decision.reason_codes),
            "estimated_post_request_spend": str(decision.projected_spend),
        }

    @app.post("/v1/admin/model-router/simulate")
    def simulate_model_route(
        payload: dict[str, object],
        identity_context: AuthenticatedUserContext = Depends(limited("router_simulation", admin)),  # noqa: B008
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
            session = Session(resources.engine())
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
        identity_context: AuthenticatedUserContext = Depends(limited("knowledge")),  # noqa: B008
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
        identity_context: AuthenticatedUserContext = Depends(
            limited("knowledge", admin_or_development)
        ),  # noqa: B008
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
        identity_context: AuthenticatedUserContext = Depends(limited("knowledge")),  # noqa: B008
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
        identity_context: AuthenticatedUserContext = Depends(limited("upload")),  # noqa: B008
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
            accepted: dict[str, object] = {
                "document_id": str(result.document.id),
                "version_id": str(result.version.id),
                "job_id": str(job.id),
                "status": job.status,
            }
            message = IngestionMessage(job.id, identity_context.tenant_id, job.correlation_id)
            # Commit first: a worker may receive the message immediately and must find the job.
            session.commit()
            try:
                ingestion_queue.enqueue(message)
            except (RedisConnectionError, RedisTimeoutError):
                # Never leave a queued job without a message: fail it visibly so the
                # retry endpoint can enqueue it again once the queue recovers.
                job.status = job.state = "failed"
                job.error_code = job.failure_code = "queue_unavailable"
                job.error_message_redacted = "ingestion queue unavailable; retry the job"
                session.commit()
                raise
            ingestion_metrics["jobs_total"] += 1
            return accepted
        except (ValueError, KnowledgeAuthorizationError) as exc:
            session.rollback()
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        finally:
            session.close()

    @app.get("/v1/documents/{document_id}/versions")
    def document_versions(
        document_id: str,
        identity_context: AuthenticatedUserContext = Depends(limited("knowledge")),  # noqa: B008
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
        identity_context: AuthenticatedUserContext = Depends(limited("ingestion")),  # noqa: B008
    ) -> list[dict[str, object]]:
        from sqlalchemy import select

        from ragops.persistence.models import Document, DocumentVersion, IngestionJob

        _service, session = km_service(identity_context)
        try:
            tenant = identity_context.tenant_id
            rows = session.execute(
                select(IngestionJob, Document.title, DocumentVersion.filename)
                .outerjoin(
                    Document,
                    (Document.tenant_id == IngestionJob.tenant_id)
                    & (Document.id == IngestionJob.document_id),
                )
                .outerjoin(
                    DocumentVersion,
                    (DocumentVersion.tenant_id == IngestionJob.tenant_id)
                    & (DocumentVersion.id == IngestionJob.document_version_id),
                )
                .where(IngestionJob.tenant_id == tenant)
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
                    "title": title,
                    "filename": filename,
                }
                for job, title, filename in rows
            ]
        finally:
            session.close()

    @app.get("/v1/ingestion/jobs/{job_id}")
    def ingestion_job(
        job_id: str,
        identity_context: AuthenticatedUserContext = Depends(limited("ingestion")),  # noqa: B008
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
        identity_context: AuthenticatedUserContext = Depends(
            limited("ingestion", admin_or_development)
        ),  # noqa: B008
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
        identity_context: AuthenticatedUserContext = Depends(
            limited("ingestion", admin_or_development)
        ),  # noqa: B008
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
        identity_context: AuthenticatedUserContext = Depends(
            limited("ingestion", admin_or_development)
        ),  # noqa: B008
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
        except (KnowledgeAuthorizationError, ValueError) as exc:
            raise HTTPException(status_code=404, detail="document not found") from exc
        finally:
            session.close()

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/ready")
    def ready() -> JSONResponse:
        status_code, payload = readiness.evaluate()
        # Aggregate demo-corpus size kept for the dashboard; no tenant data is exposed.
        if workflow is not None:
            payload["documents"] = len(workflow.repository.chunks())
        return JSONResponse(payload, status_code=status_code)

    @app.get("/metrics", response_class=PlainTextResponse)
    def metrics() -> str:
        values = metrics_registry.to_dict()
        lines = [f"ragops_{key} {value}" for key, value in values.items()]
        lines.extend(
            [f"ragops_ingestion_{key} {value}" for key, value in ingestion_metrics.items()]
        )
        try:
            depth, queue_up = ingestion_queue.status()["queued"], 1
        except (RedisConnectionError, RedisTimeoutError):
            depth, queue_up = -1, 0  # metrics stay observable and never claim a failed probe
        lines.append(f"ragops_ingestion_queue_depth {depth}")
        lines.append(f"ragops_ingestion_queue_up {queue_up}")
        return "\n".join(lines) + "\n"

    @app.post("/v1/query", response_model=QueryApiResponse)
    def query(
        request: QueryApiRequest,
        trusted: AuthenticatedUserContext = Depends(limited("rag")),  # noqa: B008
    ) -> QueryApiResponse:
        if vector_query is not None:
            return vector_answer(trusted, request)
        authenticated = resolve_authenticated_identity(settings, trusted, request)
        state = demo_workflow().run(
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

    def vector_answer(
        trusted: AuthenticatedUserContext, request: QueryApiRequest
    ) -> QueryApiResponse:
        """The tenant is the verified identity's; request fields never select it."""
        assert vector_query is not None
        try:
            result = vector_query.run(trusted, request.question, request.top_k)
        except InvalidTenantScope:
            raise HTTPException(status_code=403, detail="tenant scope rejected") from None
        except RetrievalError as exc:
            from ragops.workflows.vector_query import retrieval_outcome

            detail = _RETRIEVAL_DETAILS.get(retrieval_outcome(exc), "retrieval unavailable")
            raise HTTPException(
                status_code=503, detail=detail, headers={"Retry-After": "1"}
            ) from None
        except NoEligibleModel:
            raise HTTPException(status_code=409, detail="no eligible model") from None
        except BudgetLimitExceeded as exc:
            raise HTTPException(status_code=429, detail=exc.detail) from None
        except AccountingError as exc:
            raise HTTPException(
                status_code=503, detail=exc.detail, headers={"Retry-After": "1"}
            ) from None
        except GenerationUnavailable:
            raise HTTPException(
                status_code=503, detail="generation unavailable", headers={"Retry-After": "1"}
            ) from None
        if any(item.tenant_id != trusted.tenant_id for item in result.citations):
            raise HTTPException(status_code=503, detail="retrieval integrity violation")
        return QueryApiResponse(
            answer=result.answer,
            abstained=result.abstained,
            evidence_score=result.evidence_score,
            citations=[
                CitationApi(
                    source_id=item.source_id,
                    title=item.title,
                    tenant_id=item.tenant_id,
                    score=item.score,
                    document_id=item.document_id,
                    document_version_id=item.document_version_id,
                    chunk_id=item.chunk_id,
                    page_number=item.page_number,
                )
                for item in result.citations
            ],
            metrics=QueryMetricsApi(
                tokens=result.tokens,
                estimated_cost_eur=result.estimated_cost_eur,
                retrieval_latency_ms=result.retrieval_latency_ms,
                llm_latency_ms=result.llm_latency_ms,
                prompt_injection_detected=result.prompt_injection_detected,
            ),
            correlation_id=result.correlation_id,
        )

    @app.post("/v1/documents/ingest")
    def ingest(
        _identity: AuthenticatedUserContext = Depends(limited("ingestion")),  # noqa: B008
    ) -> dict[str, int | str]:
        demo = demo_workflow()
        demo.repository._chunks = None  # noqa: SLF001 - explicit local demo refresh
        return {"status": "ingested", "chunks": len(demo.repository.chunks())}

    @app.get("/v1/documents")
    def documents(
        identity_context: AuthenticatedUserContext = Depends(limited("rag")),  # noqa: B008
    ) -> list[dict[str, str]]:
        seen: dict[str, dict[str, str]] = {}
        for chunk in demo_workflow().repository.chunks():
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
        identity_context: AuthenticatedUserContext = Depends(limited("rag")),  # noqa: B008
    ) -> dict[str, str]:
        for chunk in demo_workflow().repository.chunks():
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
        _identity: AuthenticatedUserContext = Depends(limited("knowledge", admin_or_development)),  # noqa: B008
    ) -> dict[str, str]:
        raise HTTPException(
            status_code=501, detail=f"delete disabled in portfolio demo: {document_id}"
        )

    @app.post("/v1/evaluations/run")
    def run_evaluation_endpoint(
        _identity: AuthenticatedUserContext = Depends(limited("evaluation", admin_or_development)),  # noqa: B008
    ) -> dict[str, str]:
        from ragops.evaluation.runner import run_evaluation

        report = run_evaluation(Path("data/evaluation/gold_questions.json"), settings.evidence_dir)
        return {
            "status": str(report["status"]),
            "report": str(settings.evidence_dir / "rag-evaluation.json"),
        }

    @app.get("/v1/evaluations")
    def evaluations(
        _identity: AuthenticatedUserContext = Depends(limited("evaluation", admin_or_development)),  # noqa: B008
    ) -> dict[str, object]:
        path = settings.evidence_dir / "rag-evaluation.json"
        if not path.exists():
            return {"status": "not_run"}
        report: dict[str, object] = json.loads(path.read_text(encoding="utf-8"))
        return {"path": str(path), **report, "status": "available"}

    @app.get("/v1/audit-events")
    def audit_events(
        identity_context: AuthenticatedUserContext = Depends(
            limited("administration", admin_or_development)
        ),  # noqa: B008
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
        _identity: AuthenticatedUserContext = Depends(
            limited("administration", admin_or_development)
        ),  # noqa: B008
    ) -> dict[str, float | int]:
        values = metrics_registry.to_dict()
        return {
            "request_count": int(values["request_count"]),
            "total_tokens": int(values["total_tokens"]),
            "total_cost_eur": float(values["total_cost_eur"]),
        }

    return app
