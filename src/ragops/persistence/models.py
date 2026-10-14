"""Tenant-owned SQLAlchemy schema. All inter-entity keys include the tenant.

This schema is an implementation foundation, not an enabled production runtime.
Alembic owns schema creation. JSON columns contain structured configuration only, except the
bounded, expiring response snapshot of a succeeded idempotent query (``QueryIdempotencyRecord``).
"""
# ruff: noqa: E501

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    LargeBinary,
    MetaData,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    metadata = MetaData(
        naming_convention={
            "ix": "ix_%(table_name)s_%(column_0_name)s",
            "uq": "uq_%(table_name)s_%(column_0_name)s",
            "ck": "ck_%(table_name)s_%(constraint_name)s",
            "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
            "pk": "pk_%(table_name)s",
        }
    )


class Tenant(Base):
    __tablename__ = "tenants"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    active: Mapped[bool] = mapped_column(default=True)


class TenantOwned:
    tenant_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("tenants.id", ondelete="RESTRICT"), primary_key=True
    )
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )


def tenant_fk(column: str, table: str) -> ForeignKeyConstraint:
    return ForeignKeyConstraint(
        ["tenant_id", column], [f"{table}.tenant_id", f"{table}.id"], ondelete="RESTRICT"
    )


class User(TenantOwned, Base):
    __tablename__ = "users"
    __table_args__ = (UniqueConstraint("tenant_id", "issuer", "subject"),)
    issuer: Mapped[str] = mapped_column(String(512))
    subject: Mapped[str] = mapped_column(String(255))
    active: Mapped[bool] = mapped_column(default=True)


class RoleAssignment(TenantOwned, Base):
    __tablename__ = "role_assignments"
    __table_args__ = (
        tenant_fk("user_id", "users"),
        UniqueConstraint("tenant_id", "user_id", "role"),
        CheckConstraint(
            "role IN ('viewer','sales','support','operations','compliance','admin')", name="role"
        ),
    )
    user_id: Mapped[UUID]
    role: Mapped[str] = mapped_column(String(32))


class Workspace(TenantOwned, Base):
    __tablename__ = "workspaces"
    __table_args__ = (
        UniqueConstraint("tenant_id", "name"),
        Index("uq_workspaces_tenant_slug", "tenant_id", "slug", unique=True),
        Index("ix_workspaces_tenant_status", "tenant_id", "status"),
    )
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text, default="", nullable=True)
    slug: Mapped[str] = mapped_column(String(120), nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="active", nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )
    created_by: Mapped[str] = mapped_column(String(255), default="system", nullable=True)


class KnowledgeCollection(TenantOwned, Base):
    __tablename__ = "knowledge_collections"
    __table_args__ = (
        tenant_fk("workspace_id", "workspaces"),
        UniqueConstraint("tenant_id", "workspace_id", "name"),
        Index(
            "uq_knowledge_collections_tenant_slug", "tenant_id", "workspace_id", "slug", unique=True
        ),
        Index("ix_collections_tenant_workspace", "tenant_id", "workspace_id"),
    )
    workspace_id: Mapped[UUID]
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text, default="", nullable=True)
    slug: Mapped[str] = mapped_column(String(120), nullable=True)
    access_level: Mapped[str] = mapped_column(String(32), default="internal", nullable=True)
    allowed_roles: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=True)
    active: Mapped[bool] = mapped_column(default=True, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )
    created_by: Mapped[str] = mapped_column(String(255), default="system", nullable=True)


class Document(TenantOwned, Base):
    __tablename__ = "documents"
    __table_args__ = (
        tenant_fk("collection_id", "knowledge_collections"),
        Index("ix_documents_tenant_status", "tenant_id", "status"),
        Index("ix_documents_tenant_collection", "tenant_id", "collection_id"),
        CheckConstraint(
            "access_level IN ('public','internal','confidential','restricted')", name="access"
        ),
    )
    collection_id: Mapped[UUID]
    workspace_id: Mapped[UUID] = mapped_column(nullable=True)
    title: Mapped[str] = mapped_column(String(255))
    logical_document_key: Mapped[str] = mapped_column(String(255), nullable=True)
    document_metadata: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=True)
    current_version_id: Mapped[UUID | None] = mapped_column(nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="uploaded", nullable=True)
    access_level: Mapped[str] = mapped_column(String(32), default="internal")
    allowed_roles: Mapped[list[str]] = mapped_column(JSON, default=list)
    active: Mapped[bool] = mapped_column(default=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )
    created_by: Mapped[str] = mapped_column(String(255), default="system", nullable=True)


class DocumentVersion(TenantOwned, Base):
    __tablename__ = "document_versions"
    __table_args__ = (
        tenant_fk("document_id", "documents"),
        Index("ix_document_versions_tenant_document", "tenant_id", "document_id"),
        Index("ix_document_versions_content_hash", "tenant_id", "content_hash"),
        UniqueConstraint("tenant_id", "document_id", "version"),
        CheckConstraint("version > 0", name="version_positive"),
        CheckConstraint("valid_to IS NULL OR valid_to > valid_from", name="validity"),
    )
    document_id: Mapped[UUID]
    version: Mapped[int]
    filename: Mapped[str] = mapped_column(String(255))
    mime_type: Mapped[str] = mapped_column(String(128))
    content: Mapped[bytes] = mapped_column(LargeBinary)
    content_hash: Mapped[str] = mapped_column(String(64))
    storage_reference: Mapped[str] = mapped_column(String(512), default="", nullable=True)
    size: Mapped[int] = mapped_column(default=0, nullable=True)
    ingestion_status: Mapped[str] = mapped_column(String(32), default="uploaded", nullable=True)
    superseded_by: Mapped[UUID | None] = mapped_column(nullable=True)
    valid_from: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    valid_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    @property
    def version_number(self) -> int:
        return self.version

    @property
    def storage_ref(self) -> str:
        return self.storage_reference


