"""Structured, deterministic answers from PostgreSQL and the reviewer workflow.

Every read is scoped to the caller's tenant, to documents at or below the caller's access level
and to each document's current version; anything else does not exist for the caller. Reviews
need the ``admin`` or ``compliance`` role, name the entity version they saw and are applied with
a conditional update: a stale or concurrent edit is a :class:`Conflict`. Source facts are never
changed by a review; every decision is an append-only event.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ragops.auth.rbac import ACCESS_RANK, ROLE_MAX_ACCESS, can_access_level
from ragops.intelligence.claims import AmountView, GroupView, MemberView, summarize
from ragops.intelligence.rules import DOCUMENT_CLASSES, normalize_identifier, normalize_name
from ragops.intelligence.service import (
    attribute_persons,
    refresh_debtor_status,
    refresh_groups,
    regroup_claims,
    regroup_tenant,
    snapshot,
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
    IntelligenceReviewEvent,
    PersonAlias,
    PersonProfile,
)
from ragops.storage.models import Role

REVIEW_ROLES = frozenset({"admin", "compliance"})
REASON_CATEGORIES = frozenset(
    {
        "verified_against_source",
        "extraction_error",
        "wrong_party",
        "not_same_claim",
        "duplicate",
        "not_a_claim",
        "needs_follow_up",
        "other",
    }
)
DEADLINE_TYPES = ("due_date", "payment_deadline")
MAX_LIST = 500


class IntelligenceError(Exception):
    pass


class NotFound(IntelligenceError):
    pass


class Forbidden(IntelligenceError):
    pass


class Conflict(IntelligenceError):
    pass


class InvalidReview(IntelligenceError):
    pass


def _iso(value: date | None) -> str | None:
    return value.isoformat() if value else None


def _money(value: Decimal | None) -> str | None:
    return None if value is None else f"{value.quantize(Decimal('0.01'))}"


# --------------------------------------------------------------------------- reads


class IntelligenceReader:
    def __init__(self, session: Session, tenant_id: str, role: Role) -> None:
        if not tenant_id or role not in ROLE_MAX_ACCESS:
            raise Forbidden("tenant and role are required")
        self.session, self.tenant, self.role = session, tenant_id, role

    # -- visibility
    def _visible_levels(self) -> list[str]:
        return [level for level in ACCESS_RANK if can_access_level(self.role, level)]

    def _visible(self) -> dict[UUID, tuple[Document, DocumentVersion]]:
        """Current versions of the tenant's active documents the caller may see."""
        rows = self.session.execute(
            select(Document, DocumentVersion)
            .join(
                DocumentVersion,
                (DocumentVersion.tenant_id == Document.tenant_id)
                & (DocumentVersion.id == Document.current_version_id),
            )
            .where(
                Document.tenant_id == self.tenant,
                Document.active.is_(True),
                Document.access_level.in_(self._visible_levels()),
            )
        ).all()
        return {version.id: (document, version) for document, version in rows}

    def _document(self, document_id: UUID) -> tuple[Document, DocumentVersion]:
        for document, version in self._visible().values():
            if document.id == document_id:
                return document, version
        raise NotFound("document not found")

    def _runs(self, version_ids: Iterable[UUID]) -> dict[UUID, DocumentIntelligenceRun]:
        ids = list(version_ids)
        if not ids:
            return {}
        return {
            run.document_version_id: run
            for run in self.session.scalars(
                select(DocumentIntelligenceRun).where(
                    DocumentIntelligenceRun.tenant_id == self.tenant,
                    DocumentIntelligenceRun.document_version_id.in_(ids),
                )
            )
        }

    def _primary(self, version_ids: Iterable[UUID]) -> dict[UUID, str]:
        ids = list(version_ids)
        if not ids:
            return {}
        out: dict[UUID, str] = {}
        for row in self.session.scalars(
            select(DocumentClassification).where(
                DocumentClassification.tenant_id == self.tenant,
                DocumentClassification.document_version_id.in_(ids),
            )
        ):
            if row.review_state == "confirmed":
                out[row.document_version_id] = row.document_class
            elif row.is_primary and row.review_state != "rejected":
                out.setdefault(row.document_version_id, row.document_class)
        return out

    def _facts(
        self, version_ids: Iterable[UUID], types: Sequence[str] | None = None
    ) -> list[DocumentFact]:
        ids = list(version_ids)
        if not ids:
            return []
        query = select(DocumentFact).where(
            DocumentFact.tenant_id == self.tenant,
            DocumentFact.document_version_id.in_(ids),
            DocumentFact.status == "active",
        )
        if types:
            query = query.where(DocumentFact.fact_type.in_(list(types)))
        return list(
            self.session.scalars(
                query.order_by(
                    DocumentFact.page_number, DocumentFact.source_line, DocumentFact.char_start
                )
            )
        )

    def _person_versions(self, person_id: str, role: str = "debtor") -> set[UUID]:
        try:
            person = UUID(person_id)
        except ValueError:
            raise NotFound("person not found") from None
        return set(
            self.session.scalars(
                select(DocumentPersonLink.document_version_id).where(
                    DocumentPersonLink.tenant_id == self.tenant,
                    DocumentPersonLink.person_id == person,
                    DocumentPersonLink.role == role,
                    DocumentPersonLink.state.in_(("evidenced", "confirmed")),
                )
            )
        )

    # -- views
    @staticmethod
    def effective(fact: DocumentFact) -> dict[str, Any]:
        if fact.review_state == "corrected":
            return {
                "origin": "user",
                "text": fact.corrected_text,
                "amount": _money(fact.corrected_amount),
                "currency": fact.corrected_currency,
                "date": _iso(fact.corrected_date),
            }
        if fact.review_state == "rejected":
            return {"origin": "rejected"}
        return {
            "origin": "document" if fact.assertion == "document_stated" else "rule",
            "text": fact.normalized_value,
            "amount": _money(fact.amount),
            "currency": fact.currency,
            "date": _iso(fact.date_value),
        }

    def fact_view(self, fact: DocumentFact, *, source: bool = False) -> dict[str, Any]:
        view: dict[str, Any] = {
            "id": str(fact.id),
            "document_version_id": str(fact.document_version_id),
            "fact_type": fact.fact_type,
            "category": fact.category,
            "assertion": fact.assertion,
            "value": fact.normalized_value,
            "amount": _money(fact.amount),
            "currency": fact.currency,
            "date": _iso(fact.date_value),
            "as_of": _iso(fact.as_of_date),
            "page_number": fact.page_number,
            "quality_state": fact.quality_state,
            "review_state": fact.review_state,
            "review_version": fact.review_version,
            "extractor_version": fact.extractor_version,
            "effective": self.effective(fact),
        }
        if source:
            view["source"] = {
                "document_version_id": str(fact.document_version_id),
                "page_number": fact.page_number,
                "line": fact.source_line,
                "char_start": fact.char_start,
                "char_end": fact.char_end,
                "raw_value": fact.raw_value,
                "chunk_index": fact.chunk_index,
                "extraction_method": fact.extraction_method,
                "ocr_line": fact.ocr_line,
                "regions": list(fact.regions or []),
                "coordinate_space": "page-normalized-v1" if fact.regions else None,
            }
        return view

    def _summary(
        self,
        document: Document,
        version: DocumentVersion,
        run: DocumentIntelligenceRun | None,
        primary: str | None,
    ) -> dict[str, Any]:
        return {
            "document_id": str(document.id),
            "version_id": str(version.id),
            "title": document.title,
            "classification": primary,
            "classification_status": run.classification_status if run else "not_analyzed",
            "debtor_status": run.debtor_status if run else "unknown",
            "review_required": bool(run and (run.review_required or run.flagged_for_review)),
            "analysis_status": run.status if run else "not_analyzed",
            "review_version": run.review_version if run else None,
        }

    def documents(
        self,
        classification: str | None = None,
        person_id: str | None = None,
        review_required: bool | None = None,
    ) -> list[dict[str, Any]]:
        if classification is not None and classification not in DOCUMENT_CLASSES:
            raise InvalidReview("unknown classification")
        visible = self._visible()
        ids = set(visible)
        if person_id is not None:
            ids &= self._person_versions(person_id)
        runs, primary = self._runs(ids), self._primary(ids)
        out = []
        for version_id in ids:
            document, version = visible[version_id]
            item = self._summary(document, version, runs.get(version_id), primary.get(version_id))
            if classification is not None and item["classification"] != classification:
                continue
            if review_required is not None and item["review_required"] != review_required:
                continue
            out.append(item)
        return sorted(out, key=lambda item: (item["title"], item["document_id"]))[:MAX_LIST]

    def document(self, document_id: str) -> dict[str, Any]:
        try:
            identifier = UUID(document_id)
        except ValueError:
            raise NotFound("document not found") from None
        document, version = self._document(identifier)
        run = self._runs([version.id]).get(version.id)
        primary = self._primary([version.id]).get(version.id)
        classifications = self.session.scalars(
            select(DocumentClassification)
            .where(
                DocumentClassification.tenant_id == self.tenant,
                DocumentClassification.document_version_id == version.id,
            )
            .order_by(DocumentClassification.rank)
        ).all()
        links = self.session.scalars(
            select(DocumentPersonLink).where(
                DocumentPersonLink.tenant_id == self.tenant,
                DocumentPersonLink.document_version_id == version.id,
            )
        ).all()
        members = self.session.execute(
            select(ClaimGroupMember, ClaimGroup)
            .join(
                ClaimGroup,
                (ClaimGroup.tenant_id == ClaimGroupMember.tenant_id)
                & (ClaimGroup.id == ClaimGroupMember.claim_group_id),
            )
            .where(
                ClaimGroupMember.tenant_id == self.tenant,
                ClaimGroupMember.document_version_id == version.id,
            )
        ).all()
        return {
            **self._summary(document, version, run, primary),
            "ignored_instruction_lines": run.ignored_instruction_lines if run else 0,
            "extractor_version": run.extractor_version if run else None,
            "classifications": [
                {
                    "id": str(row.id),
                    "document_class": row.document_class,
                    "evidence_strength": row.evidence_strength,
                    "rank": row.rank,
                    "is_primary": row.is_primary,
                    "evidence": list(row.evidence or []),
                    "taxonomy_version": row.taxonomy_version,
                    "review_state": row.review_state,
                    "review_version": row.review_version,
                }
                for row in classifications
            ],
            "facts": [self.fact_view(fact) for fact in self._facts([version.id])],
            "persons": [
                {
                    "id": str(link.id),
                    "person_id": str(link.person_id),
                    "role": link.role,
                    "state": link.state,
                    "origin": link.origin,
                    "basis": list(link.basis or []),
                    "review_version": link.review_version,
                }
                for link in links
                if link.state != "withdrawn"
            ],
            "claim_groups": [
                {
                    "claim_group_id": str(group.id),
                    "member_id": str(member.id),
                    "membership": member.state,
                    "status": group.status,
                    "basis": list(member.basis or []),
                }
                for member, group in members
            ],
        }

    def fact(self, fact_id: str) -> dict[str, Any]:
        try:
            identifier = UUID(fact_id)
        except ValueError:
            raise NotFound("fact not found") from None
        fact = self.session.scalar(
            select(DocumentFact).where(
                DocumentFact.tenant_id == self.tenant, DocumentFact.id == identifier
            )
        )
        visible = self._visible()
        if fact is None or fact.document_version_id not in visible:
            raise NotFound("fact not found")
        document, _version = visible[fact.document_version_id]
        view = self.fact_view(fact, source=True)
        view["source"]["document_id"] = str(document.id)
        view["source"]["title"] = document.title
        return view

    def creditors(self) -> list[dict[str, Any]]:
        visible = self._visible()
        found: dict[str, dict[str, Any]] = {}
        for fact in self._facts(visible, ("creditor", "original_creditor", "current_claimant")):
            if fact.review_state == "rejected" or not fact.normalized_value:
                continue
            name = fact.normalized_value
            if fact.review_state == "corrected" and fact.corrected_text:
                name = normalize_name(fact.corrected_text)
            entry = found.setdefault(name, {"creditor": name, "documents": [], "roles": set()})
            document, _ = visible[fact.document_version_id]
            entry["roles"].add(fact.fact_type)
            reference = {
                "document_id": str(document.id),
                "fact_id": str(fact.id),
                "assertion": fact.assertion,
            }
            if reference not in entry["documents"]:
                entry["documents"].append(reference)
        return [
            {**entry, "roles": sorted(entry["roles"])}
            for entry in sorted(found.values(), key=lambda item: item["creditor"])
        ]

    def deadlines(self, person_id: str | None = None) -> list[dict[str, Any]]:
        visible = self._visible()
        ids = set(visible)
        if person_id is not None:
            ids &= self._person_versions(person_id)
        out = []
        for fact in self._facts(ids, DEADLINE_TYPES):
            if fact.review_state == "rejected":
                continue
            value = fact.corrected_date if fact.review_state == "corrected" else fact.date_value
            document, _ = visible[fact.document_version_id]
            out.append(
                {
                    "fact_id": str(fact.id),
                    "document_id": str(document.id),
                    "title": document.title,
                    "fact_type": fact.fact_type,
                    "date": _iso(value),
                    "page_number": fact.page_number,
                    "review_state": fact.review_state,
                    "quality_state": fact.quality_state,
                }
            )
        return sorted(out, key=lambda item: (item["date"] or "", item["title"]))

    def findings(self) -> list[dict[str, Any]]:
        """Everything that still needs a human: facts, classifications, persons, groups."""
        visible = self._visible()
        out: list[dict[str, Any]] = []
        for fact in self._facts(visible):
            if fact.review_state == "review_required":
                document, _ = visible[fact.document_version_id]
                out.append(
                    {
                        "kind": "fact",
                        "id": str(fact.id),
                        "document_id": str(document.id),
                        "fact_type": fact.fact_type,
                        "reason": "low_quality_source"
                        if fact.quality_state == "review_required"
                        else "review_required",
                    }
                )
        runs = self._runs(visible)
        for version_id, run in runs.items():
            document, _ = visible[version_id]
            if run.classification_status != "classified":
                out.append(
                    {
                        "kind": "classification",
                        "id": str(run.id),
                        "document_id": str(document.id),
                        "reason": run.classification_status,
                    }
                )
            if run.debtor_status == "ambiguous":
                out.append(
                    {
                        "kind": "debtor",
                        "id": str(run.id),
                        "document_id": str(document.id),
                        "reason": "ambiguous_debtor",
                    }
                )
            if run.flagged_for_review:
                out.append(
                    {
                        "kind": "document",
                        "id": str(run.id),
                        "document_id": str(document.id),
                        "reason": "flagged_by_reviewer",
                    }
                )
            if run.status == "failed":
                out.append(
                    {
                        "kind": "analysis",
                        "id": str(run.id),
                        "document_id": str(document.id),
                        "reason": "analysis_failed",
                    }
                )
        for group in self._groups(visible):
            if group.status not in {"system_linked", "confirmed", "dismissed"}:
                out.append({"kind": "claim_group", "id": str(group.id), "reason": group.status})
        return out[:MAX_LIST]

    def persons(self) -> list[dict[str, Any]]:
        profiles = self.session.scalars(
            select(PersonProfile)
            .where(PersonProfile.tenant_id == self.tenant)
            .order_by(PersonProfile.label)
        ).all()
        counts: dict[UUID, int] = defaultdict(int)
        for person_id, _version in self.session.execute(
            select(PersonAlias.person_id, PersonAlias.id).where(
                PersonAlias.tenant_id == self.tenant
            )
        ):
            counts[person_id] += 1
        # Alias values are not listed: they are personal data and only needed for matching.
        return [
            {"id": str(p.id), "label": p.label, "status": p.status, "alias_count": counts[p.id]}
            for p in profiles
        ]

    def _groups(self, visible: Mapping[UUID, Any]) -> list[ClaimGroup]:
        if not visible:
            return []
        group_ids = set(
            self.session.scalars(
                select(ClaimGroupMember.claim_group_id).where(
                    ClaimGroupMember.tenant_id == self.tenant,
                    ClaimGroupMember.document_version_id.in_(list(visible)),
                )
            )
        )
        if not group_ids:
            return []
        return list(
            self.session.scalars(
                select(ClaimGroup).where(
                    ClaimGroup.tenant_id == self.tenant, ClaimGroup.id.in_(list(group_ids))
                )
            )
        )

    def claims(self, person_id: str | None = None) -> dict[str, Any]:
        visible = self._visible()
        scope = set(visible)
        if person_id is not None:
            scope &= self._person_versions(person_id)
        runs, primary = self._runs(scope), self._primary(scope)
        members = (
            self.session.execute(
                select(ClaimGroupMember).where(
                    ClaimGroupMember.tenant_id == self.tenant,
                    ClaimGroupMember.document_version_id.in_(list(scope) or [UUID(int=0)]),
                )
            )
            .scalars()
            .all()
        )
        groups = {
            group.id: group
            for group in self.session.scalars(
                select(ClaimGroup).where(
                    ClaimGroup.tenant_id == self.tenant,
                    ClaimGroup.id.in_([m.claim_group_id for m in members] or [UUID(int=0)]),
                )
            )
        }
        facts = self._facts(scope)
        by_version: dict[UUID, list[DocumentFact]] = defaultdict(list)
        for fact in facts:
            by_version[fact.document_version_id].append(fact)
        views: dict[UUID, GroupView] = {}
        active_versions: set[UUID] = set()
        rejected_versions: set[UUID] = set()
        for member in members:
            group = groups[member.claim_group_id]
            if group.status == "dismissed":
                rejected_versions.add(member.document_version_id)
                continue
            document, version = visible[member.document_version_id]
            rows = by_version.get(version.id, [])
            if member.state == "rejected":
                rejected_versions.add(version.id)
            else:
                active_versions.add(version.id)
            issue = next(
                (
                    (f.corrected_date if f.review_state == "corrected" else f.date_value)
                    for f in rows
                    if f.fact_type == "issue_date" and f.review_state != "rejected"
                ),
                None,
            )
            amounts = (
                [
                    AmountView(
                        str(f.id),
                        f.fact_type,
                        f.corrected_amount
                        if f.review_state == "corrected" and f.corrected_amount is not None
                        else f.amount or Decimal("0"),
                        f.corrected_currency
                        if f.review_state == "corrected" and f.corrected_currency
                        else f.currency,
                        f.as_of_date,
                        f.review_state,
                        f.quality_state,
                    )
                    for f in rows
                    if f.category == "amount" and f.review_state != "rejected"
                ]
                if member.state != "rejected"
                else []
            )
            view = views.setdefault(
                group.id,
                GroupView(
                    str(group.id),
                    group.status,
                    group.creditor_normalized,
                    group.identifier_type,
                    group.identifier_normalized,
                    group.review_version,
                    [],
                ),
            )
            view.members.append(
                MemberView(
                    str(member.id),
                    str(document.id),
                    str(version.id),
                    document.title,
                    member.state,
                    member.review_version,
                    primary.get(version.id),
                    issue,
                    any(f.fact_type == "settled" and f.review_state != "rejected" for f in rows),
                    amounts,
                )
            )
        ungrouped: list[tuple[str, str, str]] = []
        for version_id in sorted(scope, key=str):
            if version_id in active_versions:
                continue
            rows = by_version.get(version_id, [])
            if not any(f.category == "amount" and f.review_state != "rejected" for f in rows):
                continue
            if primary.get(version_id) in {"unrelated_document"}:
                continue
            document, _ = visible[version_id]
            reason = (
                "membership_rejected" if version_id in rejected_versions else "no_claim_identifier"
            )
            ungrouped.append((str(document.id), document.title, reason))
        del runs
        result = summarize(
            sorted(views.values(), key=lambda g: (g.creditor, g.identifier)), ungrouped
        )
        result["person_id"] = person_id
        return result


