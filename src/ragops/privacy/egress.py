"""Privacy egress gate: nothing reaches an external model provider without an exact grant.

Invariant: no confidential source text, user question or extracted fact leaves the local
environment unless a durable, tenant-specific policy authorizes exactly this purpose,
provider, model and these data classes, and the authorization decision was recorded first.

Decisions are taken from PostgreSQL only. A missing, inactive, expired, malformed or
unreadable policy denies; so does a decision that cannot be recorded. An allowed decision
yields an ``EgressPermit`` that is bound to the digest of the exact provider payload and to
the policy id and version, is sealed with a per-process key, expires after ``PERMIT_TTL`` and
can be used once. External adapters refuse to send without a valid permit, so a caller that
skips the gate fails closed.

Linearization point: the *dispatch check*. Immediately before the SDK call the adapter
consumes the permit and the guard re-reads the policy (``FOR SHARE`` on PostgreSQL) and
records a ``dispatch`` decision. A revocation, suspension, version change or expiry that
committed before this check denies the request, as does an unreadable store; only a request
whose dispatch check already succeeded is in flight and may complete.

A policy is a technical authorization by the platform operator. It is not a legal basis and
not data-subject consent: ``personal_data`` policies only record the operator's references
to those prerequisites, and they are required before personal data may be sent at all.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import threading
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any, Protocol
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from ragops.persistence.models import (
    PrivacyEgressDecision,
    PrivacyEgressPolicy,
    PrivacyTenantProfile,
)

PERMIT_TTL = timedelta(seconds=60)


class Purpose(StrEnum):
    RAG_ANSWER = "rag_answer"
    PROVIDER_VALIDATION = "provider_validation"


class DataClass(StrEnum):
    USER_QUESTION = "user_question"
    DOCUMENT_TEXT = "document_text"
    EXTRACTED_FACT = "extracted_fact"
    SYNTHETIC_PROBE = "synthetic_probe"
    # Content whose origin is not established. It can never be authorized.
    UNCLASSIFIED = "unclassified"


AUTHORIZABLE = frozenset(DataClass) - {DataClass.UNCLASSIFIED}
# Retention controls an operator may attest per provider for personal data. OpenAI's
# Responses API honours store=false (always sent by the adapter); Azure OpenAI chat
# completions have no equivalent request flag, so only a contractual zero-retention
# arrangement can be recorded for it. Unknown providers support none.
RETENTION_CONTROLS: dict[str, frozenset[str]] = {
    "openai": frozenset({"store_false", "zero_data_retention"}),
    "azure_openai": frozenset({"zero_data_retention"}),
}


class Reason(StrEnum):
    AUTHORIZED = "authorized"
    NO_POLICY = "no_policy"
    PROVIDER_NOT_AUTHORIZED = "provider_not_authorized"
    MODEL_NOT_AUTHORIZED = "model_not_authorized"
    POLICY_SUSPENDED = "policy_suspended"
    POLICY_REVOKED = "policy_revoked"
    POLICY_NOT_YET_VALID = "policy_not_yet_valid"
    POLICY_EXPIRED = "policy_expired"
    POLICY_MALFORMED = "policy_malformed"
    DATA_CLASS_NOT_AUTHORIZED = "data_class_not_authorized"
    UNCLASSIFIED_CONTENT = "unclassified_content"
    SYNTHETIC_SCOPE_REQUIRES_SYNTHETIC_TENANT = "synthetic_scope_requires_synthetic_tenant"
    PERSONAL_DATA_PREREQUISITES_MISSING = "personal_data_prerequisites_missing"
    RETENTION_CONTROL_UNSUPPORTED = "retention_control_unsupported"
    POLICY_VERSION_CHANGED = "policy_version_changed"
    POLICY_STORE_UNAVAILABLE = "policy_store_unavailable"
    DECISION_NOT_RECORDED = "decision_not_recorded"
    NO_GUARD = "no_guard_configured"


class EgressDenied(RuntimeError):
    """External processing is not authorized; nothing was or may be sent."""

    status_code = 409

    def __init__(self, reason: Reason) -> None:
        super().__init__(f"external processing not authorized: {reason.value}")
        self.reason = reason


@dataclass(frozen=True)
class EgressRequest:
    """What would leave the system. Tenant and classes come from the trusted backend only."""

    tenant_id: str
    correlation_id: str
    purpose: Purpose
    provider_id: str
    model_id: str
    data_classes: frozenset[DataClass]


@dataclass(frozen=True)
class EgressDecision:
    allowed: bool
    reason: Reason
    decision_id: UUID | None = None
    policy_id: UUID | None = None
    policy_version: int | None = None


# ------------------------------------------------------------------ locality and payload


def is_external(adapter: object) -> bool:
    """Locality is a property of the adapter class; anything not declared local is external."""
    return getattr(type(adapter), "egress_locality", None) != "local"


class _CitationLike(Protocol):
    @property
    def source_id(self) -> str: ...
    @property
    def title(self) -> str: ...
    @property
    def snippet(self) -> str: ...


def payload_digest(
    provider_id: str,
    model_id: str,
    question: str,
    citations: Sequence[_CitationLike],
    context: str,
) -> str:
    """Digest of everything an adapter derives its request from (the request is a pure
    function of these values plus fixed instructions)."""
    canonical = json.dumps(
        {
            "provider": provider_id,
            "model": model_id,
            "question": question,
            "context": context,
            "citations": [[c.source_id, c.title, c.snippet] for c in citations],
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


# ------------------------------------------------------------------ permits


@dataclass(frozen=True)
class EgressPermit:
    decision_id: str
    tenant_id: str
    purpose: str
    provider_id: str
    model_id: str
    digest: str
    expires_at: float
    kind: str  # "policy" (confirmed again at dispatch) or "probe" (fixed synthetic payload)
    policy_id: str
    policy_version: int
    data_classes: tuple[str, ...]
    seal: str
    # Dispatch confirmation by the issuing guard; not sealed (it is code, not a claim).
    confirm: Callable[[EgressPermit], Reason] | None = field(
        default=None, compare=False, repr=False
    )

    def sealed_fields(self) -> tuple[object, ...]:
        return (
            self.decision_id,
            self.tenant_id,
            self.purpose,
            self.provider_id,
            self.model_id,
            self.digest,
            self.expires_at,
            self.kind,
            self.policy_id,
            self.policy_version,
            ",".join(self.data_classes),
        )


_PERMIT_KEY = secrets.token_bytes(32)
_used: dict[str, float] = {}
_used_lock = threading.Lock()


def _seal(*fields: object) -> str:
    message = "\x1f".join(str(field) for field in fields).encode()
    return hmac.new(_PERMIT_KEY, message, hashlib.sha256).hexdigest()


def _issue(
    decision_id: str,
    tenant_id: str,
    purpose: str,
    provider_id: str,
    model_id: str,
    digest: str,
    now: datetime,
    *,
    kind: str,
    policy_id: str = "",
    policy_version: int = 0,
    data_classes: tuple[str, ...] = (),
    confirm: Callable[[EgressPermit], Reason] | None = None,
) -> EgressPermit:
    unsealed = EgressPermit(
        decision_id,
        tenant_id,
        purpose,
        provider_id,
        model_id,
        digest,
        (now + PERMIT_TTL).timestamp(),
        kind,
        policy_id,
        policy_version,
        tuple(sorted(data_classes)),
        seal="",
        confirm=confirm,
    )
    return replace(unsealed, seal=_seal(*unsealed.sealed_fields()))


def require_permit(
    permit: EgressPermit | None,
    provider_id: str,
    model_id: str,
    question: str,
    citations: Sequence[_CitationLike],
    context: str,
    *,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> None:
    """Called by every external adapter before it builds a request. Raises EgressDenied."""
    if not isinstance(permit, EgressPermit):
        raise EgressDenied(Reason.NO_POLICY)
    expected = _seal(*permit.sealed_fields())
    digest = payload_digest(provider_id, model_id, question, citations, context)
    if (
        not hmac.compare_digest(expected, permit.seal)
        or (permit.provider_id, permit.model_id) != (provider_id, model_id)
        or not hmac.compare_digest(permit.digest, digest)
        or permit.expires_at < now().timestamp()
    ):
        raise EgressDenied(Reason.NO_POLICY)
    with _used_lock:
        current = now().timestamp()
        for key in [key for key, expiry in _used.items() if expiry < current]:
            del _used[key]
        if permit.decision_id in _used:
            raise EgressDenied(Reason.NO_POLICY)
        _used[permit.decision_id] = permit.expires_at
    # Consumed above (a denied confirmation never frees it). Policy permits are confirmed
    # against the policy store now: this is the linearization point of the authorization.
    if permit.kind == "probe":
        return
    if permit.kind != "policy" or permit.confirm is None:
        raise EgressDenied(Reason.NO_POLICY)
    reason = permit.confirm(permit)
    if reason is not Reason.AUTHORIZED:
        raise EgressDenied(reason)


# ------------------------------------------------------------------ synthetic probe

# The only payload the paid provider gate may send. It carries no tenant data, so the
# operator's opt-in to that paid run is the authorization; any other content is refused.
SYNTHETIC_PROBE_QUESTION = (
    "Return a short confirmation that the live external provider is reachable."
)
SYNTHETIC_PROBE_CONTEXT = "Synthetic connectivity probe; do not invent business facts."


def synthetic_probe_permit(
    provider_id: str,
    model_id: str,
    question: str,
    citations: Sequence[_CitationLike],
    context: str,
    *,
    environment: str | None = None,
) -> EgressPermit:
    environment = (environment or os.getenv("RAGOPS_ENV") or "local").lower()
    if (
        environment == "production"
        or question != SYNTHETIC_PROBE_QUESTION
        or context != SYNTHETIC_PROBE_CONTEXT
        or list(citations)
    ):
        raise EgressDenied(Reason.NO_POLICY)
    digest = payload_digest(provider_id, model_id, question, citations, context)
    return _issue(
        str(uuid4()),
        "",
        Purpose.PROVIDER_VALIDATION.value,
        provider_id,
        model_id,
        digest,
        datetime.now(UTC),
        kind="probe",
        data_classes=(DataClass.SYNTHETIC_PROBE.value,),
    )


# ------------------------------------------------------------------ policy evaluation


def _aware(value: datetime) -> datetime:
    # SQLite returns naive datetimes; values are always written in UTC.
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def evaluate(
    request: EgressRequest,
    policies: Iterable[PrivacyEgressPolicy],
    profile: PrivacyTenantProfile | None,
    now: datetime,
) -> tuple[Reason, PrivacyEgressPolicy | None]:
    """Pure decision over the tenant's policies for this purpose and provider."""
    if DataClass.UNCLASSIFIED in request.data_classes or not request.data_classes:
        return Reason.UNCLASSIFIED_CONTENT, None
    candidates = [p for p in policies if p.tenant_id == request.tenant_id]
    if not candidates:
        return Reason.NO_POLICY, None
    same_provider = [p for p in candidates if p.provider_id == request.provider_id]
    if not same_provider:
        return Reason.PROVIDER_NOT_AUTHORIZED, None
    exact = [p for p in same_provider if p.model_id == request.model_id]
    if not exact:
        return Reason.MODEL_NOT_AUTHORIZED, None
    (policy,) = exact  # unique per (tenant, purpose, provider, model)
    try:
        classes = policy.data_classes
        if (
            not isinstance(classes, list)
            or not classes
            or any(item not in {c.value for c in AUTHORIZABLE} for item in classes)
            or policy.state not in {"active", "suspended", "revoked"}
            or policy.data_scope not in {"synthetic_test_data", "personal_data"}
            or not isinstance(policy.version, int)
            or policy.version < 1
            or not (policy.approved_by or "").strip()
        ):
            return Reason.POLICY_MALFORMED, policy
        valid_from, valid_until = _aware(policy.valid_from), _aware(policy.valid_until)
    except (TypeError, ValueError, AttributeError):
        return Reason.POLICY_MALFORMED, policy
    if policy.state == "revoked":
        return Reason.POLICY_REVOKED, policy
    if policy.state == "suspended":
        return Reason.POLICY_SUSPENDED, policy
    if now < valid_from:
        return Reason.POLICY_NOT_YET_VALID, policy
    if now >= valid_until:
        return Reason.POLICY_EXPIRED, policy
    if not {c.value for c in request.data_classes} <= set(classes):
        return Reason.DATA_CLASS_NOT_AUTHORIZED, policy
    if policy.data_scope == "synthetic_test_data":
        if profile is None or not profile.synthetic_data_only:
            return Reason.SYNTHETIC_SCOPE_REQUIRES_SYNTHETIC_TENANT, policy
    else:
        refs = (
            policy.legal_basis_ref,
            policy.processor_agreement_ref,
            policy.transfer_mechanism_ref,
        )
        if any(not (ref or "").strip() for ref in refs):
            return Reason.PERSONAL_DATA_PREREQUISITES_MISSING, policy
        if policy.retention_control not in RETENTION_CONTROLS.get(policy.provider_id, ()):
            return Reason.RETENTION_CONTROL_UNSUPPORTED, policy
    return Reason.AUTHORIZED, policy


