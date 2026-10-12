"""Authenticated structured document-intelligence routes (``/v1/intelligence``, ENT-11.8).

Reads need a verified identity and document access (tenant and access level); reviews, person
management and reprocessing need the ``admin`` or ``compliance`` role. Request bodies reject
unknown fields, so a client can never supply a source reference, page or raw value. Answers
come from PostgreSQL deterministically; no model is called. ``/v1/query`` is not touched.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any, Literal

from fastapi import Depends, FastAPI, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from ragops.auth.identity import AuthenticatedUserContext
from ragops.intelligence.queries import (
    REVIEW_ROLES,
    Conflict,
    Forbidden,
    IntelligenceError,
    IntelligenceReader,
    IntelligenceReviewer,
    InvalidReview,
    NotFound,
)
from ragops.workers.queue import IngestionMessage
from ragops.workers.worker import IngestionWorker

ENDPOINT_CLASS = "intelligence"


class PersonCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    label: str = Field(min_length=1, max_length=120)


class AliasCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["name", "identifier"]
    value: str = Field(min_length=2, max_length=255)


class CorrectionValue(BaseModel):
    model_config = ConfigDict(extra="forbid")
    amount: str | None = Field(default=None, max_length=32)
    currency: str | None = Field(default=None, max_length=3)
    date: str | None = Field(default=None, max_length=10)
    text: str | None = Field(default=None, max_length=255)


class ReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    entity_type: Literal[
        "fact", "classification", "person_link", "claim_group", "claim_member", "document"
    ]
    entity_id: str = Field(min_length=1, max_length=64)
    action: Literal[
        "confirm",
        "reject",
        "correct",
        "dismiss",
        "mark_for_review",
        "clear_review_flag",
        "assign_person",
    ]
    expected_version: int = Field(ge=1, le=1_000_000)
    reason_category: Literal[
        "verified_against_source",
        "extraction_error",
        "wrong_party",
        "not_same_claim",
        "duplicate",
        "not_a_claim",
        "needs_follow_up",
        "other",
    ]
    value: CorrectionValue | None = None
    person_id: str | None = Field(default=None, max_length=64)
    role: Literal["debtor", "addressee"] = "debtor"


STATUS = {NotFound: 404, Forbidden: 403, Conflict: 409, InvalidReview: 422}


def _error(exc: IntelligenceError) -> HTTPException:
    status = next((code for kind, code in STATUS.items() if isinstance(exc, kind)), 400)
    detail = {404: "not found", 403: "review role required", 409: "conflict"}.get(status, str(exc))
    return HTTPException(status_code=status, detail=detail)


def register_intelligence_routes(
    app: FastAPI,
    *,
    authenticated: Callable[..., AuthenticatedUserContext],
    open_session: Callable[[], Session],
    enqueue: Callable[[IngestionMessage], None],
    max_attempts: int,
) -> None:
    @contextmanager
    def reading(identity: AuthenticatedUserContext) -> Iterator[IntelligenceReader]:
        session = open_session()
        try:
            yield IntelligenceReader(session, identity.tenant_id, identity.role)
        except IntelligenceError as exc:
            raise _error(exc) from None
        finally:
            session.close()

    @contextmanager
    def reviewing(identity: AuthenticatedUserContext) -> Iterator[IntelligenceReviewer]:
        if not REVIEW_ROLES.intersection(identity.roles):
            raise HTTPException(status_code=403, detail="review role required")
        session = open_session()
        try:
            yield IntelligenceReviewer(
                session, identity.tenant_id, identity.subject or identity.user_id, identity.roles
            )
            session.commit()
        except IntelligenceError as exc:
            session.rollback()
            raise _error(exc) from None
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    identity_dep = Depends(authenticated)

    @app.get("/v1/intelligence/documents")
    def intelligence_documents(
        identity: AuthenticatedUserContext = identity_dep,
        classification: str | None = Query(None, max_length=40),
        person_id: str | None = Query(None, max_length=64),
        review_required: bool | None = None,
    ) -> list[dict[str, Any]]:
        with reading(identity) as reader:
            return reader.documents(classification, person_id, review_required)

    @app.get("/v1/intelligence/documents/{document_id}")
    def intelligence_document(
        document_id: str, identity: AuthenticatedUserContext = identity_dep
    ) -> dict[str, Any]:
        with reading(identity) as reader:
            return reader.document(document_id)

    @app.get("/v1/intelligence/creditors")
    def intelligence_creditors(
        identity: AuthenticatedUserContext = identity_dep,
    ) -> list[dict[str, Any]]:
        with reading(identity) as reader:
            return reader.creditors()

    @app.get("/v1/intelligence/claims")
    def intelligence_claims(
        identity: AuthenticatedUserContext = identity_dep,
        person_id: str | None = Query(None, max_length=64),
    ) -> dict[str, Any]:
        with reading(identity) as reader:
            return reader.claims(person_id)

    @app.get("/v1/intelligence/deadlines")
    def intelligence_deadlines(
        identity: AuthenticatedUserContext = identity_dep,
        person_id: str | None = Query(None, max_length=64),
    ) -> list[dict[str, Any]]:
        with reading(identity) as reader:
            return reader.deadlines(person_id)

    @app.get("/v1/intelligence/findings")
    def intelligence_findings(
        identity: AuthenticatedUserContext = identity_dep,
    ) -> list[dict[str, Any]]:
        with reading(identity) as reader:
            return reader.findings()

    @app.get("/v1/intelligence/facts/{fact_id}")
    def intelligence_fact(
        fact_id: str, identity: AuthenticatedUserContext = identity_dep
    ) -> dict[str, Any]:
        with reading(identity) as reader:
            return reader.fact(fact_id)

    @app.get("/v1/intelligence/persons")
    def intelligence_persons(
        identity: AuthenticatedUserContext = identity_dep,
    ) -> list[dict[str, Any]]:
        with reading(identity) as reader:
            return reader.persons()

    @app.post("/v1/intelligence/persons", status_code=201)
    def intelligence_create_person(
        payload: PersonCreate, identity: AuthenticatedUserContext = identity_dep
    ) -> dict[str, Any]:
        with reviewing(identity) as reviewer:
            return reviewer.create_person(payload.label)

    @app.post("/v1/intelligence/persons/{person_id}/aliases", status_code=201)
    def intelligence_add_alias(
        person_id: str, payload: AliasCreate, identity: AuthenticatedUserContext = identity_dep
    ) -> dict[str, Any]:
        with reviewing(identity) as reviewer:
            return reviewer.add_alias(person_id, payload.kind, payload.value)

    @app.post("/v1/intelligence/reviews")
    def intelligence_review(
        payload: ReviewRequest, identity: AuthenticatedUserContext = identity_dep
    ) -> dict[str, Any]:
        value = payload.value.model_dump(exclude_none=True) if payload.value else None
        with reviewing(identity) as reviewer:
            return reviewer.review(
                payload.entity_type,
                payload.entity_id,
                payload.action,
                payload.expected_version,
                payload.reason_category,
                value=value,
                person_id=payload.person_id,
                role=payload.role,
            )

    @app.post("/v1/intelligence/documents/{document_id}/reprocess", status_code=202)
    def intelligence_reprocess(
        document_id: str, identity: AuthenticatedUserContext = identity_dep
    ) -> dict[str, Any]:
        """Re-run extraction and analysis of the current version through the worker."""
        from uuid import UUID

        message: IngestionMessage | None = None
        with reviewing(identity) as reviewer:
            try:
                identifier = UUID(document_id)
            except ValueError:
                raise HTTPException(status_code=404, detail="not found") from None
            document, version = reviewer.reader._document(identifier)
            job = IngestionWorker(reviewer.session, max_attempts=max_attempts).create_job(
                identity.tenant_id, document.id, version.id
            )
            message = IngestionMessage(job.id, identity.tenant_id, job.correlation_id)
            accepted = {
                "job_id": str(job.id),
                "document_id": str(document.id),
                "version_id": str(version.id),
                "status": job.status,
            }
        # Enqueue only after the job is committed, as the upload route does; a job without a
        # message is failed visibly so the retry route can enqueue it again.
        try:
            enqueue(message)
        except Exception:
            mark_unqueued(identity.tenant_id, message)
            raise
        return accepted

    def mark_unqueued(tenant_id: str, message: IngestionMessage) -> None:
        from sqlalchemy import select

        from ragops.persistence.models import IngestionJob

        session = open_session()
        try:
            job = session.scalar(
                select(IngestionJob).where(
                    IngestionJob.tenant_id == tenant_id, IngestionJob.id == message.job_id
                )
            )
            if job is not None:
                job.status = job.state = "failed"
                job.error_code = job.failure_code = "queue_unavailable"
                job.error_message_redacted = "ingestion queue unavailable; retry the job"
                session.commit()
        finally:
            session.close()