# --------------------------------------------------------------------------- reviews


class IntelligenceReviewer:
    """Reviewer decisions; every successful call appends exactly one review event."""

    def __init__(self, session: Session, tenant_id: str, actor: str, roles: Sequence[str]) -> None:
        if not tenant_id or not actor:
            raise Forbidden("tenant and actor are required")
        if not REVIEW_ROLES.intersection(roles):
            raise Forbidden("review role required")
        role = "admin" if "admin" in roles else "compliance"
        self.session, self.tenant, self.actor = session, tenant_id, actor
        self.reader = IntelligenceReader(session, tenant_id, role)  # type: ignore[arg-type]

    # -- persons
    def create_person(self, label: str) -> dict[str, Any]:
        label = " ".join(label.split())
        if not 1 <= len(label) <= 120:
            raise InvalidReview("label must be 1-120 characters")
        exists = self.session.scalar(
            select(PersonProfile.id).where(
                PersonProfile.tenant_id == self.tenant, PersonProfile.label == label
            )
        )
        if exists is not None:
            raise Conflict("person label already exists")
        person = PersonProfile(
            tenant_id=self.tenant,
            label=label,
            status="active",
            created_by=self.actor,
            review_version=1,
        )
        self.session.add(person)
        self.session.flush()
        return {"id": str(person.id), "label": person.label, "status": person.status}

    def add_alias(self, person_id: str, kind: str, value: str) -> dict[str, Any]:
        if kind not in {"name", "identifier"}:
            raise InvalidReview("alias kind must be name or identifier")
        try:
            identifier = UUID(person_id)
        except ValueError:
            raise NotFound("person not found") from None
        person = self.session.scalar(
            select(PersonProfile).where(
                PersonProfile.tenant_id == self.tenant, PersonProfile.id == identifier
            )
        )
        if person is None:
            raise NotFound("person not found")
        normalized = normalize_name(value) if kind == "name" else normalize_identifier(value)
        if not 2 <= len(normalized) <= 255:
            raise InvalidReview("alias must be 2-255 characters")
        owner = self.session.scalar(
            select(PersonAlias.person_id).where(
                PersonAlias.tenant_id == self.tenant,
                PersonAlias.kind == kind,
                PersonAlias.normalized_value == normalized,
            )
        )
        if owner is not None:
            # One alias never identifies two people; the same alias twice is also refused.
            raise Conflict("alias already assigned")
        alias = PersonAlias(
            tenant_id=self.tenant,
            person_id=person.id,
            kind=kind,
            normalized_value=normalized,
            created_by=self.actor,
        )
        self.session.add(alias)
        try:
            self.session.flush()
        except IntegrityError:
            raise Conflict("alias already assigned") from None
        regroup_tenant(self.session, self.tenant)
        return {"id": str(alias.id), "person_id": str(person.id), "kind": kind}

    # -- reviews
    def _event(
        self,
        entity_type: str,
        entity_id: UUID,
        version: int,
        document_version: UUID | None,
        action: str,
        reason: str,
        before: dict[str, Any],
        after: dict[str, Any],
    ) -> None:
        self.session.add(
            IntelligenceReviewEvent(
                tenant_id=self.tenant,
                entity_type=entity_type,
                entity_id=entity_id,
                entity_version=version,
                document_version_id=document_version,
                action=action,
                reason_category=reason,
                previous_state=before,
                new_state=after,
                actor_subject=self.actor,
            )
        )
        try:
            self.session.flush()
        except IntegrityError:
            raise Conflict("entity changed concurrently") from None

    def _bump(self, model: Any, entity_id: UUID, expected: int, values: dict[str, Any]) -> None:
        """Apply the change only if nobody changed the entity since ``expected``."""
        result = self.session.execute(
            update(model)
            .where(
                model.tenant_id == self.tenant,
                model.id == entity_id,
                model.review_version == expected,
            )
            .values(**values, review_version=expected + 1)
            .execution_options(synchronize_session=False)
        )
        if result.rowcount != 1:  # type: ignore[attr-defined]
            raise Conflict("stale review version")

    def _visible_version(self, version_id: UUID) -> None:
        if version_id not in self.reader._visible():
            raise NotFound("entity not found")

    def review(
        self,
        entity_type: str,
        entity_id: str,
        action: str,
        expected_version: int,
        reason_category: str,
        *,
        value: Mapping[str, Any] | None = None,
        person_id: str | None = None,
        role: str = "debtor",
    ) -> dict[str, Any]:
        if reason_category not in REASON_CATEGORIES:
            raise InvalidReview("unknown reason category")
        if expected_version < 1:
            raise InvalidReview("expected_version must be positive")
        try:
            identifier = UUID(entity_id)
        except ValueError:
            raise NotFound("entity not found") from None
        handler = {
            "fact": self._review_fact,
            "classification": self._review_classification,
            "person_link": self._review_link,
            "claim_group": self._review_group,
            "claim_member": self._review_member,
            "document": self._review_document,
        }.get(entity_type)
        if handler is None:
            raise InvalidReview("unknown entity type")
        return handler(
            identifier, action, expected_version, reason_category, value, person_id, role
        )

    def _review_fact(
        self,
        identifier: UUID,
        action: str,
        expected: int,
        reason: str,
        value: Mapping[str, Any] | None,
        _person: str | None,
        _role: str,
    ) -> dict[str, Any]:
        fact = self.session.scalar(
            select(DocumentFact).where(
                DocumentFact.tenant_id == self.tenant, DocumentFact.id == identifier
            )
        )
        if fact is None:
            raise NotFound("entity not found")
        self._visible_version(fact.document_version_id)
        fields = (
            "review_state",
            "corrected_text",
            "corrected_amount",
            "corrected_currency",
            "corrected_date",
        )
        before = snapshot(fact, fields)
        if action == "confirm":
            changes: dict[str, Any] = {"review_state": "confirmed"}
        elif action == "reject":
            changes = {"review_state": "rejected"}
        elif action == "mark_for_review":
            changes = {"review_state": "review_required"}
        elif action == "correct":
            changes = {"review_state": "corrected", **self._correction(fact, value or {})}
        else:
            raise InvalidReview("unsupported action for a fact")
        self._bump(DocumentFact, fact.id, expected, changes)
        after = {**before, **{k: (None if v is None else str(v)) for k, v in changes.items()}}
        self._event(
            "fact", fact.id, expected + 1, fact.document_version_id, action, reason, before, after
        )
        self.session.expire(fact)
        # A corrected or rejected party or identifier can change persons and claim keys.
        attribute_persons(self.session, self.tenant, [fact.document_version_id])
        regroup_claims(self.session, self.tenant, [fact.document_version_id])
        return {
            "id": str(fact.id),
            "review_state": changes["review_state"],
            "review_version": expected + 1,
        }

    @staticmethod
    def _correction(fact: DocumentFact, value: Mapping[str, Any]) -> dict[str, Any]:
        """A typed user value; it is stored beside the source fact, never over it."""
        if fact.category == "amount":
            try:
                amount = Decimal(str(value.get("amount")))
            except (InvalidOperation, TypeError):
                raise InvalidReview("amount must be a decimal") from None
            currency = str(value.get("currency", ""))
            if (
                not amount.is_finite()
                or amount < 0
                or amount > Decimal("1e15")
                or amount != amount.quantize(Decimal("0.01"))
            ):
                raise InvalidReview("amount must be a non-negative decimal with two places")
            if len(currency) != 3 or not currency.isupper() or not currency.isalpha():
                raise InvalidReview("currency must be an ISO 4217 code")
            return {"corrected_amount": amount, "corrected_currency": currency}
        if fact.category == "date":
            try:
                parsed = date.fromisoformat(str(value.get("date")))
            except ValueError:
                raise InvalidReview("date must be ISO 8601") from None
            return {"corrected_date": parsed}
        if fact.category in {"party", "identifier"}:
            corrected = " ".join(str(value.get("text", "")).split())
            if not 1 <= len(corrected) <= 255:
                raise InvalidReview("text must be 1-255 characters")
            return {"corrected_text": corrected}
        raise InvalidReview("status indicators can be confirmed or rejected, not corrected")

    def _review_classification(
        self,
        identifier: UUID,
        action: str,
        expected: int,
        reason: str,
        _value: Mapping[str, Any] | None,
        _person: str | None,
        _role: str,
    ) -> dict[str, Any]:
        row = self.session.scalar(
            select(DocumentClassification).where(
                DocumentClassification.tenant_id == self.tenant,
                DocumentClassification.id == identifier,
            )
        )
        if row is None:
            raise NotFound("entity not found")
        self._visible_version(row.document_version_id)
        states = {
            "confirm": "confirmed",
            "reject": "rejected",
            "mark_for_review": "review_required",
        }
        if action not in states:
            raise InvalidReview("unsupported action for a classification")
        before = snapshot(row, ("review_state", "document_class"))
        if action == "confirm":
            # Exactly one confirmed class per version: confirming one supersedes the others.
            others = self.session.scalars(
                select(DocumentClassification).where(
                    DocumentClassification.tenant_id == self.tenant,
                    DocumentClassification.document_version_id == row.document_version_id,
                    DocumentClassification.id != row.id,
                    DocumentClassification.review_state == "confirmed",
                )
            )
            for other in others:
                other.review_state = "unreviewed"
        self._bump(DocumentClassification, row.id, expected, {"review_state": states[action]})
        self._event(
            "classification",
            row.id,
            expected + 1,
            row.document_version_id,
            action,
            reason,
            before,
            {**before, "review_state": states[action]},
        )
        return {"id": str(row.id), "review_state": states[action], "review_version": expected + 1}

    def _review_link(
        self,
        identifier: UUID,
        action: str,
        expected: int,
        reason: str,
        _value: Mapping[str, Any] | None,
        _person: str | None,
        _role: str,
    ) -> dict[str, Any]:
        link = self.session.scalar(
            select(DocumentPersonLink).where(
                DocumentPersonLink.tenant_id == self.tenant, DocumentPersonLink.id == identifier
            )
        )
        if link is None:
            raise NotFound("entity not found")
        self._visible_version(link.document_version_id)
        states = {"confirm": "confirmed", "reject": "rejected"}
        if action not in states:
            raise InvalidReview("unsupported action for a person link")
        before = snapshot(link, ("state", "role", "origin"))
        self._bump(DocumentPersonLink, link.id, expected, {"state": states[action]})
        self._event(
            "person_link",
            link.id,
            expected + 1,
            link.document_version_id,
            action,
            reason,
            before,
            {**before, "state": states[action]},
        )
        self.session.expire(link)
        self._after_person_change(link.document_version_id)
        return {"id": str(link.id), "state": states[action], "review_version": expected + 1}

    def _after_person_change(self, version_id: UUID) -> None:
        self.session.flush()
        refresh_debtor_status(self.session, self.tenant, version_id)
        groups = list(
            self.session.scalars(
                select(ClaimGroupMember.claim_group_id).where(
                    ClaimGroupMember.tenant_id == self.tenant,
                    ClaimGroupMember.document_version_id == version_id,
                )
            )
        )
        refresh_groups(self.session, self.tenant, groups)

    def _review_document(
        self,
        identifier: UUID,
        action: str,
        expected: int,
        reason: str,
        _value: Mapping[str, Any] | None,
        person_id: str | None,
        role: str,
    ) -> dict[str, Any]:
        document, version = self.reader._document(identifier)
        run = self.session.scalar(
            select(DocumentIntelligenceRun).where(
                DocumentIntelligenceRun.tenant_id == self.tenant,
                DocumentIntelligenceRun.document_version_id == version.id,
            )
        )
        if run is None:
            raise NotFound("entity not found")
        before = snapshot(run, ("flagged_for_review", "debtor_status"))
        if action == "mark_for_review":
            self._bump(DocumentIntelligenceRun, run.id, expected, {"flagged_for_review": True})
            after = {**before, "flagged_for_review": True}
        elif action == "clear_review_flag":
            self._bump(DocumentIntelligenceRun, run.id, expected, {"flagged_for_review": False})
            after = {**before, "flagged_for_review": False}
        elif action == "assign_person":
            if role not in {"debtor", "addressee"}:
                raise InvalidReview("role must be debtor or addressee")
            try:
                person = UUID(str(person_id))
            except ValueError:
                raise NotFound("person not found") from None
            if (
                self.session.scalar(
                    select(PersonProfile.id).where(
                        PersonProfile.tenant_id == self.tenant, PersonProfile.id == person
                    )
                )
                is None
            ):
                raise NotFound("person not found")
            self._bump(DocumentIntelligenceRun, run.id, expected, {})
            link = self.session.scalar(
                select(DocumentPersonLink).where(
                    DocumentPersonLink.tenant_id == self.tenant,
                    DocumentPersonLink.document_version_id == version.id,
                    DocumentPersonLink.person_id == person,
                    DocumentPersonLink.role == role,
                )
            )
            if link is None:
                self.session.add(
                    DocumentPersonLink(
                        tenant_id=self.tenant,
                        document_version_id=version.id,
                        person_id=person,
                        role=role,
                        state="confirmed",
                        origin="user",
                        basis=["assigned_by_reviewer"],
                        review_version=1,
                    )
                )
            else:
                link.state, link.origin = "confirmed", "user"
                link.review_version += 1
            # A reviewer's assignment decides the debtor: other open proposals are set aside.
            for other in self.session.scalars(
                select(DocumentPersonLink).where(
                    DocumentPersonLink.tenant_id == self.tenant,
                    DocumentPersonLink.document_version_id == version.id,
                    DocumentPersonLink.role == role,
                    DocumentPersonLink.person_id != person,
                    DocumentPersonLink.state == "review_required",
                )
            ):
                other.state = "rejected"
            after = {**before, "assigned_person": str(person), "role": role}
        else:
            raise InvalidReview("unsupported action for a document")
        self._event("document", run.id, expected + 1, version.id, action, reason, before, after)
        self.session.expire(run)
        self._after_person_change(version.id)
        return {"document_id": str(document.id), "review_version": expected + 1}

    def _review_group(
        self,
        identifier: UUID,
        action: str,
        expected: int,
        reason: str,
        _value: Mapping[str, Any] | None,
        _person: str | None,
        _role: str,
    ) -> dict[str, Any]:
        group = self.session.scalar(
            select(ClaimGroup).where(
                ClaimGroup.tenant_id == self.tenant, ClaimGroup.id == identifier
            )
        )
        visible_groups = {g.id for g in self.reader._groups(self.reader._visible())}
        if group is None or group.id not in visible_groups:
            raise NotFound("entity not found")
        states = {"confirm": "confirmed", "dismiss": "dismissed", "reject": "dismissed"}
        if action not in states:
            raise InvalidReview("unsupported action for a claim group")
        before = snapshot(group, ("status",))
        self._bump(ClaimGroup, group.id, expected, {"status": states[action]})
        self._event(
            "claim_group",
            group.id,
            expected + 1,
            None,
            action,
            reason,
            before,
            {"status": states[action]},
        )
        return {"id": str(group.id), "status": states[action], "review_version": expected + 1}

    def _review_member(
        self,
        identifier: UUID,
        action: str,
        expected: int,
        reason: str,
        _value: Mapping[str, Any] | None,
        _person: str | None,
        _role: str,
    ) -> dict[str, Any]:
        member = self.session.scalar(
            select(ClaimGroupMember).where(
                ClaimGroupMember.tenant_id == self.tenant, ClaimGroupMember.id == identifier
            )
        )
        if member is None:
            raise NotFound("entity not found")
        self._visible_version(member.document_version_id)
        states = {"confirm": "confirmed", "reject": "rejected"}
        if action not in states:
            raise InvalidReview("unsupported action for a claim membership")
        before = snapshot(member, ("state", "origin"))
        self._bump(
            ClaimGroupMember, member.id, expected, {"state": states[action], "origin": "user"}
        )
        self._event(
            "claim_member",
            member.id,
            expected + 1,
            member.document_version_id,
            action,
            reason,
            before,
            {"state": states[action], "origin": "user"},
        )
        self.session.flush()
        refresh_groups(self.session, self.tenant, [member.claim_group_id])
        return {"id": str(member.id), "state": states[action], "review_version": expected + 1}
