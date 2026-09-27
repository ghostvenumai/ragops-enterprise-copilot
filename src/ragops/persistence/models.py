"""Tenant-owned SQLAlchemy schema. All inter-entity keys include the tenant.

This schema is an implementation foundation, not an enabled production runtime.
Alembic owns schema creation. JSON columns contain structured configuration only.
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