class DocumentPageExtraction(TenantOwned, Base):
    """Per-page extraction report of a document version (ENT-11.7); never holds document text."""

    __tablename__ = "document_page_extractions"
    __table_args__ = (
        tenant_fk("document_version_id", "document_versions"),
        UniqueConstraint("tenant_id", "document_version_id", "page_number", name="uq_document_page_extractions_page"),
        Index("ix_document_page_extractions_tenant_version", "tenant_id", "document_version_id"),
        CheckConstraint("page_number >= 1", name="page_number"),
        CheckConstraint(
            "classification IN ('native_text','scanned_image','mixed_content','empty_page','unreadable','ocr_review_required')",
            name="classification",
        ),
        CheckConstraint("quality_state IN ('accepted','review_required','not_applicable')", name="quality_state"),
        CheckConstraint("char_count >= 0 AND word_count >= 0", name="counts"),
    )
    document_version_id: Mapped[UUID]
    page_number: Mapped[int]
    classification: Mapped[str] = mapped_column(String(32))
    extraction_method: Mapped[str] = mapped_column(String(32))
    schema_version: Mapped[str] = mapped_column(String(32))
    coordinate_space: Mapped[str | None] = mapped_column(String(32))
    width: Mapped[float | None]
    height: Mapped[float | None]
    unit: Mapped[str | None] = mapped_column(String(8))
    engine: Mapped[str | None] = mapped_column(String(32))
    engine_version: Mapped[str | None] = mapped_column(String(32))
    config_version: Mapped[str | None] = mapped_column(String(64))
    language: Mapped[str | None] = mapped_column(String(64))
    quality_state: Mapped[str] = mapped_column(String(24))
    mean_confidence: Mapped[float | None]
    low_confidence_ratio: Mapped[float | None]
    implausible_ratio: Mapped[float | None]
    char_count: Mapped[int] = mapped_column(default=0)
    word_count: Mapped[int] = mapped_column(default=0)
    flags: Mapped[list[str]] = mapped_column(JSON, default=list)
    transformations: Mapped[list[str]] = mapped_column(JSON, default=list)
    regions: Mapped[list[list[float]]] = mapped_column(JSON, default=list)
    lines: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    ocr_milliseconds: Mapped[int] = mapped_column(default=0)


class DocumentChunkSource(TenantOwned, Base):
    """Where a chunk came from: its source page, extraction method and OCR line boxes."""

    __tablename__ = "document_chunk_sources"
    __table_args__ = (
        tenant_fk("document_version_id", "document_versions"),
        UniqueConstraint("tenant_id", "document_version_id", "chunk_index", name="uq_document_chunk_sources_chunk"),
        Index("ix_document_chunk_sources_tenant_version", "tenant_id", "document_version_id"),
        CheckConstraint("chunk_index >= 0", name="chunk_index"),
        CheckConstraint("page_number IS NULL OR page_number >= 1", name="page_number"),
    )
    document_version_id: Mapped[UUID]
    chunk_index: Mapped[int]
    chunk_id: Mapped[str] = mapped_column(String(80))
    page_number: Mapped[int | None]
    extraction_method: Mapped[str] = mapped_column(String(32))
    regions: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)


