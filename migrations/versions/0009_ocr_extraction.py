"""Add per-page extraction reports and chunk provenance for OCR ingestion (ENT-11.7).

Purely additive: no existing row is read or rewritten. Versions ingested before ENT-11.7 have
no page report; their chunks keep their stored page numbers. Downgrade drops both tables; the
document versions, their content and the vector index are untouched.
"""

# ruff: noqa: E501, I001
from alembic import op
import sqlalchemy as sa

revision = "0009_ocr_extraction"
down_revision = "0008_query_reconciliation"
branch_labels = None
depends_on = None

CLASSES = "classification IN ('native_text','scanned_image','mixed_content','empty_page','unreadable','ocr_review_required')"


def _base_columns() -> list[sa.Column[object]]:
    return [
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("document_version_id", sa.Uuid(), nullable=False),
    ]


def _base_constraints(table: str) -> list[sa.Constraint]:
    return [
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "document_version_id"],
            ["document_versions.tenant_id", "document_versions.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("tenant_id", "id", name=op.f(f"pk_{table}")),
    ]


def upgrade() -> None:
    op.create_table(
        "document_page_extractions",
        *_base_columns(),
        sa.Column("page_number", sa.Integer(), nullable=False),
        sa.Column("classification", sa.String(32), nullable=False),
        sa.Column("extraction_method", sa.String(32), nullable=False),
        sa.Column("schema_version", sa.String(32), nullable=False),
        sa.Column("coordinate_space", sa.String(32)),
        sa.Column("width", sa.Float()),
        sa.Column("height", sa.Float()),
        sa.Column("unit", sa.String(8)),
        sa.Column("engine", sa.String(32)),
        sa.Column("engine_version", sa.String(32)),
        sa.Column("config_version", sa.String(64)),
        sa.Column("language", sa.String(64)),
        sa.Column("quality_state", sa.String(24), nullable=False),
        sa.Column("mean_confidence", sa.Float()),
        sa.Column("low_confidence_ratio", sa.Float()),
        sa.Column("implausible_ratio", sa.Float()),
        sa.Column("char_count", sa.Integer(), nullable=False),
        sa.Column("word_count", sa.Integer(), nullable=False),
        sa.Column("flags", sa.JSON(), nullable=False),
        sa.Column("transformations", sa.JSON(), nullable=False),
        sa.Column("regions", sa.JSON(), nullable=False),
        sa.Column("lines", sa.JSON(), nullable=False),
        sa.Column("ocr_milliseconds", sa.Integer(), nullable=False),
        *_base_constraints("document_page_extractions"),
        sa.UniqueConstraint(
            "tenant_id",
            "document_version_id",
            "page_number",
            name="uq_document_page_extractions_page",
        ),
        sa.CheckConstraint(
            "page_number >= 1", name=op.f("ck_document_page_extractions_page_number")
        ),
        sa.CheckConstraint(CLASSES, name=op.f("ck_document_page_extractions_classification")),
        sa.CheckConstraint(
            "quality_state IN ('accepted','review_required','not_applicable')",
            name=op.f("ck_document_page_extractions_quality_state"),
        ),
        sa.CheckConstraint(
            "char_count >= 0 AND word_count >= 0", name=op.f("ck_document_page_extractions_counts")
        ),
    )
    op.create_index(
        "ix_document_page_extractions_tenant_version",
        "document_page_extractions",
        ["tenant_id", "document_version_id"],
    )
    op.create_table(
        "document_chunk_sources",
        *_base_columns(),
        sa.Column("chunk_index", sa.Integer(), nullable=False),
        sa.Column("chunk_id", sa.String(80), nullable=False),
        sa.Column("page_number", sa.Integer()),
        sa.Column("extraction_method", sa.String(32), nullable=False),
        sa.Column("regions", sa.JSON(), nullable=False),
        *_base_constraints("document_chunk_sources"),
        sa.UniqueConstraint(
            "tenant_id",
            "document_version_id",
            "chunk_index",
            name="uq_document_chunk_sources_chunk",
        ),
        sa.CheckConstraint("chunk_index >= 0", name=op.f("ck_document_chunk_sources_chunk_index")),
        sa.CheckConstraint(
            "page_number IS NULL OR page_number >= 1",
            name=op.f("ck_document_chunk_sources_page_number"),
        ),
    )
    op.create_index(
        "ix_document_chunk_sources_tenant_version",
        "document_chunk_sources",
        ["tenant_id", "document_version_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_document_chunk_sources_tenant_version", table_name="document_chunk_sources")
    op.drop_table("document_chunk_sources")
    op.drop_index(
        "ix_document_page_extractions_tenant_version", table_name="document_page_extractions"
    )
    op.drop_table("document_page_extractions")
