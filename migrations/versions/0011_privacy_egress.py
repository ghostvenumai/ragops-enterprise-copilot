"""Add privacy egress authorization: tenant profiles, policies, revisions, decisions (ENT-13.1).

Purely additive. Without rows every external provider call is denied, so upgrading never
widens what may leave the system. Downgrade drops the four new tables only.
"""

# ruff: noqa: E501, I001
from alembic import op
import sqlalchemy as sa

revision = "0011_privacy_egress"
down_revision = "0010_document_intelligence"
branch_labels = None
depends_on = None

STATES = "state IN ('active','suspended','revoked')"


def _base(table: str) -> list[sa.schema.SchemaItem]:
    return [
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("tenant_id", "id", name=op.f(f"pk_{table}")),
    ]


def _check(table: str, name: str, condition: str) -> sa.CheckConstraint:
    return sa.CheckConstraint(condition, name=op.f(f"ck_{table}_{name}"))


def upgrade() -> None:
    op.create_table(
        "privacy_tenant_profiles",
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("synthetic_data_only", sa.Boolean(), nullable=False),
        sa.Column("recorded_by", sa.String(255), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("tenant_id", name=op.f("pk_privacy_tenant_profiles")),
    )
    table = "privacy_egress_policies"
    op.create_table(
        table,
        *_base(table),
        sa.Column("purpose", sa.String(32), nullable=False),
        sa.Column("provider_id", sa.String(100), nullable=False),
        sa.Column("model_id", sa.String(200), nullable=False),
        sa.Column("data_classes", sa.JSON(), nullable=False),
        sa.Column("data_scope", sa.String(24), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("valid_from", sa.DateTime(timezone=True), nullable=False),
        sa.Column("valid_until", sa.DateTime(timezone=True), nullable=False),
        sa.Column("legal_basis_ref", sa.String(200)),
        sa.Column("processor_agreement_ref", sa.String(200)),
        sa.Column("transfer_mechanism_ref", sa.String(200)),
        sa.Column("retention_control", sa.String(32)),
        sa.Column("approved_by", sa.String(255), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "tenant_id",
            "purpose",
            "provider_id",
            "model_id",
            name="uq_privacy_egress_policies_scope",
        ),
        _check(table, "state", STATES),
        _check(table, "data_scope", "data_scope IN ('synthetic_test_data','personal_data')"),
        _check(table, "version", "version >= 1"),
        _check(table, "validity", "valid_until > valid_from"),
        _check(
            table,
            "personal_data_prerequisites",
            "data_scope <> 'personal_data' OR (legal_basis_ref IS NOT NULL AND "
            "processor_agreement_ref IS NOT NULL AND transfer_mechanism_ref IS NOT NULL AND "
            "retention_control IS NOT NULL)",
        ),
        _check(
            table,
            "retention_control",
            "retention_control IS NULL OR retention_control IN ('store_false','zero_data_retention')",
        ),
    )
    table = "privacy_egress_policy_revisions"
    op.create_table(
        table,
        *_base(table),
        sa.Column("policy_id", sa.Uuid(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("snapshot", sa.JSON(), nullable=False),
        sa.Column("actor", sa.String(255), nullable=False),
        sa.Column("reason", sa.String(200), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id", "policy_id"],
            ["privacy_egress_policies.tenant_id", "privacy_egress_policies.id"],
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "tenant_id", "policy_id", "version", name="uq_privacy_egress_policy_revisions_version"
        ),
        _check(table, "state", STATES),
    )
    table = "privacy_egress_decisions"
    op.create_table(
        table,
        *_base(table),
        sa.Column("correlation_id", sa.String(64), nullable=False),
        sa.Column("stage", sa.String(16), nullable=False),
        sa.Column("purpose", sa.String(32), nullable=False),
        sa.Column("provider_id", sa.String(100), nullable=False),
        sa.Column("model_id", sa.String(200), nullable=False),
        sa.Column("data_classes", sa.JSON(), nullable=False),
        sa.Column("policy_id", sa.Uuid()),
        sa.Column("policy_version", sa.Integer()),
        sa.Column("decision", sa.String(8), nullable=False),
        sa.Column("reason_code", sa.String(48), nullable=False),
        _check(table, "decision", "decision IN ('allow','deny')"),
        _check(table, "stage", "stage IN ('precheck','boundary','dispatch','probe')"),
    )
    op.create_index(
        "ix_privacy_egress_decisions_tenant_correlation",
        "privacy_egress_decisions",
        ["tenant_id", "correlation_id"],
    )
    if op.get_bind().dialect.name == "postgresql":
        # Revisions and decisions are evidence: the database refuses to rewrite them.
        op.execute(
            "CREATE FUNCTION ragops_privacy_append_only() RETURNS trigger LANGUAGE plpgsql AS "
            "$$ BEGIN RAISE EXCEPTION 'privacy evidence is append-only'; END $$"
        )
        for name in ("privacy_egress_policy_revisions", "privacy_egress_decisions"):
            op.execute(
                f"CREATE TRIGGER {name}_append_only BEFORE UPDATE OR DELETE ON {name} "
                "FOR EACH ROW EXECUTE FUNCTION ragops_privacy_append_only()"
            )


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        for name in ("privacy_egress_policy_revisions", "privacy_egress_decisions"):
            op.execute(f"DROP TRIGGER IF EXISTS {name}_append_only ON {name}")
        op.execute("DROP FUNCTION IF EXISTS ragops_privacy_append_only()")
    op.drop_index(
        "ix_privacy_egress_decisions_tenant_correlation", table_name="privacy_egress_decisions"
    )
    op.drop_table("privacy_egress_decisions")
    op.drop_table("privacy_egress_policy_revisions")
    op.drop_table("privacy_egress_policies")
    op.drop_table("privacy_tenant_profiles")