class IngestionJob(TenantOwned, Base):
    __tablename__ = "ingestion_jobs"
    __table_args__ = (
        tenant_fk("document_version_id", "document_versions"),
        Index("ix_ingestion_jobs_tenant_status", "tenant_id", "status"),
        Index("ix_ingestion_jobs_tenant_document", "tenant_id", "document_id"),
        Index("ix_ingestion_jobs_tenant_updated", "tenant_id", "updated_at"),
        CheckConstraint(
            "state IN ('queued','running','completed','failed','cancelled')", name="state"
        ),
        CheckConstraint("attempts >= 0 AND attempts <= max_attempts", name="attempts"),
        CheckConstraint("max_attempts BETWEEN 1 AND 10", name="max_attempts"),
        CheckConstraint("progress BETWEEN 0 AND 100", name="progress"),
    )
    document_id: Mapped[UUID | None] = mapped_column(nullable=True)
    document_version_id: Mapped[UUID]
    status: Mapped[str] = mapped_column(String(16), default="queued", nullable=True)
    state: Mapped[str] = mapped_column(String(16), default="queued", index=True)
    stage: Mapped[str | None] = mapped_column(String(32), nullable=True)
    attempt_count: Mapped[int] = mapped_column(default=0, nullable=True)
    attempts: Mapped[int] = mapped_column(default=0)
    max_attempts: Mapped[int] = mapped_column(default=3)
    progress: Mapped[int] = mapped_column(default=0)
    correlation_id: Mapped[UUID] = mapped_column(default=uuid4)
    failure_code: Mapped[str | None] = mapped_column(String(64))
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message_redacted: Mapped[str | None] = mapped_column(String(512), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    worker_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Conversation(TenantOwned, Base):
    __tablename__ = "conversations"
    __table_args__ = (tenant_fk("user_id", "users"), tenant_fk("workspace_id", "workspaces"))
    user_id: Mapped[UUID]
    workspace_id: Mapped[UUID]
    title: Mapped[str] = mapped_column(String(200))


class Message(TenantOwned, Base):
    __tablename__ = "messages"
    __table_args__ = (
        tenant_fk("conversation_id", "conversations"),
        CheckConstraint("role IN ('user','assistant')", name="role"),
    )
    conversation_id: Mapped[UUID]
    role: Mapped[str] = mapped_column(String(16))
    content: Mapped[str] = mapped_column(Text)


class Citation(TenantOwned, Base):
    __tablename__ = "citations"
    __table_args__ = (
        tenant_fk("message_id", "messages"),
        tenant_fk("document_version_id", "document_versions"),
    )
    message_id: Mapped[UUID]
    document_version_id: Mapped[UUID]
    source_id: Mapped[str] = mapped_column(String(512))
    score: Mapped[float]


class AuditEvent(TenantOwned, Base):
    __tablename__ = "audit_events"
    actor_subject: Mapped[str] = mapped_column(String(255))
    event_type: Mapped[str] = mapped_column(String(64))
    outcome: Mapped[str] = mapped_column(String(32))
    correlation_id: Mapped[UUID]
    details: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class ProviderConfiguration(TenantOwned, Base):
    __tablename__ = "provider_configurations"
    __table_args__ = (UniqueConstraint("tenant_id", "name"),)
    name: Mapped[str] = mapped_column(String(100))
    kind: Mapped[str] = mapped_column(String(32))
    endpoint: Mapped[str | None] = mapped_column(String(512))
    credential_reference: Mapped[str | None] = mapped_column(String(255))
    enabled: Mapped[bool] = mapped_column(default=False)


class ModelConfiguration(TenantOwned, Base):
    __tablename__ = "model_configurations"
    __table_args__ = (
        tenant_fk("provider_id", "provider_configurations"),
        UniqueConstraint("tenant_id", "provider_id", "name"),
        CheckConstraint("input_price >= 0 AND output_price >= 0", name="nonnegative_price"),
    )
    provider_id: Mapped[UUID]
    name: Mapped[str] = mapped_column(String(200))
    approved: Mapped[bool] = mapped_column(default=False)
    input_price: Mapped[Decimal] = mapped_column(Numeric(18, 8))
    output_price: Mapped[Decimal] = mapped_column(Numeric(18, 8))
    routing_classes: Mapped[list[str]] = mapped_column(JSON, default=list)
    display_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    enabled: Mapped[bool] = mapped_column(default=True, nullable=True)
    routing_tier: Mapped[str | None] = mapped_column(String(32), nullable=True)
    context_window: Mapped[int | None] = mapped_column(nullable=True)
    supports_tools: Mapped[bool] = mapped_column(default=False, nullable=True)
    supports_structured_output: Mapped[bool] = mapped_column(default=False, nullable=True)
    latency_class: Mapped[str | None] = mapped_column(String(32), nullable=True)
    approved_use_cases: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=True)
    high_risk_allowed: Mapped[bool] = mapped_column(default=False, nullable=True)
    fallback_priority: Mapped[int] = mapped_column(default=100, nullable=True)


class TenantModelPolicy(TenantOwned, Base):
    __tablename__ = "tenant_model_policies"
    __table_args__ = (
        UniqueConstraint("tenant_id", "name"),
        Index("ix_tenant_model_policies_tenant", "tenant_id"),
    )
    name: Mapped[str] = mapped_column(String(100), default="default", nullable=True)
    allowed_providers: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=True)
    allowed_models: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=True)
    default_tier: Mapped[str] = mapped_column(String(32), default="standard", nullable=True)
    max_routing_tier: Mapped[str] = mapped_column(String(32), default="complex", nullable=True)
    high_risk_models: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=True)
    cost_ceiling_eur: Mapped[Decimal | None] = mapped_column(Numeric(18, 8), nullable=True)
    fallback_enabled: Mapped[bool] = mapped_column(default=True, nullable=True)


class UsageRecord(TenantOwned, Base):
    __tablename__ = "usage_records"
    __table_args__ = (
        tenant_fk("user_id", "users"),
        tenant_fk("model_id", "model_configurations"),
        UniqueConstraint("tenant_id", "correlation_id"),
        CheckConstraint("input_tokens >= 0 AND output_tokens >= 0", name="tokens"),
        CheckConstraint("cost >= 0 AND latency_ms >= 0", name="cost_latency"),
        Index("ix_usage_records_tenant_routing", "tenant_id", "routing_class"),
    )
    user_id: Mapped[UUID]
    model_id: Mapped[UUID]
    correlation_id: Mapped[UUID]
    input_tokens: Mapped[int]
    output_tokens: Mapped[int]
    cost: Mapped[Decimal] = mapped_column(Numeric(18, 8))
    latency_ms: Mapped[float]
    endpoint: Mapped[str] = mapped_column(String(128))
    workflow: Mapped[str] = mapped_column(String(100))
    provider_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    model_id_text: Mapped[str | None] = mapped_column(String(200), nullable=True)
    routing_class: Mapped[str | None] = mapped_column(String(32), nullable=True)
    routing_reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
    total_tokens: Mapped[int | None] = mapped_column(nullable=True)
    estimated_cost: Mapped[Decimal | None] = mapped_column(Numeric(18, 8), nullable=True)
    actual_cost: Mapped[Decimal | None] = mapped_column(Numeric(18, 8), nullable=True)
    fallback_count: Mapped[int] = mapped_column(default=0, nullable=True)
    request_id: Mapped[str | None] = mapped_column(String(255), nullable=True)


