"""Tenant- and access-level-scoped page reports and chunk provenance (ENT-11.7).

Both views come from PostgreSQL, the authority for positions; they carry classification,
quality, geometry and identifiers only, never document text. A document outside the caller's
tenant, a version of another document and a document above the caller's access level are
indistinguishable from a missing one.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any
from uuid import UUID

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from ragops.auth.rbac import can_access_level
from ragops.persistence.models import (
    Document,
    DocumentChunkSource,
    DocumentPageExtraction,
    DocumentVersion,
)
from ragops.storage.models import Role

REVIEW_CLASSES = ("unreadable", "ocr_review_required")


def page_counts(
    session: Session, tenant_id: str, version_ids: Iterable[UUID | None]
) -> dict[UUID, tuple[int, int]]:
    """``{version_id: (pages, pages needing review)}`` for the tenant's versions."""
    ids = [value for value in version_ids if value is not None]
    if not ids:
        return {}
    rows = session.execute(
        select(
            DocumentPageExtraction.document_version_id,
            func.count(),
            func.sum(case((DocumentPageExtraction.classification.in_(REVIEW_CLASSES), 1), else_=0)),
        )
        .where(
            DocumentPageExtraction.tenant_id == tenant_id,
            DocumentPageExtraction.document_version_id.in_(ids),
        )
        .group_by(DocumentPageExtraction.document_version_id)
    )
    return {version: (int(total), int(review or 0)) for version, total, review in rows}


def _version(
    session: Session, tenant_id: str, role: Role, document_id: UUID, version_id: UUID
) -> tuple[Document, DocumentVersion] | None:
    document = session.scalar(
        select(Document).where(Document.tenant_id == tenant_id, Document.id == document_id)
    )
    if document is None or not can_access_level(role, document.access_level):  # type: ignore[arg-type]
        return None
    version = session.scalar(
        select(DocumentVersion).where(
            DocumentVersion.tenant_id == tenant_id,
            DocumentVersion.id == version_id,
            DocumentVersion.document_id == document_id,
        )
    )
    return None if version is None else (document, version)


def page_report(
    session: Session, tenant_id: str, role: Role, document_id: UUID, version_id: UUID
) -> dict[str, Any] | None:
    found = _version(session, tenant_id, role, document_id, version_id)
    if found is None:
        return None
    _, version = found
    pages = session.scalars(
        select(DocumentPageExtraction)
        .where(
            DocumentPageExtraction.tenant_id == tenant_id,
            DocumentPageExtraction.document_version_id == version_id,
        )
        .order_by(DocumentPageExtraction.page_number)
    ).all()
    return {
        "document_id": str(document_id),
        "version_id": str(version_id),
        "ingestion_status": version.ingestion_status,
        "review_required": version.ingestion_status == "review_required"
        or any(page.classification in REVIEW_CLASSES for page in pages),
        "pages": [
            {
                "page_number": page.page_number,
                "classification": page.classification,
                "extraction_method": page.extraction_method,
                "quality_state": page.quality_state,
                "mean_confidence": page.mean_confidence,
                "char_count": page.char_count,
                "word_count": page.word_count,
                "flags": list(page.flags or []),
                "transformations": list(page.transformations or []),
                "width": page.width,
                "height": page.height,
                "unit": page.unit,
                "coordinate_space": page.coordinate_space,
                "engine": page.engine,
                "engine_version": page.engine_version,
                "config_version": page.config_version,
                "language": page.language,
                "schema_version": page.schema_version,
                "line_count": len(page.lines or []),
                "ocr_milliseconds": page.ocr_milliseconds,
            }
            for page in pages
        ],
    }


def chunk_provenance(
    session: Session,
    tenant_id: str,
    role: Role,
    document_id: UUID,
    version_id: UUID,
    chunk_index: int,
) -> dict[str, Any] | None:
    found = _version(session, tenant_id, role, document_id, version_id)
    if found is None:
        return None
    document, version = found
    source = session.scalar(
        select(DocumentChunkSource).where(
            DocumentChunkSource.tenant_id == tenant_id,
            DocumentChunkSource.document_version_id == version_id,
            DocumentChunkSource.chunk_index == chunk_index,
        )
    )
    if source is None:
        return None
    page = None
    if source.page_number is not None:
        page = session.scalar(
            select(DocumentPageExtraction).where(
                DocumentPageExtraction.tenant_id == tenant_id,
                DocumentPageExtraction.document_version_id == version_id,
                DocumentPageExtraction.page_number == source.page_number,
            )
        )
    return {
        "document_id": str(document_id),
        "version_id": str(version_id),
        "title": document.title,
        "source_name": version.filename,
        "chunk_id": source.chunk_id,
        "chunk_index": source.chunk_index,
        "page_number": source.page_number,
        "extraction_method": source.extraction_method,
        "coordinate_space": page.coordinate_space if page else None,
        "page_width": page.width if page else None,
        "page_height": page.height if page else None,
        "unit": page.unit if page else None,
        "regions": list(source.regions or []),
    }
