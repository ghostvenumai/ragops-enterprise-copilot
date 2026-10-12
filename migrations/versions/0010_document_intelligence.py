"""Add document intelligence: persons, classifications, facts, claim groups, reviews (ENT-11.8).

Purely additive: no existing row is read, reclassified or rewritten, and the OCR tables of
0009 are untouched. Versions ingested earlier have no analysis until they are reprocessed.
Downgrade drops the nine new tables only.
"""

# ruff: noqa: E501, I001
from alembic import op
import sqlalchemy as sa

revision = "0010_document_intelligence"
down_revision = "0009_ocr_extraction"
branch_labels = None
depends_on = None

DOCUMENT_CLASSES = (
    "document_class IN ('invoice','payment_reminder','formal_demand','debt_collection_letter',"
    "'payment_order_or_court_notice','enforcement_document','creditor_statement','loan_agreement',"
    "'payment_receipt','settlement_agreement','correspondence','unrelated_document','unknown')"
)


def _base(table: str) -> list[sa.schema.SchemaItem]:
    return [
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("tenant_id", "id", name=op.f(f"pk_{table}")),
    ]


def _fk(column: str, table: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        ["tenant_id", column], [f"{table}.tenant_id", f"{table}.id"], ondelete="RESTRICT"
    )


def _check(table: str, name: str, condition: str) -> sa.CheckConstraint:
    return sa.CheckConstraint(condition, name=op.f(f"ck_{table}_{name}"))