class Budget(TenantOwned, Base):
    __tablename__ = "budgets"
    __table_args__ = (
        UniqueConstraint("tenant_id", "month"),
        CheckConstraint("amount > 0", name="amount_positive"),
        CheckConstraint("hard_stop_ratio >= 1", name="hard_stop"),
    )
    month: Mapped[date]
    amount: Mapped[Decimal] = mapped_column(Numeric(18, 8))
    hard_stop_ratio: Mapped[Decimal] = mapped_column(Numeric(5, 2), default=Decimal("1.20"))


class TenantBudget(TenantOwned, Base):
    __tablename__ = "tenant_budgets"
    __table_args__ = (
        UniqueConstraint("tenant_id", "period_start", "period_end"),
        CheckConstraint("budget_amount > 0", name="budget_positive"),
        CheckConstraint("warning_threshold >= 0 AND warning_threshold <= 1", name="warning_ratio"),
        CheckConstraint("soft_limit_threshold >= 0", name="soft_ratio"),
        CheckConstraint("hard_limit_threshold IS NULL OR hard_limit_threshold >= soft_limit_threshold", name="hard_ratio"),
        Index("ix_tenant_budgets_tenant_period", "tenant_id", "period_start", "period_end"),
    )
    period_start: Mapped[date]
    period_end: Mapped[date]
    budget_amount: Mapped[Decimal] = mapped_column(Numeric(18, 8))
    currency: Mapped[str] = mapped_column(String(3), default="EUR")
    warning_threshold: Mapped[Decimal] = mapped_column(Numeric(5, 4), default=Decimal("0.80"))
    soft_limit_threshold: Mapped[Decimal] = mapped_column(Numeric(5, 4), default=Decimal("1.00"))
    hard_limit_threshold: Mapped[Decimal | None] = mapped_column(Numeric(5, 4), default=Decimal("1.20"), nullable=True)
    enforcement_mode: Mapped[str] = mapped_column(String(20), default="optimize")


class QuotaPolicy(TenantOwned, Base):
    __tablename__ = "quota_policies"
    __table_args__ = (UniqueConstraint("tenant_id", "name"), Index("ix_quota_policies_tenant", "tenant_id"))
    name: Mapped[str] = mapped_column(String(100), default="default")
    period_days: Mapped[int] = mapped_column(default=30)
    max_requests: Mapped[int | None] = mapped_column(nullable=True)
    max_tokens: Mapped[int | None] = mapped_column(nullable=True)
    max_cost: Mapped[Decimal | None] = mapped_column(Numeric(18, 8), nullable=True)
    currency: Mapped[str] = mapped_column(String(3), default="EUR")
    enabled: Mapped[bool] = mapped_column(default=True)


class BudgetAlert(TenantOwned, Base):
    __tablename__ = "budget_alerts"
    __table_args__ = (UniqueConstraint("tenant_id", "period_start", "kind"), Index("ix_budget_alerts_tenant_status", "tenant_id", "status"))
    period_start: Mapped[date]
    kind: Mapped[str] = mapped_column(String(64))
    threshold: Mapped[Decimal] = mapped_column(Numeric(8, 4))
    status: Mapped[str] = mapped_column(String(20), default="active")
    message: Mapped[str] = mapped_column(String(255))


