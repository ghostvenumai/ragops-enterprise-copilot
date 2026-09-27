"""Add lifecycle and management metadata for enterprise knowledge management."""

from alembic import op
import sqlalchemy as sa

revision = "0002_knowledge_management"
down_revision = "0001_enterprise"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Columns are nullable during the additive migration so existing phase-1
    # records remain readable; new service writes always provide values.
    additions = {
        "workspaces": [
            ("description", sa.Text()), ("slug", sa.String(120)),
            ("status", sa.String(32), "active"), ("updated_at", sa.DateTime(timezone=True)),
            ("created_by", sa.String(255), "system"),
        ],
        "knowledge_collections": [
            ("description", sa.Text()), ("slug", sa.String(120)),
            ("access_level", sa.String(32), "internal"), ("allowed_roles", sa.JSON()),
            ("active", sa.Boolean(), True), ("updated_at", sa.DateTime(timezone=True)),
            ("created_by", sa.String(255), "system"),
        ],
        "documents": [
            ("workspace_id", sa.Uuid()), ("logical_document_key", sa.String(255)),
            ("document_metadata", sa.JSON()),
            ("current_version_id", sa.Uuid()), ("status", sa.String(32), "uploaded"),
            ("updated_at", sa.DateTime(timezone=True)), ("created_by", sa.String(255), "system"),
        ],
        "document_versions": [
            ("storage_reference", sa.String(512), ""), ("size", sa.Integer(), 0),
            ("ingestion_status", sa.String(32), "uploaded"), ("superseded_by", sa.Uuid()),
        ],
    }
    for table, columns in additions.items():
        for item in columns:
            name, type_ = item[:2]
            column = sa.Column(name, type_, nullable=True)
            if len(item) == 3:
                column = sa.Column(name, type_, nullable=True, server_default=sa.text(repr(item[2])))
            op.add_column(table, column)
    op.create_index("uq_workspaces_tenant_slug", "workspaces", ["tenant_id", "slug"], unique=True)
    op.create_index(
        "uq_knowledge_collections_tenant_slug", "knowledge_collections",
        ["tenant_id", "workspace_id", "slug"], unique=True,
    )
    op.create_index("ix_workspaces_tenant_status", "workspaces", ["tenant_id", "status"])
    op.create_index("ix_collections_tenant_workspace", "knowledge_collections", ["tenant_id", "workspace_id"])
    op.create_index("ix_documents_tenant_status", "documents", ["tenant_id", "status"])
    op.create_index("ix_documents_tenant_collection", "documents", ["tenant_id", "collection_id"])
    op.create_index("ix_document_versions_tenant_document", "document_versions", ["tenant_id", "document_id"])
    op.create_index("ix_document_versions_content_hash", "document_versions", ["tenant_id", "content_hash"])


def downgrade() -> None:
    for table, index in (
        ("knowledge_collections", "uq_knowledge_collections_tenant_slug"),
        ("workspaces", "uq_workspaces_tenant_slug"),
        ("document_versions", "ix_document_versions_content_hash"),
        ("document_versions", "ix_document_versions_tenant_document"),
        ("documents", "ix_documents_tenant_collection"),
        ("documents", "ix_documents_tenant_status"),
        ("knowledge_collections", "ix_collections_tenant_workspace"),
        ("workspaces", "ix_workspaces_tenant_status"),
    ):
        op.drop_index(index, table_name=table)
    for table, columns in {
        "document_versions": ["storage_reference", "size", "ingestion_status", "superseded_by"],
            "documents": ["workspace_id", "logical_document_key", "document_metadata", "current_version_id", "status", "updated_at", "created_by"],
        "knowledge_collections": ["description", "slug", "access_level", "allowed_roles", "active", "updated_at", "created_by"],
        "workspaces": ["description", "slug", "status", "updated_at", "created_by"],
    }.items():
        for column in reversed(columns):
            op.drop_column(table, column)
