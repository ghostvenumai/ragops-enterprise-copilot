"""A permit issued before a revocation never starts a new external request (ENT-13.1 review).

Linearization point: the dispatch check that the adapter performs immediately before it
calls the SDK. A revocation, suspension, version change or expiry committed before that
check denies the request; only a request already dispatched may complete. Synthetic data,
real OpenAI adapter class with a counting fake SDK client.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest

pytest.importorskip("alembic", reason="Install declared persistence extra; product gate blocks")

from sqlalchemy import update
from sqlalchemy.exc import OperationalError
from tests.security.finops_helpers import migrated_sqlite
from tests.security.privacy_helpers import synthetic_grant

from ragops.llm.providers import OpenAIProvider
from ragops.modeling.router import ProviderError, ProviderErrorCategory
from ragops.persistence.models import PrivacyEgressPolicy
from ragops.privacy import policies
from ragops.privacy.egress import DataClass, EgressRequest, PrivacyEgressGuard, Purpose

MODEL = "gpt-synthetic"
QUESTION, CONTEXT = "Synthetische Frage zu Akte 4711?", "Synthetischer Kontext ohne echte Daten."


class CountingSDK:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.responses = SimpleNamespace(create=self._create)

    def _create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        return SimpleNamespace(
            output_text="ok",
            model=MODEL,
            usage=SimpleNamespace(input_tokens=1, output_tokens=1, total_tokens=2),
        )


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "fake-not-a-key")
    monkeypatch.setenv("OPENAI_MODEL", MODEL)
    engine, _ = migrated_sqlite(tmp_path / "permit.db")
    policy = synthetic_grant(engine, "tenant-a", "openai", MODEL)
    guard = PrivacyEgressGuard(engine)
    request = EgressRequest(
        "tenant-a",
        "permit-review",
        Purpose.RAG_ANSWER,
        "openai",
        MODEL,
        frozenset({DataClass.USER_QUESTION, DataClass.DOCUMENT_TEXT}),
    )
    sdk = CountingSDK()
    permit = guard.permit(request, QUESTION, [], CONTEXT)  # issued while authorized
    return SimpleNamespace(
        engine=engine, policy=policy, guard=guard, permit=permit, sdk=sdk,
        adapter=OpenAIProvider(client=sdk),
    )  # fmt: skip


def send(world) -> None:
    world.adapter.generate(QUESTION, [], CONTEXT, permit=world.permit)


def assert_refused(world) -> None:
    with pytest.raises(ProviderError) as raised:
        send(world)
    assert raised.value.category is ProviderErrorCategory.POLICY_DENIED
    assert world.sdk.calls == []  # no new external request was initiated


def test_an_unrevoked_permit_still_dispatches_exactly_once(world) -> None:
    send(world)
    with pytest.raises(ProviderError):
        send(world)  # single use is unchanged
    assert len(world.sdk.calls) == 1


def test_a_committed_revocation_stops_a_previously_issued_permit(world) -> None:
    policies.change_state(
        world.engine, "tenant-a", world.policy.id, "revoke", expected_version=1,
        actor="op", reason="revoked before dispatch",
    )  # fmt: skip
    assert_refused(world)


def test_a_suspension_stops_a_previously_issued_permit(world) -> None:
    policies.change_state(
        world.engine, "tenant-a", world.policy.id, "suspend", expected_version=1,
        actor="op", reason="paused",
    )  # fmt: skip
    assert_refused(world)


def test_a_policy_version_change_stops_a_previously_issued_permit(world) -> None:
    """Suspend and resume: the policy is active again, but not the version that was approved."""
    for action, version in (("suspend", 1), ("resume", 2)):
        policies.change_state(
            world.engine, "tenant-a", world.policy.id, action, expected_version=version,
            actor="op", reason=action,
        )  # fmt: skip
    assert_refused(world)


def test_policy_expiry_stops_a_previously_issued_permit(world) -> None:
    with world.engine.begin() as connection:
        connection.execute(
            update(PrivacyEgressPolicy)
            .where(PrivacyEgressPolicy.id == world.policy.id)
            .values(valid_until=datetime.now(UTC) - timedelta(seconds=1))
        )
    assert_refused(world)


def test_an_unavailable_policy_store_stops_a_previously_issued_permit(world) -> None:
    def broken() -> Any:
        raise OperationalError("SELECT 1", {}, Exception("database down"))

    world.guard._engine = broken  # the store fails after the permit was issued
    assert_refused(world)