class BudgetReservation(TenantOwned, Base):
    __tablename__ = "budget_reservations"
    __table_args__ = (
        tenant_fk("budget_id", "tenant_budgets"),
        UniqueConstraint("tenant_id", "idempotency_key"),
        Index("ix_budget_reservations_tenant_status", "tenant_id", "status"),
        CheckConstraint("estimated_amount >= 0", name="estimated_positive"),
        CheckConstraint("final_amount IS NULL OR final_amount >= 0", name="final_positive"),
        CheckConstraint("status IN ('reserved','committed','released','expired')", name="status"),
    )
    budget_id: Mapped[UUID]
    idempotency_key: Mapped[str] = mapped_column(String(255))
    estimated_amount: Mapped[Decimal] = mapped_column(Numeric(18, 8))
    final_amount: Mapped[Decimal | None] = mapped_column(Numeric(18, 8), nullable=True)
    currency: Mapped[str] = mapped_column(String(3), default="EUR")
    status: Mapped[str] = mapped_column(String(16), default="reserved")
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    committed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class QueryIdempotencyRecord(TenantOwned, Base):
    """One logical idempotent query per tenant, principal, endpoint and key digest.

    Only an HMAC-SHA256 of the client key under a server secret and a digest of the principal
    are stored. ``response`` holds the
    replay envelope of a finished request until ``expires_at``; ambiguous records keep no
    response and never expire on their own.
    """

    __tablename__ = "query_idempotency_records"
    __table_args__ = (
        UniqueConstraint("tenant_id", "principal", "endpoint", "key_hash"),
        Index("ix_query_idempotency_records_tenant_state", "tenant_id", "state", "expires_at"),
        CheckConstraint(
            "state IN ('claimed','executing','succeeded','retryable','failed','ambiguous_held',"
            "'reconciled')",
            name="state",
        ),
        CheckConstraint("attempt >= 1", name="attempt"),
    )
    principal: Mapped[str] = mapped_column(String(64))
    endpoint: Mapped[str] = mapped_column(String(64))
    key_hash: Mapped[str] = mapped_column(String(64))
    request_fingerprint: Mapped[str] = mapped_column(String(64))
    correlation_id: Mapped[UUID]
    state: Mapped[str] = mapped_column(String(16))
    attempt: Mapped[int] = mapped_column(default=1)
    status_code: Mapped[int | None] = mapped_column(nullable=True)
    response: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    lease_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class QueryExecution(TenantOwned, Base):
    """Durable provider boundary and usage receipt of one accounted query attempt (ENT-11.6).

    Written in the reservation's transaction as ``prepared``; ``provider_may_run`` is committed
    before the provider is invoked, ``receipt_recorded`` holds the reported usage and the
    execution-time price snapshot before it is booked, ``accounted`` is written together with
    the usage record and the reservation commit. ``released`` means the provider certainly did
    not bill. Only identifiers, counts and amounts are stored: no question, context, answer,
    key or credential.
    """

    __tablename__ = "query_executions"
    __table_args__ = (
        tenant_fk("reservation_id", "budget_reservations"),
        tenant_fk("user_id", "users"),
        tenant_fk("model_configuration_id", "model_configurations"),
        UniqueConstraint("tenant_id", "reservation_id", name="uq_query_executions_reservation"),
        UniqueConstraint(
            "tenant_id", "correlation_id", "attempt", name="uq_query_executions_attempt"
        ),
        Index("ix_query_executions_tenant_state", "tenant_id", "state", "updated_at"),
        CheckConstraint(
            "state IN ('prepared','provider_may_run','receipt_recorded','accounted','released')",
            name="state",
        ),
        CheckConstraint("attempt >= 1", name="attempt"),
        CheckConstraint(
            "state <> 'receipt_recorded' OR (provider_id IS NOT NULL AND model_id_text IS NOT "
            "NULL AND model_configuration_id IS NOT NULL AND input_price IS NOT NULL AND "
            "output_price IS NOT NULL AND input_tokens IS NOT NULL AND output_tokens IS NOT "
            "NULL AND cost IS NOT NULL AND latency_ms IS NOT NULL AND fallback_count IS NOT NULL)",
            name="receipt_complete",
        ),
        CheckConstraint(
            "(input_tokens IS NULL OR input_tokens >= 0) AND (output_tokens IS NULL OR "
            "output_tokens >= 0) AND (cost IS NULL OR cost >= 0) AND (input_price IS NULL OR "
            "input_price >= 0) AND (output_price IS NULL OR output_price >= 0) AND "
            "(latency_ms IS NULL OR latency_ms >= 0) AND (fallback_count IS NULL OR "
            "fallback_count >= 0) AND estimated_cost >= 0",
            name="nonnegative",
        ),
    )
    correlation_id: Mapped[UUID]
    attempt: Mapped[int] = mapped_column(default=1)
    reservation_id: Mapped[UUID]
    user_id: Mapped[UUID]
    state: Mapped[str] = mapped_column(String(24))
    routing_class: Mapped[str] = mapped_column(String(32))
    routing_reason: Mapped[str] = mapped_column(String(255))
    estimated_cost: Mapped[Decimal] = mapped_column(Numeric(18, 8))
    provider_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    model_id_text: Mapped[str | None] = mapped_column(String(200), nullable=True)
    model_configuration_id: Mapped[UUID | None] = mapped_column(nullable=True)
    input_price: Mapped[Decimal | None] = mapped_column(Numeric(18, 8), nullable=True)
    output_price: Mapped[Decimal | None] = mapped_column(Numeric(18, 8), nullable=True)
    input_tokens: Mapped[int | None] = mapped_column(nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(nullable=True)
    cost: Mapped[Decimal | None] = mapped_column(Numeric(18, 8), nullable=True)
    latency_ms: Mapped[float | None] = mapped_column(nullable=True)
    fallback_count: Mapped[int | None] = mapped_column(nullable=True)
    ambiguity_reason: Mapped[str | None] = mapped_column(String(32), nullable=True)
    resolution: Mapped[str | None] = mapped_column(String(32), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class EvaluationRun(TenantOwned, Base):
    __tablename__ = "evaluation_runs"
    configuration: Mapped[dict[str, Any]] = mapped_column(JSON)
    results: Mapped[dict[str, Any]] = mapped_column(JSON)
    dataset_hash: Mapped[str] = mapped_column(String(64))


class SecurityEvent(TenantOwned, Base):
    __tablename__ = "security_events"
    __table_args__ = (
        tenant_fk("audit_event_id", "audit_events"),
        CheckConstraint("severity IN ('info','low','medium','high','critical')", name="severity"),
    )
    audit_event_id: Mapped[UUID]
    category: Mapped[str] = mapped_column(String(64))
    severity: Mapped[str] = mapped_column(String(16))


class SystemSetting(TenantOwned, Base):
    __tablename__ = "system_settings"
    __table_args__ = (UniqueConstraint("tenant_id", "key"),)
    key: Mapped[str] = mapped_column(String(128))
    value: Mapped[dict[str, Any]] = mapped_column(JSON)


# --------------------------------------------------------------------------- document intelligence (ENT-11.8)

DOCUMENT_CLASS_CHECK = (
    "document_class IN ('invoice','payment_reminder','formal_demand','debt_collection_letter',"
    "'payment_order_or_court_notice','enforcement_document','creditor_statement','loan_agreement',"
    "'payment_receipt','settlement_agreement','correspondence','unrelated_document','unknown')"
)
FACT_REVIEW_CHECK = "review_state IN ('unreviewed','review_required','confirmed','corrected','rejected')"


class PersonProfile(TenantOwned, Base):
    """A tenant-scoped person with a stable id and a neutral label; a name is never an id."""

    __tablename__ = "person_profiles"
    __table_args__ = (
        UniqueConstraint("tenant_id", "label", name="uq_person_profiles_label"),
        CheckConstraint("status IN ('active','archived')", name="status"),
        CheckConstraint("review_version >= 1", name="review_version"),
    )
    label: Mapped[str] = mapped_column(String(120))
    status: Mapped[str] = mapped_column(String(16), default="active")
    created_by: Mapped[str] = mapped_column(String(255))
    review_version: Mapped[int] = mapped_column(default=1)


class PersonAlias(TenantOwned, Base):
    """An explicitly configured name or identifier; it belongs to at most one person."""

    __tablename__ = "person_aliases"
    __table_args__ = (
        tenant_fk("person_id", "person_profiles"),
        UniqueConstraint("tenant_id", "kind", "normalized_value", name="uq_person_aliases_value"),
        Index("ix_person_aliases_tenant_person", "tenant_id", "person_id"),
        CheckConstraint("kind IN ('name','identifier')", name="kind"),
    )
    person_id: Mapped[UUID]
    kind: Mapped[str] = mapped_column(String(16))
    normalized_value: Mapped[str] = mapped_column(String(255))
    created_by: Mapped[str] = mapped_column(String(255))


class DocumentIntelligenceRun(TenantOwned, Base):
    """The analysis of one document version: extractor version, outcome and review state."""

    __tablename__ = "document_intelligence_runs"
    __table_args__ = (
        tenant_fk("document_version_id", "document_versions"),
        UniqueConstraint("tenant_id", "document_version_id", name="uq_document_intelligence_runs_version"),
        CheckConstraint("status IN ('completed','failed')", name="status"),
        CheckConstraint("classification_status IN ('classified','ambiguous','unknown')", name="classification_status"),
        CheckConstraint("debtor_status IN ('identified','ambiguous','unmatched','unknown')", name="debtor_status"),
        CheckConstraint("fact_count >= 0 AND ignored_instruction_lines >= 0", name="counts"),
        CheckConstraint("review_version >= 1", name="review_version"),
    )
    document_version_id: Mapped[UUID]
    extractor_version: Mapped[str] = mapped_column(String(32))
    taxonomy_version: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(16))
    error_code: Mapped[str | None] = mapped_column(String(64))
    classification_status: Mapped[str] = mapped_column(String(16), default="unknown")
    debtor_status: Mapped[str] = mapped_column(String(16), default="unknown")
    review_required: Mapped[bool] = mapped_column(default=False)
    flagged_for_review: Mapped[bool] = mapped_column(default=False)
    fact_count: Mapped[int] = mapped_column(default=0)
    ignored_instruction_lines: Mapped[int] = mapped_column(default=0)
    content_hash: Mapped[str] = mapped_column(String(64))
    analyzed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    review_version: Mapped[int] = mapped_column(default=1)


class DocumentClassification(TenantOwned, Base):
    """One classification candidate with its rule evidence; never a probability."""

    __tablename__ = "document_classifications"
    __table_args__ = (
        tenant_fk("document_version_id", "document_versions"),
        UniqueConstraint("tenant_id", "document_version_id", "taxonomy_version", "document_class", name="uq_document_classifications_class"),
        Index("ix_document_classifications_tenant_class", "tenant_id", "document_class"),
        CheckConstraint(DOCUMENT_CLASS_CHECK, name="document_class"),
        CheckConstraint("evidence_strength IN ('strong','moderate','weak')", name="evidence_strength"),
        CheckConstraint("review_state IN ('unreviewed','review_required','confirmed','rejected')", name="review_state"),
        CheckConstraint("review_version >= 1 AND rank >= 1", name="versions"),
    )
    document_version_id: Mapped[UUID]
    taxonomy_version: Mapped[str] = mapped_column(String(32))
    document_class: Mapped[str] = mapped_column(String(40))
    evidence_strength: Mapped[str] = mapped_column(String(16))
    rank: Mapped[int]
    is_primary: Mapped[bool] = mapped_column(default=False)
    evidence: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    extracted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    review_state: Mapped[str] = mapped_column(String(24), default="unreviewed")
    review_version: Mapped[int] = mapped_column(default=1)


class DocumentFact(TenantOwned, Base):
    """An immutable extracted fact with provenance; user decisions live beside it."""

    __tablename__ = "document_facts"
    __table_args__ = (
        tenant_fk("document_version_id", "document_versions"),
        UniqueConstraint("tenant_id", "document_version_id", "fact_key", name="uq_document_facts_key"),
        Index("ix_document_facts_tenant_version", "tenant_id", "document_version_id"),
        Index("ix_document_facts_tenant_type", "tenant_id", "fact_type"),
        Index("ix_document_facts_tenant_value", "tenant_id", "normalized_value"),
        Index("ix_document_facts_tenant_review", "tenant_id", "review_state"),
        CheckConstraint("category IN ('party','amount','identifier','date','status')", name="category"),
        CheckConstraint("assertion IN ('document_stated','rule_inferred')", name="assertion"),
        CheckConstraint("quality_state IN ('accepted','review_required','not_applicable')", name="quality_state"),
        CheckConstraint(FACT_REVIEW_CHECK, name="review_state"),
        CheckConstraint("status IN ('active','withdrawn')", name="status"),
        CheckConstraint("amount IS NULL OR amount >= 0", name="amount"),
        CheckConstraint("corrected_amount IS NULL OR corrected_amount >= 0", name="corrected_amount"),
        CheckConstraint("char_start >= 0 AND char_end > char_start", name="span"),
        CheckConstraint("page_number IS NULL OR page_number >= 1", name="page_number"),
        CheckConstraint("review_version >= 1", name="review_version"),
    )
    document_version_id: Mapped[UUID]
    extractor_version: Mapped[str] = mapped_column(String(32))
    fact_key: Mapped[str] = mapped_column(String(64))
    category: Mapped[str] = mapped_column(String(16))
    fact_type: Mapped[str] = mapped_column(String(40))
    assertion: Mapped[str] = mapped_column(String(16))
    raw_value: Mapped[str] = mapped_column(String(255))
    normalized_value: Mapped[str | None] = mapped_column(String(255))
    amount: Mapped[Decimal | None] = mapped_column(Numeric(18, 2))
    currency: Mapped[str | None] = mapped_column(String(3))
    date_value: Mapped[date | None]
    as_of_date: Mapped[date | None]
    page_number: Mapped[int | None]
    source_line: Mapped[int]
    char_start: Mapped[int]
    char_end: Mapped[int]
    chunk_index: Mapped[int | None]
    ocr_line: Mapped[int | None]
    extraction_method: Mapped[str] = mapped_column(String(32))
    quality_state: Mapped[str] = mapped_column(String(24))
    regions: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(16), default="active")
    review_state: Mapped[str] = mapped_column(String(24), default="unreviewed")
    review_version: Mapped[int] = mapped_column(default=1)
    corrected_text: Mapped[str | None] = mapped_column(String(255))
    corrected_amount: Mapped[Decimal | None] = mapped_column(Numeric(18, 2))
    corrected_currency: Mapped[str | None] = mapped_column(String(3))
    corrected_date: Mapped[date | None]


