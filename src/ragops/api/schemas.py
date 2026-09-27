"""Typed Pydantic schemas exposed through the FastAPI OpenAPI contract."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from ragops.storage.models import Role


class QueryApiRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question: str = Field(min_length=1, max_length=4000)
    # Legacy fields are accepted only by explicit development/demo identity.
    # Production routes ignore them and use verified claims instead.
    tenant_id: str | None = Field(
        default=None, min_length=1, max_length=64, pattern=r"^tenant-[a-z0-9-]+$"
    )
    user_id: str | None = Field(default=None, min_length=1, max_length=128)
    role: Role | None = None
    top_k: int = Field(default=5, ge=1, le=20)


class CitationApi(BaseModel):
    source_id: str
    title: str
    tenant_id: str
    score: float


class QueryMetricsApi(BaseModel):
    tokens: int
    estimated_cost_eur: float
    retrieval_latency_ms: float
    llm_latency_ms: float
    prompt_injection_detected: bool


class QueryApiResponse(BaseModel):
    answer: str
    abstained: bool
    evidence_score: float
    citations: list[CitationApi]
    metrics: QueryMetricsApi
    correlation_id: str
