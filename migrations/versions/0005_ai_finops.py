"""AI FinOps budgets, quotas and alerts."""
# ruff: noqa: E501, I001

from alembic import op
import sqlalchemy as sa

revision = "0005_ai_finops"
down_revision = "0004_model_governance"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "tenant_budgets",
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("period_start", sa.Date(), nullable=False),
        sa.Column("period_end", sa.Date(), nullable=False),
        sa.Column("budget_amount", sa.Numeric(18, 8), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("warning_threshold", sa.Numeric(5, 4), nullable=False),
        sa.Column("soft_limit_threshold", sa.Numeric(5, 4), nullable=False),
        sa.Column("hard_limit_threshold", sa.Numeric(5, 4), nullable=True),
        sa.Column("enforcement_mode", sa.String(20), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("tenant_id", "id"),
        sa.UniqueConstraint("tenant_id", "period_start", "period_end"),
    )
    op.create_index(
        "ix_tenant_budgets_tenant_period",
        "tenant_budgets",
        ["tenant_id", "period_start", "period_end"],
    )
    op.create_table(
        "quota_policies",
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("period_days", sa.Integer(), nullable=False),
        sa.Column("max_requests", sa.Integer()),
        sa.Column("max_tokens", sa.Integer()),
        sa.Column("max_cost", sa.Numeric(18, 8)),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("tenant_id", "id"),
        sa.UniqueConstraint("tenant_id", "name"),
    )
    op.create_index("ix_quota_policies_tenant", "quota_policies", ["tenant_id"])
    op.create_table(
        "budget_alerts",
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("period_start", sa.Date(), nullable=False),
        sa.Column("kind", sa.String(64), nullable=False),
        sa.Column("threshold", sa.Numeric(8, 4), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("message", sa.String(255), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("tenant_id", "id"),
        sa.UniqueConstraint("tenant_id", "period_start", "kind"),
    )
    op.create_index("ix_budget_alerts_tenant_status", "budget_alerts", ["tenant_id", "status"])


def downgrade() -> None:
    op.drop_table("budget_alerts")
    op.drop_table("quota_policies")
    op.drop_table("tenant_budgets")
