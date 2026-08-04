"""FastAPI application factory."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import PlainTextResponse

from ragops.api.schemas import (
    CitationApi,
    QueryApiRequest,
    QueryApiResponse,
    QueryMetricsApi,
)
from ragops.config.settings import Settings
from ragops.governance.audit import AuditLogger
from ragops.monitoring.metrics import MetricsRegistry
from ragops.storage.json_store import JsonRepository
from ragops.storage.models import QueryUser
from ragops.workflows.state_machine import QueryRequest, RAGWorkflow


def _workflow(settings: Settings) -> RAGWorkflow:
    repository = JsonRepository(settings.data_dir)
    metrics = MetricsRegistry()
    audit = AuditLogger(settings.evidence_dir / "audit-events.jsonl")
    return RAGWorkflow(repository, audit_logger=audit, metrics=metrics)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    app = FastAPI(title="RAGOps Enterprise Copilot", version="0.1.0")
    workflow = _workflow(settings)

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/ready")
    def ready() -> dict[str, str | int]:
        return {"status": "ready", "documents": len(workflow.repository.chunks())}

    @app.get("/metrics", response_class=PlainTextResponse)
    def metrics() -> str:
        values = workflow.metrics.to_dict()
        return "\n".join(f"ragops_{key} {value}" for key, value in values.items()) + "\n"

    @app.post("/v1/query", response_model=QueryApiResponse)
    def query(request: QueryApiRequest) -> QueryApiResponse:
        state = workflow.run(
            QueryRequest(
                question=request.question,
                user=QueryUser(
                    user_id=request.user_id,
                    tenant_id=request.tenant_id,
                    role=request.role,
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
    def ingest() -> dict[str, int | str]:
        workflow.repository._chunks = None  # noqa: SLF001 - explicit local demo refresh
        return {"status": "ingested", "chunks": len(workflow.repository.chunks())}

    @app.get("/v1/documents")
    def documents() -> list[dict[str, str]]:
        seen: dict[str, dict[str, str]] = {}
        for chunk in workflow.repository.chunks():
            seen[chunk.document_id] = {
                "document_id": chunk.document_id,
                "tenant_id": chunk.tenant_id,
                "title": chunk.metadata.title,
                "access_level": chunk.metadata.access_level,
                "version": chunk.metadata.version,
            }
        return list(seen.values())

    @app.get("/v1/documents/{document_id}")
    def document(document_id: str) -> dict[str, str]:
        for chunk in workflow.repository.chunks():
            if chunk.document_id == document_id:
                return {
                    "document_id": chunk.document_id,
                    "tenant_id": chunk.tenant_id,
                    "title": chunk.metadata.title,
                    "source_path": chunk.metadata.source_path,
                }
        raise HTTPException(status_code=404, detail="document not found")

    @app.delete("/v1/documents/{document_id}")
    def delete_document(document_id: str) -> dict[str, str]:
        raise HTTPException(
            status_code=501, detail=f"delete disabled in portfolio demo: {document_id}"
        )

    @app.post("/v1/evaluations/run")
    def run_evaluation_endpoint() -> dict[str, str]:
        from ragops.evaluation.runner import run_evaluation

        report = run_evaluation(Path("data/evaluation/gold_questions.json"), settings.evidence_dir)
        return {
            "status": str(report["status"]),
            "report": str(settings.evidence_dir / "rag-evaluation.json"),
        }

    @app.get("/v1/evaluations")
    def evaluations() -> dict[str, object]:
        path = settings.evidence_dir / "rag-evaluation.json"
        if not path.exists():
            return {"status": "not_run"}
        report: dict[str, object] = json.loads(path.read_text(encoding="utf-8"))
        return {"path": str(path), **report, "status": "available"}

    @app.get("/v1/audit-events")
    def audit_events() -> list[dict[str, Any]]:
        path = settings.evidence_dir / "audit-events.jsonl"
        if not path.exists():
            return []
        events: list[dict[str, Any]] = []
        for line in path.read_text(encoding="utf-8").splitlines()[-100:]:
            event = json.loads(line)
            if isinstance(event, dict):
                events.append(event)
        return events

    @app.get("/v1/costs/summary")
    def costs() -> dict[str, float | int]:
        values = workflow.metrics.to_dict()
        return {
            "request_count": int(values["request_count"]),
            "total_tokens": int(values["total_tokens"]),
            "total_cost_eur": float(values["total_cost_eur"]),
        }

    return app