class DocumentPersonLink(TenantOwned, Base):
    """A person's role in one document version, from evidence or from a reviewer."""

    __tablename__ = "document_person_links"
    __table_args__ = (
        tenant_fk("document_version_id", "document_versions"),
        tenant_fk("person_id", "person_profiles"),
        UniqueConstraint("tenant_id", "document_version_id", "person_id", "role", name="uq_document_person_links_role"),
        Index("ix_document_person_links_tenant_person", "tenant_id", "person_id"),
        CheckConstraint("role IN ('debtor','addressee')", name="role"),
        CheckConstraint("state IN ('evidenced','review_required','confirmed','rejected','withdrawn')", name="state"),
        CheckConstraint("origin IN ('system','user')", name="origin"),
        CheckConstraint("review_version >= 1", name="review_version"),
    )
    document_version_id: Mapped[UUID]
    person_id: Mapped[UUID]
    role: Mapped[str] = mapped_column(String(16))
    state: Mapped[str] = mapped_column(String(24))
    origin: Mapped[str] = mapped_column(String(16), default="system")
    basis: Mapped[list[str]] = mapped_column(JSON, default=list)
    review_version: Mapped[int] = mapped_column(default=1)


class ClaimGroup(TenantOwned, Base):
    """Documents about one underlying claim, keyed by creditor and claim identifier."""

    __tablename__ = "claim_groups"
    __table_args__ = (
        UniqueConstraint("tenant_id", "group_key", name="uq_claim_groups_key"),
        CheckConstraint("status IN ('system_linked','review_required','contradictory','confirmed','dismissed')", name="status"),
        CheckConstraint("identifier_type IN ('invoice_number','contract_reference')", name="identifier_type"),
        CheckConstraint("review_version >= 1", name="review_version"),
    )
    group_key: Mapped[str] = mapped_column(String(64))
    creditor_normalized: Mapped[str] = mapped_column(String(255))
    identifier_type: Mapped[str] = mapped_column(String(40))
    identifier_normalized: Mapped[str] = mapped_column(String(120))
    status: Mapped[str] = mapped_column(String(24), default="system_linked")
    basis: Mapped[list[str]] = mapped_column(JSON, default=list)
    review_version: Mapped[int] = mapped_column(default=1)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ClaimGroupMember(TenantOwned, Base):
    """Why a document version belongs to a claim group; rejected members stay rejected."""

    __tablename__ = "claim_group_members"
    __table_args__ = (
        tenant_fk("claim_group_id", "claim_groups"),
        tenant_fk("document_version_id", "document_versions"),
        UniqueConstraint("tenant_id", "claim_group_id", "document_version_id", name="uq_claim_group_members_member"),
        Index("ix_claim_group_members_tenant_version", "tenant_id", "document_version_id"),
        CheckConstraint("state IN ('linked','proposed','confirmed','rejected')", name="state"),
        CheckConstraint("origin IN ('system','user')", name="origin"),
        CheckConstraint("review_version >= 1", name="review_version"),
    )
    claim_group_id: Mapped[UUID]
    document_version_id: Mapped[UUID]
    state: Mapped[str] = mapped_column(String(16))
    origin: Mapped[str] = mapped_column(String(16), default="system")
    basis: Mapped[list[str]] = mapped_column(JSON, default=list)
    review_version: Mapped[int] = mapped_column(default=1)


