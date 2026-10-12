"""Persist the analysis of a document version and keep persons and claim groups current.

Runs in the ingestion worker's transaction right after the page extraction is recorded. Every
write is idempotent: facts and classifications are keyed, a fact the extractor no longer
produces is withdrawn (never deleted), person links and claim memberships decided by a reviewer
are never overwritten, and a rejected membership is never re-added. On PostgreSQL the
tenant's analyses are serialized by a transaction-scoped advisory lock, so concurrent workers
create every claim group and membership exactly once.
"""

from __future__ import annotations

import hashlib
from collections import defaultdict
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from ragops.intelligence.claims import ClaimKey, claim_keys, group_status
from ragops.intelligence.identity import debtor_status, propose_links
from ragops.intelligence.rules import (
    EXTRACTOR_VERSION,
    TAXONOMY_VERSION,
    Analysis,
    Fact,
    SourceLine,
    analyze,
    normalize_identifier,
    normalize_name,
)
from ragops.persistence.models import (
    ClaimGroup,
    ClaimGroupMember,
    Document,
    DocumentClassification,
    DocumentFact,
    DocumentIntelligenceRun,
    DocumentPersonLink,
    DocumentVersion,
    PersonAlias,
)

if TYPE_CHECKING:
    from ragops.workers.worker import ChunkBuild

USER_LINK_STATES = frozenset({"confirmed", "rejected"})
USER_MEMBER_STATES = frozenset({"confirmed", "rejected"})
USER_GROUP_STATES = frozenset({"confirmed", "dismissed"})


# --------------------------------------------------------------------------- source lines


def source_lines(built: ChunkBuild) -> list[SourceLine]:
    """Lines of every extracted page with method, quality, OCR box and chunk index.

    Native text keeps its own line breaks and gets no box; OCR lines keep their line index and
    normalized box. The chunk of a line is the first chunk whose word window contains the
    line's first word (pages needing review have no chunks).
    """
    from ragops.ingestion.normalization import normalize_text

    chunk_of: dict[tuple[int | None, int], int] = {}
    position = 0
    for chunk in built.chunks:
        number = chunk.page.page_number
        for word_index in range(chunk.start, chunk.end):
            chunk_of.setdefault((number, word_index), position)
        position += 1
    lines: list[SourceLine] = []
    seen_pages: set[int | None] = set()
    for page in built.extraction.pages:
        if page.page_number in seen_pages and page.page_number is not None:
            continue
        seen_pages.add(page.page_number)
        quality = page.quality.state
        native_text = page.text
        if page.lines:
            ocr_texts = [
                " ".join(word.text for word in page.words if word.line == line.index)
                for line in page.lines
            ]
            native_text = "\n".join(page.text.split("\n")[: -len(page.lines)])
        else:
            ocr_texts = []
        word = 0
        index = 0
        for raw in native_text.split("\n") if native_text else []:
            count = len(normalize_text(raw).split())
            if count:
                lines.append(
                    SourceLine(
                        page.page_number,
                        index,
                        raw.strip(),
                        "native_text",
                        "not_applicable" if quality == "not_applicable" else quality,
                        chunk_index=chunk_of.get((page.page_number, word)),
                    )
                )
                index += 1
            word += count
        for line, text_value in zip(page.lines, ocr_texts, strict=True):
            lines.append(
                SourceLine(
                    page.page_number,
                    index,
                    text_value,
                    "ocr",
                    quality,
                    ocr_line=line.index,
                    box=(line.box.x0, line.box.y0, line.box.x1, line.box.y1),
                    chunk_index=chunk_of.get((page.page_number, word)),
                )
            )
            index += 1
            word += line.word_count
    return lines


# --------------------------------------------------------------------------- persistence


def _lock_tenant(session: Session, tenant_id: str) -> None:
    if session.get_bind().dialect.name != "postgresql":
        return
    digest = hashlib.sha256(f"ragops-intelligence:{tenant_id}".encode()).digest()
    key = int.from_bytes(digest[:8], "big", signed=True)
    session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": key})


