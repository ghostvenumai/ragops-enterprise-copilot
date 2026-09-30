"""Add tenant model policy and normalized routing/usage metadata."""

import sqlalchemy as sa
from alembic import op

revision = "0004_model_governance"
down_revision = "0003_async_ingestion"
branch_labels = None
depends_on = None


def upgrade() -> None:
    model_columns = [
        ("display_name", sa.String(200)),
        ("enabled", sa.Boolean()),
        ("routing_tier", sa.String(32)),
        ("context_window", sa.Integer()),
        ("supports_tools", sa.Boolean()),
        ("supports_structured_output", sa.Boolean()),
        ("latency_class", sa.String(32)),
        ("approved_use_cases", sa.JSON()),
        ("high_risk_allowed", sa.Boolean()),
        ("fallback_priority", sa.Integer()),
    ]
    for name, type_ in model_columns:
        op.add_column("model_configurations", sa.Column(name, type_, nullable=True))
    policy_columns = [
        ("name", sa.String(100)),
        ("allowed_providers", sa.JSON()),
        ("allowed_models", sa.JSON()),
        ("default_tier", sa.String(32)),
        ("max_routing_tier", sa.String(32)),
        ("high_risk_models", sa.JSON()),
        ("cost_ceiling_eur", sa.Numeric(18, 8)),
        ("fallback_enabled", sa.Boolean()),
    ]
    op.create_table(
        "tenant_model_policies",
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        *[sa.Column(name, type_, nullable=True) for name, type_ in policy_columns],
        sa.PrimaryKeyConstraint("tenant_id", "id", name="pk_tenant_model_policies"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="RESTRICT"),
        sa.UniqueConstraint("tenant_id", "name", name="uq_tenant_model_policies_tenant_id"),
    )
    usage_columns = [
        ("provider_id", sa.String(100)),
        ("model_id_text", sa.String(200)),
        ("routing_class", sa.String(32)),
        ("routing_reason", sa.String(255)),
        ("total_tokens", sa.Integer()),
        ("estimated_cost", sa.Numeric(18, 8)),
        ("actual_cost", sa.Numeric(18, 8)),
        ("fallback_count", sa.Integer()),
        ("request_id", sa.String(255)),
    ]
    for name, type_ in usage_columns:
        op.add_column("usage_records", sa.Column(name, type_, nullable=True))
    op.create_index("ix_tenant_model_policies_tenant", "tenant_model_policies", ["tenant_id"])
    op.create_index(
        "ix_usage_records_tenant_routing", "usage_records", ["tenant_id", "routing_class"]
    )


def downgrade() -> None:
    op.drop_index("ix_usage_records_tenant_routing", table_name="usage_records")
    op.drop_index("ix_tenant_model_policies_tenant", table_name="tenant_model_policies")
    for name in (
        "request_id",
        "fallback_count",
        "actual_cost",
        "estimated_cost",
        "total_tokens",
        "routing_reason",
        "routing_class",
        "model_id_text",
        "provider_id",
    ):
        op.drop_column("usage_records", name)
    op.drop_table("tenant_model_policies")
    for name in (
        "fallback_priority",
        "high_risk_allowed",
        "approved_use_cases",
        "latency_class",
        "supports_structured_output",
        "supports_tools",
        "context_window",
        "routing_tier",
        "enabled",
        "display_name",
    ):
        op.drop_column("model_configurations", name)
