"""Add explicit asynchronous ingestion job state and observability fields."""

from alembic import op
import sqlalchemy as sa

revision = "0003_async_ingestion"
down_revision = "0002_knowledge_management"
branch_labels = None
depends_on = None


def upgrade() -> None:
    columns = [
        ("document_id", sa.Uuid()), ("status", sa.String(16)), ("stage", sa.String(32)),
        ("attempt_count", sa.Integer()), ("error_code", sa.String(64)),
        ("error_message_redacted", sa.String(512)), ("started_at", sa.DateTime(timezone=True)),
        ("completed_at", sa.DateTime(timezone=True)), ("updated_at", sa.DateTime(timezone=True)),
        ("worker_id", sa.String(128)),
    ]
    for name, type_ in columns:
        op.add_column("ingestion_jobs", sa.Column(name, type_, nullable=True))
    op.create_index("ix_ingestion_jobs_tenant_status", "ingestion_jobs", ["tenant_id", "status"])
    op.create_index("ix_ingestion_jobs_tenant_document", "ingestion_jobs", ["tenant_id", "document_id"])
    op.create_index("ix_ingestion_jobs_tenant_updated", "ingestion_jobs", ["tenant_id", "updated_at"])


def downgrade() -> None:
    for index in ("ix_ingestion_jobs_tenant_updated", "ix_ingestion_jobs_tenant_document", "ix_ingestion_jobs_tenant_status"):
        op.drop_index(index, table_name="ingestion_jobs")
    for name in ("worker_id", "updated_at", "completed_at", "started_at", "error_message_redacted", "error_code", "attempt_count", "stage", "status", "document_id"):
        op.drop_column("ingestion_jobs", name)