class IntelligenceReviewEvent(TenantOwned, Base):
    """Append-only reviewer decision; (entity, version) is unique, so a race has one winner."""

    __tablename__ = "intelligence_review_events"
    __table_args__ = (
        UniqueConstraint("tenant_id", "entity_type", "entity_id", "entity_version", name="uq_intelligence_review_events_version"),
        Index("ix_intelligence_review_events_tenant_entity", "tenant_id", "entity_type", "entity_id"),
        CheckConstraint("entity_type IN ('document','classification','fact','person_link','claim_group','claim_member')", name="entity_type"),
        CheckConstraint("entity_version >= 2", name="entity_version"),
    )
    entity_type: Mapped[str] = mapped_column(String(32))
    entity_id: Mapped[UUID]
    entity_version: Mapped[int]
    document_version_id: Mapped[UUID | None]
    action: Mapped[str] = mapped_column(String(32))
    reason_category: Mapped[str] = mapped_column(String(32))
    previous_state: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    new_state: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    actor_subject: Mapped[str] = mapped_column(String(255))


# ----------------------------------------------------------------- privacy egress (ENT-13.1)

PRIVACY_STATES = "state IN ('active','suspended','revoked')"
PRIVACY_SCOPES = "data_scope IN ('synthetic_test_data','personal_data')"