def _fact_row(tenant_id: str, version: DocumentVersion, fact: Fact) -> DocumentFact:
    line = fact.line
    review = "review_required" if fact.quality_state == "review_required" else "unreviewed"
    return DocumentFact(
        tenant_id=tenant_id,
        document_version_id=version.id,
        extractor_version=EXTRACTOR_VERSION,
        fact_key=fact.key(),
        category=fact.category,
        fact_type=fact.fact_type,
        assertion=fact.assertion,
        raw_value=fact.raw_value[:255],
        normalized_value=fact.normalized[:255] if fact.normalized else None,
        amount=fact.amount,
        currency=fact.currency,
        date_value=fact.date_value,
        as_of_date=fact.as_of,
        page_number=line.page_number,
        source_line=line.line_index,
        char_start=fact.start,
        char_end=fact.end,
        chunk_index=line.chunk_index,
        ocr_line=line.ocr_line,
        extraction_method=line.method,
        quality_state=fact.quality_state,
        regions=[{"line": line.ocr_line, "box": [round(v, 6) for v in line.box]}]
        if line.box is not None
        else [],
        status="active",
        review_state=review,
        review_version=1,
    )


def persist_analysis(
    session: Session, document: Document, version: DocumentVersion, analysis: Analysis
) -> DocumentIntelligenceRun:
    """Store one analysis idempotently, then refresh persons and claim groups of the tenant."""
    tenant = version.tenant_id
    _lock_tenant(session, tenant)
    now = datetime.now(UTC)
    run = session.scalar(
        select(DocumentIntelligenceRun).where(
            DocumentIntelligenceRun.tenant_id == tenant,
            DocumentIntelligenceRun.document_version_id == version.id,
        )
    )
    if run is None:
        run = DocumentIntelligenceRun(
            tenant_id=tenant, document_version_id=version.id, review_version=1
        )
        session.add(run)
    run.extractor_version, run.taxonomy_version = EXTRACTOR_VERSION, TAXONOMY_VERSION
    run.status, run.error_code = "completed", None
    run.classification_status = analysis.classification_status
    run.review_required = analysis.review_required
    run.fact_count = len(analysis.facts)
    run.ignored_instruction_lines = analysis.ignored_instruction_lines
    run.content_hash = version.content_hash
    run.analyzed_at = now
    run.flagged_for_review = bool(run.flagged_for_review)
    # Classification candidates: keyed per taxonomy version; reviewer state survives.
    existing = {
        row.document_class: row
        for row in session.scalars(
            select(DocumentClassification).where(
                DocumentClassification.tenant_id == tenant,
                DocumentClassification.document_version_id == version.id,
                DocumentClassification.taxonomy_version == TAXONOMY_VERSION,
            )
        )
    }
    for rank, candidate in enumerate(analysis.classifications, start=1):
        row = existing.get(candidate.document_class)
        if row is None:
            row = DocumentClassification(
                tenant_id=tenant,
                document_version_id=version.id,
                taxonomy_version=TAXONOMY_VERSION,
                document_class=candidate.document_class,
                extracted_at=now,
                review_state="review_required"
                if analysis.classification_status != "classified"
                else "unreviewed",
                review_version=1,
            )
            session.add(row)
        row.evidence_strength = candidate.strength
        row.rank = rank
        row.is_primary = candidate.document_class == analysis.primary_class
        row.evidence = [
            {"page": page, "line": line, "rule": rule} for page, line, rule in candidate.evidence
        ]
    # Facts: insert new keys, withdraw keys the extractor no longer produces.
    stored = {
        row.fact_key: row
        for row in session.scalars(
            select(DocumentFact).where(
                DocumentFact.tenant_id == tenant, DocumentFact.document_version_id == version.id
            )
        )
    }
    produced = {fact.key(): fact for fact in analysis.facts}
    for key in sorted(produced):
        if key in stored:
            stored[key].status = "active"
        else:
            session.add(_fact_row(tenant, version, produced[key]))
    for key, stale in stored.items():
        if key not in produced and stale.extractor_version == EXTRACTOR_VERSION:
            stale.status = "withdrawn"
    session.flush()
    attribute_persons(session, tenant, [version.id])
    regroup_claims(session, tenant, [version.id])
    session.flush()
    return run


