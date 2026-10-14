"""Operator administration of privacy egress policies (no HTTP write path exists).

Every change writes the new policy state and an append-only revision with the actor and a
reason in one transaction. State changes require the expected version (optimistic
concurrency): a stale writer gets ``PolicyConflict`` and changes nothing. Policies always
expire; there is no wildcard for purpose, provider, model or data class.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from ragops.persistence.models import (
    PrivacyEgressPolicy,
    PrivacyEgressPolicyRevision,
    PrivacyTenantProfile,
    Tenant,
)
from ragops.privacy.egress import AUTHORIZABLE, RETENTION_CONTROLS, Purpose

TRANSITIONS = {
    "suspend": ({"active"}, "suspended"),
    "resume": ({"suspended"}, "active"),
    "revoke": ({"active", "suspended"}, "revoked"),
}


class PolicyError(ValueError):
    """The requested policy is invalid; nothing was written."""


class PolicyConflict(RuntimeError):
    """The policy changed since the caller read it, or does not exist in this tenant."""


@dataclass(frozen=True)
class PolicyGrant:
    tenant_id: str
    purpose: str
    provider_id: str
    model_id: str
    data_classes: tuple[str, ...]
    data_scope: str
    valid_from: datetime
    valid_until: datetime
    actor: str
    reason: str
    legal_basis_ref: str | None = None
    processor_agreement_ref: str | None = None
    transfer_mechanism_ref: str | None = None
    retention_control: str | None = None


def _now() -> datetime:
    return datetime.now(UTC)


def _text(value: str | None, name: str, limit: int) -> str:
    cleaned = (value or "").strip()
    if not cleaned or len(cleaned) > limit:
        raise PolicyError(f"{name} is required (1-{limit} characters)")
    return cleaned


def _snapshot(policy: PrivacyEgressPolicy) -> dict[str, Any]:
    return {
        "purpose": policy.purpose,
        "provider_id": policy.provider_id,
        "model_id": policy.model_id,
        "data_classes": list(policy.data_classes),
        "data_scope": policy.data_scope,
        "state": policy.state,
        "valid_from": policy.valid_from.isoformat(),
        "valid_until": policy.valid_until.isoformat(),
        "legal_basis_ref": policy.legal_basis_ref,
        "processor_agreement_ref": policy.processor_agreement_ref,
        "transfer_mechanism_ref": policy.transfer_mechanism_ref,
        "retention_control": policy.retention_control,
        "approved_by": policy.approved_by,
    }


def validate(grant: PolicyGrant) -> None:
    if grant.purpose not in {p.value for p in Purpose}:
        raise PolicyError("unknown purpose")
    _text(grant.provider_id, "provider_id", 100)
    _text(grant.model_id, "model_id", 200)
    if "*" in grant.provider_id or "*" in grant.model_id:
        raise PolicyError("wildcards are not allowed")
    classes = set(grant.data_classes)
    if not classes or not classes <= {c.value for c in AUTHORIZABLE}:
        raise PolicyError("data_classes must be a non-empty set of known, authorizable classes")
    if grant.valid_from.tzinfo is None or grant.valid_until.tzinfo is None:
        raise PolicyError("validity must be timezone-aware")
    if grant.valid_until <= grant.valid_from:
        raise PolicyError("valid_until must be after valid_from")
    if grant.data_scope == "personal_data":
        for name in ("legal_basis_ref", "processor_agreement_ref", "transfer_mechanism_ref"):
            _text(getattr(grant, name), name, 200)
        if grant.retention_control not in RETENTION_CONTROLS.get(grant.provider_id, ()):
            raise PolicyError("retention_control is not supported for this provider")
    elif grant.data_scope != "synthetic_test_data":
        raise PolicyError("unknown data_scope")
    _text(grant.actor, "actor", 255)
    _text(grant.reason, "reason", 200)


def record_tenant_profile(
    engine: Engine, tenant_id: str, *, synthetic_data_only: bool, actor: str
) -> None:
    actor = _text(actor, "actor", 255)
    with Session(engine) as session, session.begin():
        if session.get(Tenant, tenant_id) is None:
            raise PolicyConflict("tenant not found")
        profile = session.get(PrivacyTenantProfile, tenant_id)
        if profile is None:
            profile = PrivacyTenantProfile(tenant_id=tenant_id)
            session.add(profile)
        profile.synthetic_data_only = synthetic_data_only
        profile.recorded_by = actor
        profile.recorded_at = _now()


def grant(engine: Engine, request: PolicyGrant) -> PrivacyEgressPolicy:
    """Create an active policy (version 1). An existing scope must be changed, not replaced."""
    validate(request)
    now = _now()
    with Session(engine, expire_on_commit=False) as session, session.begin():
        if session.get(Tenant, request.tenant_id) is None:
            raise PolicyConflict("tenant not found")
        policy = PrivacyEgressPolicy(
            tenant_id=request.tenant_id,
            purpose=request.purpose,
            provider_id=request.provider_id.strip(),
            model_id=request.model_id.strip(),
            data_classes=sorted(set(request.data_classes)),
            data_scope=request.data_scope,
            state="active",
            version=1,
            valid_from=request.valid_from,
            valid_until=request.valid_until,
            legal_basis_ref=request.legal_basis_ref,
            processor_agreement_ref=request.processor_agreement_ref,
            transfer_mechanism_ref=request.transfer_mechanism_ref,
            retention_control=request.retention_control,
            approved_by=request.actor.strip(),
            created_at=now,
            updated_at=now,
        )
        session.add(policy)
        session.flush()
        session.add(
            PrivacyEgressPolicyRevision(
                tenant_id=policy.tenant_id,
                policy_id=policy.id,
                version=1,
                state="active",
                snapshot=_snapshot(policy),
                actor=request.actor.strip(),
                reason=request.reason.strip(),
                created_at=now,
            )
        )
    return policy


def change_state(
    engine: Engine,
    tenant_id: str,
    policy_id: UUID,
    action: str,
    *,
    expected_version: int,
    actor: str,
    reason: str,
) -> int:
    """Suspend, resume or revoke; returns the new version. Revocation is final."""
    if action not in TRANSITIONS:
        raise PolicyError("unknown action")
    actor, reason = _text(actor, "actor", 255), _text(reason, "reason", 200)
    allowed_from, target = TRANSITIONS[action]
    now = _now()
    with Session(engine, expire_on_commit=False) as session, session.begin():
        result = session.execute(
            update(PrivacyEgressPolicy)
            .where(
                PrivacyEgressPolicy.tenant_id == tenant_id,
                PrivacyEgressPolicy.id == policy_id,
                PrivacyEgressPolicy.version == expected_version,
                PrivacyEgressPolicy.state.in_(allowed_from),
            )
            .values(state=target, version=expected_version + 1, updated_at=now)
            .execution_options(synchronize_session=False)
        )
        if result.rowcount != 1:  # type: ignore[attr-defined]
            raise PolicyConflict("policy changed, is in another state or does not exist")
        policy = session.get(PrivacyEgressPolicy, (tenant_id, policy_id))
        assert policy is not None
        session.refresh(policy)
        session.add(
            PrivacyEgressPolicyRevision(
                tenant_id=tenant_id,
                policy_id=policy_id,
                version=expected_version + 1,
                state=target,
                snapshot=_snapshot(policy),
                actor=actor,
                reason=reason,
                created_at=now,
            )
        )
    return expected_version + 1


def policy_view(policy: PrivacyEgressPolicy) -> dict[str, Any]:
    return {"policy_id": str(policy.id), "version": policy.version, **_snapshot(policy)}


def list_policies(engine: Engine, tenant_id: str) -> list[dict[str, Any]]:
    with Session(engine) as session:
        rows: Iterable[PrivacyEgressPolicy] = session.scalars(
            select(PrivacyEgressPolicy)
            .where(PrivacyEgressPolicy.tenant_id == tenant_id)
            .order_by(PrivacyEgressPolicy.created_at)
        )
        return [policy_view(row) for row in rows]
