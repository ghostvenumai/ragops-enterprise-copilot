"""PostgreSQL: privacy egress evidence is append-only and revocation races are serialized.

Skipped unless RAGOPS_TEST_DATABASE_URL points at an isolated ragops_test_* database. The
test database is created and dropped by the shared fixture; synthetic data only.
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

pytest.importorskip("alembic", reason="Install declared persistence extra; product gate blocks")

from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session
from tests.integration.test_query_finops_postgres import database  # noqa: F401
from tests.security.privacy_helpers import synthetic_grant

from ragops.persistence.models import PrivacyEgressDecision
from ragops.privacy import policies
from ragops.privacy.egress import DataClass, EgressRequest, PrivacyEgressGuard, Purpose

pytestmark = pytest.mark.integration


def request(correlation: str) -> EgressRequest:
    return EgressRequest(
        "tenant-a",
        correlation,
        Purpose.RAG_ANSWER,
        "openai",
        "gpt-synthetic",
        frozenset({DataClass.USER_QUESTION, DataClass.DOCUMENT_TEXT}),
    )


def test_revisions_and_decisions_cannot_be_rewritten(database) -> None:  # noqa: F811
    policy = synthetic_grant(database, "tenant-a", "openai", "gpt-synthetic")
    assert PrivacyEgressGuard(database).decide(request("c1"), stage="precheck").allowed
    for statement in (
        "UPDATE privacy_egress_decisions SET tenant_id = tenant_id",
        "DELETE FROM privacy_egress_decisions",
        "UPDATE privacy_egress_policy_revisions SET tenant_id = tenant_id",
        "DELETE FROM privacy_egress_policy_revisions",
    ):
        with pytest.raises(DBAPIError, match="append-only"), database.begin() as connection:
            connection.execute(text(statement))
    assert policies.list_policies(database, "tenant-a")[0]["policy_id"] == str(policy.id)


def test_no_allow_is_recorded_after_a_revocation_commits(database) -> None:  # noqa: F811
    policy = synthetic_grant(database, "tenant-a", "openai", "gpt-synthetic")
    guard = PrivacyEgressGuard(database)
    revoked = threading.Event()

    def decide(index: int) -> None:
        if index == 10:
            policies.change_state(
                database,
                "tenant-a",
                policy.id,
                "revoke",
                expected_version=1,
                actor="op",
                reason="r",
            )
            revoked.set()
        else:
            guard.decide(request(f"race-{index}"), stage="boundary")

    with ThreadPoolExecutor(max_workers=10) as pool:
        list(pool.map(decide, range(21)))
    assert revoked.is_set()
    after = guard.decide(request("after"), stage="boundary")
    assert not after.allowed and after.reason.value == "policy_revoked"
    with Session(database) as session:
        rows = list(session.scalars(select(PrivacyEgressDecision)))
    # Every allow was decided on version 1, every deny on the revoked version 2.
    assert {(r.decision, r.policy_version) for r in rows} <= {("allow", 1), ("deny", 2)}


def test_a_permit_issued_before_a_committed_revocation_is_refused_at_dispatch(
    database,  # noqa: F811
) -> None:
    from ragops.privacy.egress import EgressDenied, require_permit

    policy = synthetic_grant(database, "tenant-a", "openai", "gpt-synthetic")
    guard = PrivacyEgressGuard(database)
    permit = guard.permit(request("dispatch"), "frage", [], "kontext")
    policies.change_state(
        database, "tenant-a", policy.id, "revoke", expected_version=1, actor="op", reason="r"
    )
    with pytest.raises(EgressDenied) as raised:
        require_permit(permit, "openai", "gpt-synthetic", "frage", [], "kontext")
    assert raised.value.reason.value == "policy_revoked"
    with Session(database) as session:
        stages = [
            (row.stage, row.decision, row.reason_code)
            for row in session.scalars(
                select(PrivacyEgressDecision).order_by(PrivacyEgressDecision.created_at)
            )
        ]
    assert stages == [("boundary", "allow", "authorized"), ("dispatch", "deny", "policy_revoked")]