def analyze_version(
    session: Session, document: Document, version: DocumentVersion, built: ChunkBuild
) -> DocumentIntelligenceRun:
    """Analyse one extracted version in a savepoint; a defect never fails the ingestion."""
    try:
        with session.begin_nested():
            return persist_analysis(session, document, version, analyze(source_lines(built)))
    except Exception:  # noqa: BLE001 - recorded as a failed analysis, visible to reviewers
        run = session.scalar(
            select(DocumentIntelligenceRun).where(
                DocumentIntelligenceRun.tenant_id == version.tenant_id,
                DocumentIntelligenceRun.document_version_id == version.id,
            )
        )
        if run is None:
            run = DocumentIntelligenceRun(
                tenant_id=version.tenant_id,
                document_version_id=version.id,
                classification_status="unknown",
                debtor_status="unknown",
                review_version=1,
                fact_count=0,
                ignored_instruction_lines=0,
                flagged_for_review=False,
            )
            session.add(run)
        run.extractor_version, run.taxonomy_version = EXTRACTOR_VERSION, TAXONOMY_VERSION
        run.status, run.error_code = "failed", "analysis_failed"
        run.review_required = True
        run.content_hash = version.content_hash
        run.analyzed_at = datetime.now(UTC)
        session.flush()
        return run


# --------------------------------------------------------------------------- persons


def _active_facts(
    session: Session, tenant_id: str, version_ids: Sequence[UUID]
) -> dict[UUID, list[DocumentFact]]:
    out: dict[UUID, list[DocumentFact]] = defaultdict(list)
    if not version_ids:
        return out
    for row in session.scalars(
        select(DocumentFact).where(
            DocumentFact.tenant_id == tenant_id,
            DocumentFact.document_version_id.in_(list(version_ids)),
            DocumentFact.status == "active",
        )
    ):
        out[row.document_version_id].append(row)
    return out


def _as_fact(row: DocumentFact) -> Fact:
    """The stored fact as the extractor's type (for identity and claim rules)."""
    effective = (
        row.corrected_text if row.review_state == "corrected" and row.corrected_text else None
    )
    normalized = row.normalized_value
    if effective is not None and row.category == "party":
        normalized = normalize_name(effective)
    elif effective is not None and row.category == "identifier":
        normalized = normalize_identifier(effective)
    quality = (
        "review_required"
        if row.review_state in {"review_required", "rejected"}
        else ("accepted" if row.review_state in {"confirmed", "corrected"} else row.quality_state)
    )
    line = SourceLine(
        row.page_number, row.source_line, row.raw_value, row.extraction_method, quality
    )
    return Fact(
        row.fact_type,
        row.assertion,
        row.raw_value,
        line,
        0,
        max(1, len(row.raw_value)),
        normalized=normalized,
        amount=row.amount,
        currency=row.currency,
        date_value=row.date_value,
        as_of=row.as_of_date,
    )


def _usable(rows: Iterable[DocumentFact]) -> list[Fact]:
    return [_as_fact(row) for row in rows if row.review_state != "rejected"]