def upgrade() -> None:
    op.create_table(
        "person_profiles",
        *_base("person_profiles"),
        sa.Column("label", sa.String(120), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("created_by", sa.String(255), nullable=False),
        sa.Column("review_version", sa.Integer(), nullable=False),
        sa.UniqueConstraint("tenant_id", "label", name="uq_person_profiles_label"),
        _check("person_profiles", "status", "status IN ('active','archived')"),
        _check("person_profiles", "review_version", "review_version >= 1"),
    )
    op.create_table(
        "person_aliases",
        *_base("person_aliases"),
        sa.Column("person_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("normalized_value", sa.String(255), nullable=False),
        sa.Column("created_by", sa.String(255), nullable=False),
        _fk("person_id", "person_profiles"),
        sa.UniqueConstraint(
            "tenant_id", "kind", "normalized_value", name="uq_person_aliases_value"
        ),
        _check("person_aliases", "kind", "kind IN ('name','identifier')"),
    )
    op.create_index("ix_person_aliases_tenant_person", "person_aliases", ["tenant_id", "person_id"])
    op.create_table(
        "document_intelligence_runs",
        *_base("document_intelligence_runs"),
        sa.Column("document_version_id", sa.Uuid(), nullable=False),
        sa.Column("extractor_version", sa.String(32), nullable=False),
        sa.Column("taxonomy_version", sa.String(32), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("error_code", sa.String(64)),
        sa.Column("classification_status", sa.String(16), nullable=False),
        sa.Column("debtor_status", sa.String(16), nullable=False),
        sa.Column("review_required", sa.Boolean(), nullable=False),
        sa.Column("flagged_for_review", sa.Boolean(), nullable=False),
        sa.Column("fact_count", sa.Integer(), nullable=False),
        sa.Column("ignored_instruction_lines", sa.Integer(), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("analyzed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("review_version", sa.Integer(), nullable=False),
        _fk("document_version_id", "document_versions"),
        sa.UniqueConstraint(
            "tenant_id", "document_version_id", name="uq_document_intelligence_runs_version"
        ),
        _check("document_intelligence_runs", "status", "status IN ('completed','failed')"),
        _check(
            "document_intelligence_runs",
            "classification_status",
            "classification_status IN ('classified','ambiguous','unknown')",
        ),
        _check(
            "document_intelligence_runs",
            "debtor_status",
            "debtor_status IN ('identified','ambiguous','unmatched','unknown')",
        ),
        _check(
            "document_intelligence_runs",
            "counts",
            "fact_count >= 0 AND ignored_instruction_lines >= 0",
        ),
        _check("document_intelligence_runs", "review_version", "review_version >= 1"),
    )
    op.create_table(
        "document_classifications",
        *_base("document_classifications"),
        sa.Column("document_version_id", sa.Uuid(), nullable=False),
        sa.Column("taxonomy_version", sa.String(32), nullable=False),
        sa.Column("document_class", sa.String(40), nullable=False),
        sa.Column("evidence_strength", sa.String(16), nullable=False),
        sa.Column("rank", sa.Integer(), nullable=False),
        sa.Column("is_primary", sa.Boolean(), nullable=False),
        sa.Column("evidence", sa.JSON(), nullable=False),
        sa.Column("extracted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("review_state", sa.String(24), nullable=False),
        sa.Column("review_version", sa.Integer(), nullable=False),
        _fk("document_version_id", "document_versions"),
        sa.UniqueConstraint(
            "tenant_id",
            "document_version_id",
            "taxonomy_version",
            "document_class",
            name="uq_document_classifications_class",
        ),
        _check("document_classifications", "document_class", DOCUMENT_CLASSES),
        _check(
            "document_classifications",
            "evidence_strength",
            "evidence_strength IN ('strong','moderate','weak')",
        ),
        _check(
            "document_classifications",
            "review_state",
            "review_state IN ('unreviewed','review_required','confirmed','rejected')",
        ),
        _check("document_classifications", "versions", "review_version >= 1 AND rank >= 1"),
    )
    op.create_index(
        "ix_document_classifications_tenant_class",
        "document_classifications",
        ["tenant_id", "document_class"],
    )
    op.create_table(
        "document_facts",
        *_base("document_facts"),
        sa.Column("document_version_id", sa.Uuid(), nullable=False),
        sa.Column("extractor_version", sa.String(32), nullable=False),
        sa.Column("fact_key", sa.String(64), nullable=False),
        sa.Column("category", sa.String(16), nullable=False),
        sa.Column("fact_type", sa.String(40), nullable=False),
        sa.Column("assertion", sa.String(16), nullable=False),
        sa.Column("raw_value", sa.String(255), nullable=False),
        sa.Column("normalized_value", sa.String(255)),
        sa.Column("amount", sa.Numeric(18, 2)),
        sa.Column("currency", sa.String(3)),
        sa.Column("date_value", sa.Date()),
        sa.Column("as_of_date", sa.Date()),
        sa.Column("page_number", sa.Integer()),
        sa.Column("source_line", sa.Integer(), nullable=False),
        sa.Column("char_start", sa.Integer(), nullable=False),
        sa.Column("char_end", sa.Integer(), nullable=False),
        sa.Column("chunk_index", sa.Integer()),
        sa.Column("ocr_line", sa.Integer()),
        sa.Column("extraction_method", sa.String(32), nullable=False),
        sa.Column("quality_state", sa.String(24), nullable=False),
        sa.Column("regions", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("review_state", sa.String(24), nullable=False),
        sa.Column("review_version", sa.Integer(), nullable=False),
        sa.Column("corrected_text", sa.String(255)),
        sa.Column("corrected_amount", sa.Numeric(18, 2)),
        sa.Column("corrected_currency", sa.String(3)),
        sa.Column("corrected_date", sa.Date()),
        _fk("document_version_id", "document_versions"),
        sa.UniqueConstraint(
            "tenant_id", "document_version_id", "fact_key", name="uq_document_facts_key"
        ),
        _check(
            "document_facts",
            "category",
            "category IN ('party','amount','identifier','date','status')",
        ),
        _check("document_facts", "assertion", "assertion IN ('document_stated','rule_inferred')"),
        _check(
            "document_facts",
            "quality_state",
            "quality_state IN ('accepted','review_required','not_applicable')",
        ),
        _check(
            "document_facts",
            "review_state",
            "review_state IN ('unreviewed','review_required','confirmed','corrected','rejected')",
        ),
        _check("document_facts", "status", "status IN ('active','withdrawn')"),
        _check("document_facts", "amount", "amount IS NULL OR amount >= 0"),
        _check(
            "document_facts",
            "corrected_amount",
            "corrected_amount IS NULL OR corrected_amount >= 0",
        ),
        _check("document_facts", "span", "char_start >= 0 AND char_end > char_start"),
        _check("document_facts", "page_number", "page_number IS NULL OR page_number >= 1"),
        _check("document_facts", "review_version", "review_version >= 1"),
    )
    for name, columns in (
        ("ix_document_facts_tenant_version", ["tenant_id", "document_version_id"]),
        ("ix_document_facts_tenant_type", ["tenant_id", "fact_type"]),
        ("ix_document_facts_tenant_value", ["tenant_id", "normalized_value"]),
        ("ix_document_facts_tenant_review", ["tenant_id", "review_state"]),
    ):
        op.create_index(name, "document_facts", columns)
    op.create_table(
        "document_person_links",
        *_base("document_person_links"),
        sa.Column("document_version_id", sa.Uuid(), nullable=False),
        sa.Column("person_id", sa.Uuid(), nullable=False),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("state", sa.String(24), nullable=False),
        sa.Column("origin", sa.String(16), nullable=False),
        sa.Column("basis", sa.JSON(), nullable=False),
        sa.Column("review_version", sa.Integer(), nullable=False),
        _fk("document_version_id", "document_versions"),
        _fk("person_id", "person_profiles"),
        sa.UniqueConstraint(
            "tenant_id",
            "document_version_id",
            "person_id",
            "role",
            name="uq_document_person_links_role",
        ),
        _check("document_person_links", "role", "role IN ('debtor','addressee')"),
        _check(
            "document_person_links",
            "state",
            "state IN ('evidenced','review_required','confirmed','rejected','withdrawn')",
        ),
        _check("document_person_links", "origin", "origin IN ('system','user')"),
        _check("document_person_links", "review_version", "review_version >= 1"),
    )
    op.create_index(
        "ix_document_person_links_tenant_person",
        "document_person_links",
        ["tenant_id", "person_id"],
    )
    op.create_table(
        "claim_groups",
        *_base("claim_groups"),
        sa.Column("group_key", sa.String(64), nullable=False),
        sa.Column("creditor_normalized", sa.String(255), nullable=False),
        sa.Column("identifier_type", sa.String(40), nullable=False),
        sa.Column("identifier_normalized", sa.String(120), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("basis", sa.JSON(), nullable=False),
        sa.Column("review_version", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("tenant_id", "group_key", name="uq_claim_groups_key"),
        _check(
            "claim_groups",
            "status",
            "status IN ('system_linked','review_required','contradictory','confirmed','dismissed')",
        ),
        _check(
            "claim_groups",
            "identifier_type",
            "identifier_type IN ('invoice_number','contract_reference')",
        ),
        _check("claim_groups", "review_version", "review_version >= 1"),
    )
    op.create_table(
        "claim_group_members",
        *_base("claim_group_members"),
        sa.Column("claim_group_id", sa.Uuid(), nullable=False),
        sa.Column("document_version_id", sa.Uuid(), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("origin", sa.String(16), nullable=False),
        sa.Column("basis", sa.JSON(), nullable=False),
        sa.Column("review_version", sa.Integer(), nullable=False),
        _fk("claim_group_id", "claim_groups"),
        _fk("document_version_id", "document_versions"),
        sa.UniqueConstraint(
            "tenant_id",
            "claim_group_id",
            "document_version_id",
            name="uq_claim_group_members_member",
        ),
        _check(
            "claim_group_members", "state", "state IN ('linked','proposed','confirmed','rejected')"
        ),
        _check("claim_group_members", "origin", "origin IN ('system','user')"),
        _check("claim_group_members", "review_version", "review_version >= 1"),
    )
    op.create_index(
        "ix_claim_group_members_tenant_version",
        "claim_group_members",
        ["tenant_id", "document_version_id"],
    )
    op.create_table(
        "intelligence_review_events",
        *_base("intelligence_review_events"),
        sa.Column("entity_type", sa.String(32), nullable=False),
        sa.Column("entity_id", sa.Uuid(), nullable=False),
        sa.Column("entity_version", sa.Integer(), nullable=False),
        sa.Column("document_version_id", sa.Uuid()),
        sa.Column("action", sa.String(32), nullable=False),
        sa.Column("reason_category", sa.String(32), nullable=False),
        sa.Column("previous_state", sa.JSON(), nullable=False),
        sa.Column("new_state", sa.JSON(), nullable=False),
        sa.Column("actor_subject", sa.String(255), nullable=False),
        sa.UniqueConstraint(
            "tenant_id",
            "entity_type",
            "entity_id",
            "entity_version",
            name="uq_intelligence_review_events_version",
        ),
        _check(
            "intelligence_review_events",
            "entity_type",
            "entity_type IN ('document','classification','fact','person_link','claim_group','claim_member')",
        ),
        _check("intelligence_review_events", "entity_version", "entity_version >= 2"),
    )
    op.create_index(
        "ix_intelligence_review_events_tenant_entity",
        "intelligence_review_events",
        ["tenant_id", "entity_type", "entity_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_intelligence_review_events_tenant_entity", table_name="intelligence_review_events"
    )
    op.drop_table("intelligence_review_events")
    op.drop_index("ix_claim_group_members_tenant_version", table_name="claim_group_members")
    op.drop_table("claim_group_members")
    op.drop_table("claim_groups")
    op.drop_index("ix_document_person_links_tenant_person", table_name="document_person_links")
    op.drop_table("document_person_links")
    for name in (
        "ix_document_facts_tenant_review",
        "ix_document_facts_tenant_value",
        "ix_document_facts_tenant_type",
        "ix_document_facts_tenant_version",
    ):
        op.drop_index(name, table_name="document_facts")
    op.drop_table("document_facts")
    op.drop_index("ix_document_classifications_tenant_class", table_name="document_classifications")
    op.drop_table("document_classifications")
    op.drop_table("document_intelligence_runs")
    op.drop_index("ix_person_aliases_tenant_person", table_name="person_aliases")
    op.drop_table("person_aliases")
    op.drop_table("person_profiles")
