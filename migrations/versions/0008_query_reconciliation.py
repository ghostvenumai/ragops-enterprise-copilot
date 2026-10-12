"""Add the durable query execution ledger and the reconciled idempotency state.

No existing row is rewritten on upgrade: ENT-11.5 reservations and ambiguous records have no
provider-boundary evidence, stay held and are reported as review-required. On downgrade a
reconciled idempotency record becomes ``ambiguous_held`` (never executable, never "not
billed") and the ledger is dropped; reservations and usage records are untouched.
"""

# ruff: noqa: E501, I001
from alembic import op
import sqlalchemy as sa

revision = "0008_query_reconciliation"
down_revision = "0007_query_idempotency"
branch_labels = None
depends_on = None

OLD_STATES = "state IN ('claimed','executing','succeeded','retryable','failed','ambiguous_held')"
NEW_STATES = (
    "state IN ('claimed','executing','succeeded','retryable','failed','ambiguous_held',"
    "'reconciled')"
)


def _idempotency_states(condition: str) -> None:
    name = op.f("ck_query_idempotency_records_state")
    with op.batch_alter_table("query_idempotency_records") as batch:
        batch.drop_constraint(name, type_="check")
        batch.create_check_constraint(name, condition)


def upgrade() -> None:
    op.create_table(
        "query_executions",
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("correlation_id", sa.Uuid(), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("reservation_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("state", sa.String(24), nullable=False),
        sa.Column("routing_class", sa.String(32), nullable=False),
        sa.Column("routing_reason", sa.String(255), nullable=False),
        sa.Column("estimated_cost", sa.Numeric(18, 8), nullable=False),
        sa.Column("provider_id", sa.String(100)),
        sa.Column("model_id_text", sa.String(200)),
        sa.Column("model_configuration_id", sa.Uuid()),
        sa.Column("input_price", sa.Numeric(18, 8)),
        sa.Column("output_price", sa.Numeric(18, 8)),
        sa.Column("input_tokens", sa.Integer()),
        sa.Column("output_tokens", sa.Integer()),
        sa.Column("cost", sa.Numeric(18, 8)),
        sa.Column("latency_ms", sa.Float()),
        sa.Column("fallback_count", sa.Integer()),
        sa.Column("ambiguity_reason", sa.String(32)),
        sa.Column("resolution", sa.String(32)),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True)),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "reservation_id"],
            ["budget_reservations.tenant_id", "budget_reservations.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "user_id"], ["users.tenant_id", "users.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "model_configuration_id"],
            ["model_configurations.tenant_id", "model_configurations.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("tenant_id", "id"),
        sa.UniqueConstraint("tenant_id", "reservation_id", name="uq_query_executions_reservation"),
        sa.UniqueConstraint(
            "tenant_id", "correlation_id", "attempt", name="uq_query_executions_attempt"
        ),
        sa.CheckConstraint(
            "state IN ('prepared','provider_may_run','receipt_recorded','accounted','released')",
            name=op.f("ck_query_executions_state"),
        ),
        sa.CheckConstraint("attempt >= 1", name=op.f("ck_query_executions_attempt")),
        sa.CheckConstraint(
            "state <> 'receipt_recorded' OR (provider_id IS NOT NULL AND model_id_text IS NOT "
            "NULL AND model_configuration_id IS NOT NULL AND input_price IS NOT NULL AND "
            "output_price IS NOT NULL AND input_tokens IS NOT NULL AND output_tokens IS NOT "
            "NULL AND cost IS NOT NULL AND latency_ms IS NOT NULL AND fallback_count IS NOT NULL)",
            name=op.f("ck_query_executions_receipt_complete"),
        ),
        sa.CheckConstraint(
            "(input_tokens IS NULL OR input_tokens >= 0) AND (output_tokens IS NULL OR "
            "output_tokens >= 0) AND (cost IS NULL OR cost >= 0) AND (input_price IS NULL OR "
            "input_price >= 0) AND (output_price IS NULL OR output_price >= 0) AND "
            "(latency_ms IS NULL OR latency_ms >= 0) AND (fallback_count IS NULL OR "
            "fallback_count >= 0) AND estimated_cost >= 0",
            name=op.f("ck_query_executions_nonnegative"),
        ),
    )
    op.create_index(
        "ix_query_executions_tenant_state",
        "query_executions",
        ["tenant_id", "state", "updated_at"],
    )
    _idempotency_states(NEW_STATES)


def downgrade() -> None:
    op.execute(
        "UPDATE query_idempotency_records SET state = 'ambiguous_held', status_code = NULL, "
        "response = NULL WHERE state = 'reconciled'"
    )
    _idempotency_states(OLD_STATES)
    op.drop_index("ix_query_executions_tenant_state", table_name="query_executions")
    op.drop_table("query_executions")
