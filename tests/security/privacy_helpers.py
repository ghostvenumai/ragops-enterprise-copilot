"""Synthetic privacy egress fixtures: real policies in a migrated database, real guard.

There is no test-only bypass in the product: a test that needs an external adapter to send
obtains a permit exactly like production code does.
"""

from __future__ import annotations

import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session
from tests.security.finops_helpers import migrated_sqlite

from ragops.persistence.models import Tenant
from ragops.privacy import policies
from ragops.privacy.egress import (
    DataClass,
    EgressPermit,
    EgressRequest,
    PrivacyEgressGuard,
    Purpose,
)

QA_CLASSES = ("document_text", "user_question")


def ensure_tenant(engine: Engine, tenant: str) -> None:
    with Session(engine) as session, session.begin():
        if session.get(Tenant, tenant) is None:
            session.add(Tenant(id=tenant, name=tenant))


def synthetic_grant(
    engine: Engine,
    tenant: str,
    provider_id: str,
    model_id: str,
    *,
    classes: tuple[str, ...] = QA_CLASSES,
    purpose: str = "rag_answer",
    valid_for: timedelta = timedelta(hours=1),
    profile: bool = True,
    **overrides: Any,
) -> Any:
    """An active synthetic-data policy for a tenant marked as synthetic-only."""
    ensure_tenant(engine, tenant)
    if profile:
        policies.record_tenant_profile(
            engine, tenant, synthetic_data_only=True, actor="test-operator"
        )
    now = datetime.now(UTC)
    values: dict[str, Any] = {
        "tenant_id": tenant,
        "purpose": purpose,
        "provider_id": provider_id,
        "model_id": model_id,
        "data_classes": classes,
        "data_scope": "synthetic_test_data",
        "valid_from": now - timedelta(minutes=1),
        "valid_until": now + valid_for,
        "actor": "test-operator",
        "reason": "synthetic test fixture",
    }
    values.update(overrides)
    return policies.grant(engine, policies.PolicyGrant(**values))


def permit_for(provider: Any, question: str, citations: list[Any], context: str) -> EgressPermit:
    """A real permit for one adapter call, from a throwaway database."""
    engine, _ = migrated_sqlite(Path(tempfile.mkdtemp()) / "privacy.db")
    synthetic_grant(engine, "tenant-adapter", provider.provider_id, provider.model)
    request = EgressRequest(
        tenant_id="tenant-adapter",
        correlation_id="adapter-test",
        purpose=Purpose.RAG_ANSWER,
        provider_id=provider.provider_id,
        model_id=provider.model,
        data_classes=frozenset({DataClass.USER_QUESTION, DataClass.DOCUMENT_TEXT}),
    )
    try:
        return PrivacyEgressGuard(engine).permit(request, question, citations, context)
    finally:
        engine.dispose()