def attribute_persons(
    session: Session, tenant_id: str, version_ids: Sequence[UUID] | None = None
) -> None:
    """Recompute system person links from stored facts and the tenant's aliases."""
    if version_ids is None:
        version_ids = list(
            session.scalars(
                select(DocumentIntelligenceRun.document_version_id).where(
                    DocumentIntelligenceRun.tenant_id == tenant_id
                )
            )
        )
    aliases = session.execute(
        select(PersonAlias.kind, PersonAlias.normalized_value, PersonAlias.person_id).where(
            PersonAlias.tenant_id == tenant_id
        )
    ).all()
    names = {value: str(person) for kind, value, person in aliases if kind == "name"}
    identifiers = {value: str(person) for kind, value, person in aliases if kind == "identifier"}
    facts = _active_facts(session, tenant_id, version_ids)
    for version_id in version_ids:
        rows = facts.get(version_id, [])
        proposals = {
            (UUID(link.person_id), link.role): link
            for link in propose_links(_usable(rows), names, identifiers)
        }
        existing = {
            (link.person_id, link.role): link
            for link in session.scalars(
                select(DocumentPersonLink).where(
                    DocumentPersonLink.tenant_id == tenant_id,
                    DocumentPersonLink.document_version_id == version_id,
                )
            )
        }
        for key, proposal in sorted(
            proposals.items(), key=lambda item: (str(item[0][0]), item[0][1])
        ):
            link = existing.get(key)
            if link is None:
                session.add(
                    DocumentPersonLink(
                        tenant_id=tenant_id,
                        document_version_id=version_id,
                        person_id=key[0],
                        role=key[1],
                        state=proposal.state,
                        origin="system",
                        basis=list(proposal.reasons),
                        review_version=1,
                    )
                )
            elif link.state not in USER_LINK_STATES:
                link.state, link.basis = proposal.state, list(proposal.reasons)
        for key, link in existing.items():
            if key not in proposals and link.state not in USER_LINK_STATES:
                link.state = "withdrawn"
        session.flush()
        refresh_debtor_status(session, tenant_id, version_id, rows)


def refresh_debtor_status(
    session: Session, tenant_id: str, version_id: UUID, rows: Sequence[DocumentFact] | None = None
) -> None:
    run = session.scalar(
        select(DocumentIntelligenceRun).where(
            DocumentIntelligenceRun.tenant_id == tenant_id,
            DocumentIntelligenceRun.document_version_id == version_id,
        )
    )
    if run is None:
        return
    if rows is None:
        rows = _active_facts(session, tenant_id, [version_id]).get(version_id, [])
    links = session.scalars(
        select(DocumentPersonLink).where(
            DocumentPersonLink.tenant_id == tenant_id,
            DocumentPersonLink.document_version_id == version_id,
            DocumentPersonLink.role == "debtor",
        )
    ).all()
    decided = any(link.state == "confirmed" for link in links)
    labelled = any(row.fact_type == "debtor" and row.review_state != "rejected" for row in rows)
    run.debtor_status = debtor_status(labelled, [link.state for link in links], decided=decided)


# --------------------------------------------------------------------------- claim groups


def _version_debtors(
    session: Session, tenant_id: str, version_ids: Iterable[UUID]
) -> dict[UUID, frozenset[str]]:
    out: dict[UUID, set[str]] = defaultdict(set)
    ids = list(version_ids)
    if not ids:
        return {}
    for version_id, person_id in session.execute(
        select(DocumentPersonLink.document_version_id, DocumentPersonLink.person_id).where(
            DocumentPersonLink.tenant_id == tenant_id,
            DocumentPersonLink.document_version_id.in_(ids),
            DocumentPersonLink.role == "debtor",
            DocumentPersonLink.state.in_(("evidenced", "confirmed")),
        )
    ):
        out[version_id].add(str(person_id))
    return {key: frozenset(value) for key, value in out.items()}


