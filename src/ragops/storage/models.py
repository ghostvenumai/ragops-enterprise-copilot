"""Typed domain models used by the local RAGOps runtime."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Literal

AccessLevel = Literal["public", "internal", "confidential", "restricted"]
Role = Literal["viewer", "sales", "support", "operations", "compliance", "admin"]


@dataclass(frozen=True)
class DocumentMetadata:
    document_id: str
    tenant_id: str
    title: str
    source_path: str
    source_type: str
    classification: str
    access_level: AccessLevel
    version: str
    department: str
    created_at: date
    valid_from: date
    valid_until: date | None
    content_hash: str
    synthetic: bool = True
    lineage: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class DocumentChunk:
    chunk_id: str
    document_id: str
    tenant_id: str
    text: str
    metadata: DocumentMetadata
    ordinal: int
    token_count: int
    has_prompt_injection: bool = False

    @property
    def citation(self) -> str:
        return f"{self.metadata.title}#{self.ordinal}"


@dataclass(frozen=True)
class SearchResult:
    chunk: DocumentChunk
    bm25_score: float
    vector_score: float
    combined_score: float
    rerank_score: float
    reasons: tuple[str, ...] = ()


@dataclass(frozen=True)
class Customer:
    customer_id: str
    tenant_id: str
    name: str
    segment: str
    owner: str
    region: str
    synthetic: bool = True


@dataclass(frozen=True)
class Contract:
    contract_id: str
    tenant_id: str
    customer_id: str
    product: str
    value_eur: int
    end_date: date
    status: str
    document_id: str
    synthetic: bool = True


@dataclass(frozen=True)
class SupportTicket:
    ticket_id: str
    tenant_id: str
    customer_id: str
    severity: str
    status: str
    title: str
    opened_at: date
    document_id: str
    synthetic: bool = True


@dataclass(frozen=True)
class Citation:
    source_id: str
    title: str
    tenant_id: str
    snippet: str
    score: float


@dataclass(frozen=True)
class QueryUser:
    user_id: str
    tenant_id: str
    role: Role


@dataclass(frozen=True)
class AuditEvent:
    event_id: str
    correlation_id: str
    event_type: str
    tenant_id: str
    user_id: str
    timestamp: datetime
    outcome: str
    details: dict[str, str | int | float | bool]
