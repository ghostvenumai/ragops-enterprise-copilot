"""Add transactional tenant budget reservations."""

# ruff: noqa: E501, I001
from alembic import op
import sqlalchemy as sa

revision = "0006_finops_reservations"
down_revision = "0005_ai_finops"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "budget_reservations",
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("budget_id", sa.Uuid(), nullable=False),
        sa.Column("idempotency_key", sa.String(255), nullable=False),
        sa.Column("estimated_amount", sa.Numeric(18, 8), nullable=False),
        sa.Column("final_amount", sa.Numeric(18, 8)),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True)),
        sa.Column("committed_at", sa.DateTime(timezone=True)),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "budget_id"],
            ["tenant_budgets.tenant_id", "tenant_budgets.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("tenant_id", "id"),
        sa.UniqueConstraint("tenant_id", "idempotency_key"),
    )
    op.create_index(
        "ix_budget_reservations_tenant_status", "budget_reservations", ["tenant_id", "status"]
    )


def downgrade() -> None:
    op.drop_table("budget_reservations")