class PrivacyTenantProfile(Base):
    """Operator statement that a tenant holds synthetic test data only (absent = personal data)."""

    __tablename__ = "privacy_tenant_profiles"
    tenant_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("tenants.id", ondelete="RESTRICT"), primary_key=True
    )
    synthetic_data_only: Mapped[bool] = mapped_column(default=False)
    recorded_by: Mapped[str] = mapped_column(String(255))
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class PrivacyEgressPolicy(TenantOwned, Base):
    """One exact external-processing authorization; absent, inactive or expired means deny."""

    __tablename__ = "privacy_egress_policies"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "purpose",
            "provider_id",
            "model_id",
            name="uq_privacy_egress_policies_scope",
        ),
        CheckConstraint(PRIVACY_STATES, name="state"),
        CheckConstraint(PRIVACY_SCOPES, name="data_scope"),
        CheckConstraint("version >= 1", name="version"),
        CheckConstraint("valid_until > valid_from", name="validity"),
        CheckConstraint(
            "data_scope <> 'personal_data' OR (legal_basis_ref IS NOT NULL AND "
            "processor_agreement_ref IS NOT NULL AND transfer_mechanism_ref IS NOT NULL AND "
            "retention_control IS NOT NULL)",
            name="personal_data_prerequisites",
        ),
        CheckConstraint(
            "retention_control IS NULL OR retention_control IN ('store_false','zero_data_retention')",
            name="retention_control",
        ),
    )
    purpose: Mapped[str] = mapped_column(String(32))
    provider_id: Mapped[str] = mapped_column(String(100))
    model_id: Mapped[str] = mapped_column(String(200))
    data_classes: Mapped[list[str]] = mapped_column(JSON, default=list)
    data_scope: Mapped[str] = mapped_column(String(24))
    state: Mapped[str] = mapped_column(String(16))
    version: Mapped[int] = mapped_column(default=1)
    valid_from: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    valid_until: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    legal_basis_ref: Mapped[str | None] = mapped_column(String(200), nullable=True)
    processor_agreement_ref: Mapped[str | None] = mapped_column(String(200), nullable=True)
    transfer_mechanism_ref: Mapped[str | None] = mapped_column(String(200), nullable=True)
    retention_control: Mapped[str | None] = mapped_column(String(32), nullable=True)
    approved_by: Mapped[str] = mapped_column(String(255))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class PrivacyEgressPolicyRevision(TenantOwned, Base):
    """Append-only history: every state of a policy with the operator who caused it."""

    __tablename__ = "privacy_egress_policy_revisions"
    __table_args__ = (
        tenant_fk("policy_id", "privacy_egress_policies"),
        UniqueConstraint(
            "tenant_id", "policy_id", "version", name="uq_privacy_egress_policy_revisions_version"
        ),
        CheckConstraint(PRIVACY_STATES, name="state"),
    )
    policy_id: Mapped[UUID]
    version: Mapped[int]
    state: Mapped[str] = mapped_column(String(16))
    snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    actor: Mapped[str] = mapped_column(String(255))
    reason: Mapped[str] = mapped_column(String(200))


class PrivacyEgressDecision(TenantOwned, Base):
    """Append-only authorization decision; metadata only, never payload content."""

    __tablename__ = "privacy_egress_decisions"
    __table_args__ = (
        Index("ix_privacy_egress_decisions_tenant_correlation", "tenant_id", "correlation_id"),
        CheckConstraint("decision IN ('allow','deny')", name="decision"),
        CheckConstraint("stage IN ('precheck','boundary','dispatch','probe')", name="stage"),
    )
    correlation_id: Mapped[str] = mapped_column(String(64))
    stage: Mapped[str] = mapped_column(String(16))
    purpose: Mapped[str] = mapped_column(String(32))
    provider_id: Mapped[str] = mapped_column(String(100))
    model_id: Mapped[str] = mapped_column(String(200))
    data_classes: Mapped[list[str]] = mapped_column(JSON, default=list)
    policy_id: Mapped[UUID | None] = mapped_column(nullable=True)
    policy_version: Mapped[int | None] = mapped_column(nullable=True)
    decision: Mapped[str] = mapped_column(String(8))
    reason_code: Mapped[str] = mapped_column(String(48))