class PrivacyEgressGuard:
    """The single place where external processing is authorized (and recorded)."""

    def __init__(
        self,
        engine: Engine | Callable[[], Engine],
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._engine = engine
        self.clock = clock

    def _bind(self) -> Engine:
        engine = self._engine
        return engine if isinstance(engine, Engine) else engine()

    def decide(self, request: EgressRequest, *, stage: str) -> EgressDecision:
        """Evaluate and durably record one decision; any failure denies."""
        now = self.clock()
        try:
            engine = self._bind()
            with Session(engine) as session, session.begin():
                query = select(PrivacyEgressPolicy).where(
                    PrivacyEgressPolicy.tenant_id == request.tenant_id,
                    PrivacyEgressPolicy.purpose == request.purpose.value,
                )
                if engine.dialect.name == "postgresql":
                    # A concurrent revocation (UPDATE) waits for or excludes this decision.
                    query = query.with_for_update(read=True)
                policies = list(session.scalars(query))
                profile = session.get(PrivacyTenantProfile, request.tenant_id)
                reason, policy = evaluate(request, policies, profile, now)
                row = PrivacyEgressDecision(
                    tenant_id=request.tenant_id,
                    correlation_id=request.correlation_id[:64],
                    stage=stage,
                    purpose=request.purpose.value,
                    provider_id=request.provider_id[:100],
                    model_id=request.model_id[:200],
                    data_classes=sorted(c.value for c in request.data_classes),
                    policy_id=policy.id if policy is not None else None,
                    policy_version=policy.version if policy is not None else None,
                    decision="allow" if reason is Reason.AUTHORIZED else "deny",
                    reason_code=reason.value,
                    created_at=now,
                )
                session.add(row)
                session.flush()
                decision = EgressDecision(
                    reason is Reason.AUTHORIZED,
                    reason,
                    row.id,
                    policy.id if policy is not None else None,
                    policy.version if policy is not None else None,
                )
        except SQLAlchemyError:
            return EgressDecision(False, Reason.POLICY_STORE_UNAVAILABLE)
        except Exception:  # noqa: BLE001 - any other failure must also deny
            return EgressDecision(False, Reason.DECISION_NOT_RECORDED)
        return decision

    def permit(
        self,
        request: EgressRequest,
        question: str,
        citations: Sequence[_CitationLike],
        context: str,
    ) -> EgressPermit:
        """Authoritative boundary check for the exact payload; raises EgressDenied.

        The permit is still confirmed once more at dispatch (``confirm``)."""
        decision = self.decide(request, stage="boundary")
        if (
            not decision.allowed
            or decision.decision_id is None
            or decision.policy_id is None
            or decision.policy_version is None
        ):
            raise EgressDenied(decision.reason)
        digest = payload_digest(request.provider_id, request.model_id, question, citations, context)
        return _issue(
            str(decision.decision_id),
            request.tenant_id,
            request.purpose.value,
            request.provider_id,
            request.model_id,
            digest,
            self.clock(),
            kind="policy",
            policy_id=str(decision.policy_id),
            policy_version=decision.policy_version,
            data_classes=tuple(c.value for c in request.data_classes),
            confirm=self.confirm,
        )

    def confirm(self, permit: EgressPermit) -> Reason:
        """Dispatch check (linearization point): is the exact policy version that authorized
        this permit still active and valid now? Records a ``dispatch`` decision; any failure
        or change denies."""
        now = self.clock()
        try:
            request = EgressRequest(
                tenant_id=permit.tenant_id,
                correlation_id=permit.decision_id,
                purpose=Purpose(permit.purpose),
                provider_id=permit.provider_id,
                model_id=permit.model_id,
                data_classes=frozenset(DataClass(c) for c in permit.data_classes),
            )
            policy_id = UUID(permit.policy_id)
            engine = self._bind()
            with Session(engine) as session, session.begin():
                query = select(PrivacyEgressPolicy).where(
                    PrivacyEgressPolicy.tenant_id == permit.tenant_id,
                    PrivacyEgressPolicy.id == policy_id,
                    PrivacyEgressPolicy.purpose == permit.purpose,
                )
                if engine.dialect.name == "postgresql":
                    query = query.with_for_update(read=True)
                policy = session.scalars(query).one_or_none()
                profile = session.get(PrivacyTenantProfile, permit.tenant_id)
                if policy is None:
                    reason = Reason.NO_POLICY
                else:
                    reason, _ = evaluate(request, [policy], profile, now)
                    if reason is Reason.AUTHORIZED and policy.version != permit.policy_version:
                        reason = Reason.POLICY_VERSION_CHANGED
                session.add(
                    PrivacyEgressDecision(
                        tenant_id=permit.tenant_id,
                        correlation_id=permit.decision_id[:64],
                        stage="dispatch",
                        purpose=permit.purpose,
                        provider_id=permit.provider_id[:100],
                        model_id=permit.model_id[:200],
                        data_classes=sorted(permit.data_classes),
                        policy_id=policy_id,
                        policy_version=policy.version if policy is not None else None,
                        decision="allow" if reason is Reason.AUTHORIZED else "deny",
                        reason_code=reason.value,
                        created_at=now,
                    )
                )
        except SQLAlchemyError:
            return Reason.POLICY_STORE_UNAVAILABLE
        except Exception:  # noqa: BLE001 - malformed permit or any other failure denies
            return Reason.DECISION_NOT_RECORDED
        return reason


def decision_view(row: Any) -> dict[str, Any]:
    """Safe representation for administration reads: metadata only."""
    return {
        "decision_id": str(row.id),
        "stage": row.stage,
        "purpose": row.purpose,
        "provider_id": row.provider_id,
        "model_id": row.model_id,
        "data_classes": list(row.data_classes or []),
        "policy_id": str(row.policy_id) if row.policy_id else None,
        "policy_version": row.policy_version,
        "decision": row.decision,
        "reason_code": row.reason_code,
        "created_at": _aware(row.created_at).isoformat(),
    }