def regroup_claims(session: Session, tenant_id: str, version_ids: Sequence[UUID]) -> None:
    """Join each version to the groups of its strong claim keys; refresh touched groups."""
    facts = _active_facts(session, tenant_id, version_ids)
    touched: set[UUID] = set()
    now = datetime.now(UTC)
    for version_id in version_ids:
        primary = session.scalar(
            select(DocumentClassification.document_class).where(
                DocumentClassification.tenant_id == tenant_id,
                DocumentClassification.document_version_id == version_id,
                DocumentClassification.is_primary.is_(True),
                DocumentClassification.taxonomy_version == TAXONOMY_VERSION,
            )
        )
        keys: list[ClaimKey] = claim_keys(_usable(facts.get(version_id, [])), primary)
        wanted: dict[UUID, list[str]] = {}
        for key in sorted(keys, key=lambda item: item.digest):
            group = session.scalar(
                select(ClaimGroup).where(
                    ClaimGroup.tenant_id == tenant_id, ClaimGroup.group_key == key.digest
                )
            )
            if group is None:
                group = ClaimGroup(
                    tenant_id=tenant_id,
                    group_key=key.digest,
                    creditor_normalized=key.creditor[:255],
                    identifier_type=key.identifier_type,
                    identifier_normalized=key.identifier[:120],
                    status="system_linked",
                    basis=[],
                    review_version=1,
                    updated_at=now,
                )
                session.add(group)
                session.flush()
            basis = [
                "creditor_match" if key.creditor else "creditor_unknown",
                f"{key.identifier_type}_match",
            ]
            if len(keys) > 1:
                basis.append("multiple_claim_identifiers")
            wanted[group.id] = basis
        members = {
            member.claim_group_id: member
            for member in session.scalars(
                select(ClaimGroupMember).where(
                    ClaimGroupMember.tenant_id == tenant_id,
                    ClaimGroupMember.document_version_id == version_id,
                )
            )
        }
        for group_id, basis in wanted.items():
            state = "proposed" if "multiple_claim_identifiers" in basis else "linked"
            member = members.get(group_id)
            if member is None:
                confirmed_group = (
                    session.scalar(
                        select(ClaimGroup.status).where(
                            ClaimGroup.tenant_id == tenant_id, ClaimGroup.id == group_id
                        )
                    )
                    in USER_GROUP_STATES
                )
                session.add(
                    ClaimGroupMember(
                        tenant_id=tenant_id,
                        claim_group_id=group_id,
                        document_version_id=version_id,
                        # A new document never silently joins a group a reviewer decided.
                        state="proposed" if confirmed_group else state,
                        origin="system",
                        basis=basis,
                        review_version=1,
                    )
                )
            elif member.state not in USER_MEMBER_STATES:
                member.state, member.basis = state, basis
            touched.add(group_id)
        for group_id, member in members.items():
            if group_id not in wanted and member.state not in USER_MEMBER_STATES:
                member.state, member.basis = "proposed", ["claim_key_no_longer_extracted"]
                touched.add(group_id)
    session.flush()
    refresh_groups(session, tenant_id, touched)


def refresh_groups(session: Session, tenant_id: str, group_ids: Iterable[UUID]) -> None:
    for group_id in sorted(group_ids, key=str):
        group = session.scalar(
            select(ClaimGroup).where(ClaimGroup.tenant_id == tenant_id, ClaimGroup.id == group_id)
        )
        if group is None or group.status in USER_GROUP_STATES:
            continue
        member_versions = list(
            session.scalars(
                select(ClaimGroupMember.document_version_id).where(
                    ClaimGroupMember.tenant_id == tenant_id,
                    ClaimGroupMember.claim_group_id == group_id,
                    ClaimGroupMember.state.in_(("linked", "confirmed", "proposed")),
                )
            )
        )
        debtors = _version_debtors(session, tenant_id, member_versions)
        status, basis = group_status(
            group.creditor_normalized,
            [debtors.get(version, frozenset()) for version in member_versions],
        )
        group.status, group.basis, group.updated_at = status, basis, datetime.now(UTC)


def regroup_tenant(session: Session, tenant_id: str) -> None:
    """After a person change: refresh links, then the status of every group of the tenant."""
    attribute_persons(session, tenant_id)
    refresh_groups(
        session,
        tenant_id,
        list(session.scalars(select(ClaimGroup.id).where(ClaimGroup.tenant_id == tenant_id))),
    )


def snapshot(row: Any, fields: Sequence[str]) -> dict[str, Any]:
    """JSON-safe copy of selected columns for review events."""
    out: dict[str, Any] = {}
    for name in fields:
        value = getattr(row, name)
        out[name] = value if value is None or isinstance(value, bool | int | str) else str(value)
    return out
