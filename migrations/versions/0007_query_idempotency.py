"""Add durable, tenant-scoped idempotency records for the productive query path."""

# ruff: noqa: E501, I001
from alembic import op
import sqlalchemy as sa

revision = "0007_query_idempotency"
down_revision = "0006_finops_reservations"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "query_idempotency_records",
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("principal", sa.String(64), nullable=False),
        sa.Column("endpoint", sa.String(64), nullable=False),
        sa.Column("key_hash", sa.String(64), nullable=False),
        sa.Column("request_fingerprint", sa.String(64), nullable=False),
        sa.Column("correlation_id", sa.Uuid(), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("status_code", sa.Integer()),
        sa.Column("response", sa.JSON()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("tenant_id", "id"),
        sa.UniqueConstraint("tenant_id", "principal", "endpoint", "key_hash"),
        sa.CheckConstraint(
            "state IN ('claimed','executing','succeeded','retryable','failed','ambiguous_held')",
            name=op.f("ck_query_idempotency_records_state"),
        ),
        sa.CheckConstraint("attempt >= 1", name=op.f("ck_query_idempotency_records_attempt")),
    )
    op.create_index(
        "ix_query_idempotency_records_tenant_state",
        "query_idempotency_records",
        ["tenant_id", "state", "expires_at"],
    )


def downgrade() -> None:
    op.drop_table("query_idempotency_records")
