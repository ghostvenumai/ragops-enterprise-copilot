"""Frozen enterprise persistence foundation; runtime models are never imported."""

import sqlalchemy as sa
from alembic import op

revision = "0001_enterprise"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "tenants",
        sa.Column("id", sa.String(64), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_tenants"),
    )
    op.create_table(
        "users",
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("issuer", sa.String(512), nullable=False),
        sa.Column("subject", sa.String(255), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.PrimaryKeyConstraint("tenant_id", "id", name="pk_users"),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name="fk_users_tenant_id_tenants", ondelete="RESTRICT"
        ),
        sa.UniqueConstraint("tenant_id", "issuer", "subject", name="uq_users_tenant_id"),
    )
    op.create_table(
        "role_assignments",
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("role", sa.String(32), nullable=False),
        sa.PrimaryKeyConstraint("tenant_id", "id", name="pk_role_assignments"),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name="fk_role_assignments_tenant_id_tenants",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "user_id"],
            ["users.tenant_id", "users.id"],
            name="fk_role_assignments_tenant_id_users",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("tenant_id", "user_id", "role", name="uq_role_assignments_tenant_id"),
        sa.CheckConstraint(
            "role IN ('viewer','sales','support','operations','compliance','admin')",
            name=op.f("ck_role_assignments_role"),
        ),
    )
    op.create_table(
        "workspaces",
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.PrimaryKeyConstraint("tenant_id", "id", name="pk_workspaces"),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name="fk_workspaces_tenant_id_tenants",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("tenant_id", "name", name="uq_workspaces_tenant_id"),
    )
    op.create_table(
        "knowledge_collections",
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.PrimaryKeyConstraint("tenant_id", "id", name="pk_knowledge_collections"),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name="fk_knowledge_collections_tenant_id_tenants",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "workspace_id"],
            ["workspaces.tenant_id", "workspaces.id"],
            name="fk_knowledge_collections_tenant_id_workspaces",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "tenant_id", "workspace_id", "name", name="uq_knowledge_collections_tenant_id"
        ),
    )
    op.create_table(
        "documents",
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("collection_id", sa.Uuid(), nullable=False),
        sa.Column("title", sa.String(255), nullable=False),
        sa.Column("access_level", sa.String(32), nullable=False),
        sa.Column("allowed_roles", sa.JSON(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.PrimaryKeyConstraint("tenant_id", "id", name="pk_documents"),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name="fk_documents_tenant_id_tenants",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "collection_id"],
            ["knowledge_collections.tenant_id", "knowledge_collections.id"],
            name="fk_documents_tenant_id_knowledge_collections",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "access_level IN ('public','internal','confidential','restricted')",
            name=op.f("ck_documents_access"),
        ),
    )
    op.create_table(
        "document_versions",
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("document_id", sa.Uuid(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("filename", sa.String(255), nullable=False),
        sa.Column("mime_type", sa.String(128), nullable=False),
        sa.Column("content", sa.LargeBinary(), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("valid_from", sa.DateTime(timezone=True), nullable=False),
        sa.Column("valid_to", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("tenant_id", "id", name="pk_document_versions"),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name="fk_document_versions_tenant_id_tenants",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "document_id"],
            ["documents.tenant_id", "documents.id"],
            name="fk_document_versions_tenant_id_documents",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "tenant_id", "document_id", "version", name="uq_document_versions_tenant_id"
        ),
        sa.CheckConstraint("version > 0", name=op.f("ck_document_versions_version_positive")),
        sa.CheckConstraint(
            "valid_to IS NULL OR valid_to > valid_from", name=op.f("ck_document_versions_validity")
        ),
    )
    op.create_table(
        "ingestion_jobs",
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("document_version_id", sa.Uuid(), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column("progress", sa.Integer(), nullable=False),
        sa.Column("correlation_id", sa.Uuid(), nullable=False),
        sa.Column("failure_code", sa.String(64), nullable=True),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("tenant_id", "id", name="pk_ingestion_jobs"),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name="fk_ingestion_jobs_tenant_id_tenants",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "document_version_id"],
            ["document_versions.tenant_id", "document_versions.id"],
            name="fk_ingestion_jobs_tenant_id_document_versions",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "state IN ('queued','running','completed','failed','cancelled')",
            name=op.f("ck_ingestion_jobs_state"),
        ),
        sa.CheckConstraint(
            "attempts >= 0 AND attempts <= max_attempts", name=op.f("ck_ingestion_jobs_attempts")
        ),
        sa.CheckConstraint(
            "max_attempts BETWEEN 1 AND 10", name=op.f("ck_ingestion_jobs_max_attempts")
        ),
        sa.CheckConstraint("progress BETWEEN 0 AND 100", name=op.f("ck_ingestion_jobs_progress")),
    )
    op.create_index("ix_ingestion_jobs_state", "ingestion_jobs", ["state"])
    op.create_table(
        "conversations",
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("title", sa.String(200), nullable=False),
        sa.PrimaryKeyConstraint("tenant_id", "id", name="pk_conversations"),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name="fk_conversations_tenant_id_tenants",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "user_id"],
            ["users.tenant_id", "users.id"],
            name="fk_conversations_tenant_id_users",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "workspace_id"],
            ["workspaces.tenant_id", "workspaces.id"],
            name="fk_conversations_tenant_id_workspaces",
            ondelete="RESTRICT",
        ),
    )
    op.create_table(
        "messages",
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("conversation_id", sa.Uuid(), nullable=False),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("tenant_id", "id", name="pk_messages"),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name="fk_messages_tenant_id_tenants", ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "conversation_id"],
            ["conversations.tenant_id", "conversations.id"],
            name="fk_messages_tenant_id_conversations",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint("role IN ('user','assistant')", name=op.f("ck_messages_role")),
    )
    op.create_table(
        "citations",
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("message_id", sa.Uuid(), nullable=False),
        sa.Column("document_version_id", sa.Uuid(), nullable=False),
        sa.Column("source_id", sa.String(512), nullable=False),
        sa.Column("score", sa.Float(), nullable=False),
        sa.PrimaryKeyConstraint("tenant_id", "id", name="pk_citations"),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name="fk_citations_tenant_id_tenants",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "message_id"],
            ["messages.tenant_id", "messages.id"],
            name="fk_citations_tenant_id_messages",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "document_version_id"],
            ["document_versions.tenant_id", "document_versions.id"],
            name="fk_citations_tenant_id_document_versions",
            ondelete="RESTRICT",
        ),
    )
    op.create_table(
        "audit_events",
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("actor_subject", sa.String(255), nullable=False),
        sa.Column("event_type", sa.String(64), nullable=False),
        sa.Column("outcome", sa.String(32), nullable=False),
        sa.Column("correlation_id", sa.Uuid(), nullable=False),
        sa.Column("details", sa.JSON(), nullable=False),
        sa.PrimaryKeyConstraint("tenant_id", "id", name="pk_audit_events"),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name="fk_audit_events_tenant_id_tenants",
            ondelete="RESTRICT",
        ),
    )
    op.create_table(
        "provider_configurations",
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("endpoint", sa.String(512), nullable=True),
        sa.Column("credential_reference", sa.String(255), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.PrimaryKeyConstraint("tenant_id", "id", name="pk_provider_configurations"),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name="fk_provider_configurations_tenant_id_tenants",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("tenant_id", "name", name="uq_provider_configurations_tenant_id"),
    )
    op.create_table(
        "model_configurations",
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("provider_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("approved", sa.Boolean(), nullable=False),
        sa.Column("input_price", sa.Numeric(18, 8), nullable=False),
        sa.Column("output_price", sa.Numeric(18, 8), nullable=False),
        sa.Column("routing_classes", sa.JSON(), nullable=False),
        sa.PrimaryKeyConstraint("tenant_id", "id", name="pk_model_configurations"),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name="fk_model_configurations_tenant_id_tenants",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "provider_id"],
            ["provider_configurations.tenant_id", "provider_configurations.id"],
            name="fk_model_configurations_tenant_id_provider_configurations",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "tenant_id", "provider_id", "name", name="uq_model_configurations_tenant_id"
        ),
        sa.CheckConstraint(
            "input_price >= 0 AND output_price >= 0",
            name=op.f("ck_model_configurations_nonnegative_price"),
        ),
    )
    op.create_table(
        "usage_records",
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("model_id", sa.Uuid(), nullable=False),
        sa.Column("correlation_id", sa.Uuid(), nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=False),
        sa.Column("output_tokens", sa.Integer(), nullable=False),
        sa.Column("cost", sa.Numeric(18, 8), nullable=False),
        sa.Column("latency_ms", sa.Float(), nullable=False),
        sa.Column("endpoint", sa.String(128), nullable=False),
        sa.Column("workflow", sa.String(100), nullable=False),
        sa.PrimaryKeyConstraint("tenant_id", "id", name="pk_usage_records"),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name="fk_usage_records_tenant_id_tenants",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "user_id"],
            ["users.tenant_id", "users.id"],
            name="fk_usage_records_tenant_id_users",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "model_id"],
            ["model_configurations.tenant_id", "model_configurations.id"],
            name="fk_usage_records_tenant_id_model_configurations",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("tenant_id", "correlation_id", name="uq_usage_records_tenant_id"),
        sa.CheckConstraint(
            "input_tokens >= 0 AND output_tokens >= 0", name=op.f("ck_usage_records_tokens")
        ),
        sa.CheckConstraint(
            "cost >= 0 AND latency_ms >= 0", name=op.f("ck_usage_records_cost_latency")
        ),
    )
    op.create_table(
        "budgets",
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("month", sa.Date(), nullable=False),
        sa.Column("amount", sa.Numeric(18, 8), nullable=False),
        sa.Column("hard_stop_ratio", sa.Numeric(5, 2), nullable=False),
        sa.PrimaryKeyConstraint("tenant_id", "id", name="pk_budgets"),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name="fk_budgets_tenant_id_tenants", ondelete="RESTRICT"
        ),
        sa.UniqueConstraint("tenant_id", "month", name="uq_budgets_tenant_id"),
        sa.CheckConstraint("amount > 0", name=op.f("ck_budgets_amount_positive")),
        sa.CheckConstraint("hard_stop_ratio >= 1", name=op.f("ck_budgets_hard_stop")),
    )
    op.create_table(
        "evaluation_runs",
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("configuration", sa.JSON(), nullable=False),
        sa.Column("results", sa.JSON(), nullable=False),
        sa.Column("dataset_hash", sa.String(64), nullable=False),
        sa.PrimaryKeyConstraint("tenant_id", "id", name="pk_evaluation_runs"),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name="fk_evaluation_runs_tenant_id_tenants",
            ondelete="RESTRICT",
        ),
    )
    op.create_table(
        "security_events",
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("audit_event_id", sa.Uuid(), nullable=False),
        sa.Column("category", sa.String(64), nullable=False),
        sa.Column("severity", sa.String(16), nullable=False),
        sa.PrimaryKeyConstraint("tenant_id", "id", name="pk_security_events"),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name="fk_security_events_tenant_id_tenants",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "audit_event_id"],
            ["audit_events.tenant_id", "audit_events.id"],
            name="fk_security_events_tenant_id_audit_events",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "severity IN ('info','low','medium','high','critical')",
            name=op.f("ck_security_events_severity"),
        ),
    )
    op.create_table(
        "system_settings",
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("key", sa.String(128), nullable=False),
        sa.Column("value", sa.JSON(), nullable=False),
        sa.PrimaryKeyConstraint("tenant_id", "id", name="pk_system_settings"),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name="fk_system_settings_tenant_id_tenants",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("tenant_id", "key", name="uq_system_settings_tenant_id"),
    )


def downgrade() -> None:
    raise RuntimeError("Destructive downgrade requires an operator-reviewed migration")
