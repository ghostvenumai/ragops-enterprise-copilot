"""Query accounting: idempotency, fallback, reservation reconciliation and pricing."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest

pytest.importorskip("alembic", reason="Install declared persistence extra; product gate blocks")

from tests.security.finops_helpers import (
    migrated_sqlite,
    provision,
    reservations,
    usage_rows,
)

from ragops.auth.identity import AuthenticatedUserContext
from ragops.finops.query_accounting import (
    AccountingNotConfigured,
    QueryAccounting,
    UsageUnavailable,
)
from ragops.governance.audit import AuditLogger
from ragops.llm.providers import LLMResponse, LLMUsage
from ragops.modeling.router import (
    ComplexitySignals,
    LLMModelRouter,
    ModelSpec,
    ProviderError,
    ProviderErrorCategory,
    ProviderRegistry,
    TenantModelPolicy,
)
from ragops.monitoring.metrics import MetricsRegistry
from ragops.retrieval.context import ContextLimits
from ragops.retrieval.vector_retriever import VectorRetriever
from ragops.vector.embedding import EMBEDDING_MODEL, DeterministicEmbeddingProvider
from ragops.vector.index import DeterministicVectorIndex, VectorPayload
from ragops.workflows.vector_query import GenerationUnavailable, VectorQueryService

ISSUER = "https://issuer.synthetic.example/"
TEXT = "Das Orbit-Verfahren regelt die Freigabe von Wartungen am Wochenende."
PRICES = {
    ("primary", "cheap-v1"): (Decimal("0.00001"), Decimal("0.00002")),
    ("secondary", "solid-v1"): (Decimal("0.0001"), Decimal("0.0002")),
}

PRICED_MODELS = tuple((p, m, i, o) for (p, m), (i, o) in PRICES.items())


@dataclass
class FakeAdapter:
    provider_id: str
    model: str
    log: list[str]
    error: ProviderErrorCategory | None = None
    tokens: tuple[int, int] = (120, 30)

    def health(self) -> str:
        return "healthy"

    def generate(self, question, citations, context):
        self.log.append(self.provider_id)
        if self.error is not None:
            raise ProviderError(self.error)
        return LLMResponse(
            f"Antwort [{citations[0].source_id}]",
            LLMUsage(self.tokens[0], self.tokens[1], sum(self.tokens), 0.0, 5.0, self.model),
            used_source_ids=(citations[0].source_id,),
        )


def context(tenant: str = "tenant-a") -> AuthenticatedUserContext:
    return AuthenticatedUserContext(
        user_id=f"user-{tenant}",
        tenant_id=tenant,
        roles=("viewer",),
        issuer=ISSUER,
        subject=f"user-{tenant}",
    )


class World:
    def __init__(self, tmp_path: Path, *, primary_error=None, secondary_error=None, policy=None):
        self.engine, _ = migrated_sqlite(tmp_path / "accounting.db")
        self.log: list[str] = []
        embedding = DeterministicEmbeddingProvider(64)
        index = DeterministicVectorIndex(64)
        index.upsert_chunks([(embedding.embed([TEXT])[0], self._payload())])
        catalog = [
            ModelSpec(
                "primary",
                "cheap-v1",
                "Cheap",
                input_cost_per_token=0.00001,
                output_cost_per_token=0.00002,
            ),
            ModelSpec(
                "secondary",
                "solid-v1",
                "Solid",
                input_cost_per_token=0.0001,
                output_cost_per_token=0.0002,
            ),
        ]
        registry = ProviderRegistry()
        registry.register(FakeAdapter("primary", "cheap-v1", self.log, primary_error))
        registry.register(FakeAdapter("secondary", "solid-v1", self.log, secondary_error))
        self.policies = {"tenant-a": policy} if policy else {}
        self.accounting = QueryAccounting(lambda: self.engine)
        self.audit_path = tmp_path / "audit.jsonl"
        self.service = VectorQueryService(
            VectorRetriever(index, embedding),
            lambda: LLMModelRouter(catalog, self.policies, registry),
            ContextLimits(20, 6000),
            AuditLogger(self.audit_path),
            MetricsRegistry(),
            accounting=self.accounting,
            max_output_tokens=600,
        )

    @staticmethod
    def _payload() -> VectorPayload:
        from datetime import UTC, datetime, timedelta

        now = datetime.now(UTC)
        return VectorPayload(
            "tenant-a",
            "w",
            "c",
            "doc-a",
            "doc-a-v1",
            "doc-a-v1:0",
            "internal",
            "indexed",
            "indexed",
            "hash-a",
            0,
            "orbit.txt",
            now.isoformat(),
            (now - timedelta(minutes=1)).isoformat(),
            None,
            "Orbit",
            None,
            TEXT,
            EMBEDDING_MODEL,
        )

    def provision(self, models=None, **kw):
        models = models if models is not None else PRICED_MODELS
        return provision(
            self.engine,
            "tenant-a",
            issuer=ISSUER,
            subject="user-tenant-a",
            models=models,
            kinds={"primary": "openai", "secondary": "azure_openai"},
            **kw,
        )

    def ask(self):
        return self.service.run(context(), "Was regelt das Orbit-Verfahren?", 5)


def test_allowed_fallback_is_accounted_once_on_the_provider_that_answered(tmp_path) -> None:
    world = World(tmp_path, primary_error=ProviderErrorCategory.RATE_LIMIT)
    world.provision()
    result = world.ask()
    assert world.log == ["primary", "secondary"] and result.abstained is False
    (row,) = usage_rows(world.engine)
    assert (row.provider_id, row.model_id_text, row.fallback_count) == ("secondary", "solid-v1", 1)
    expected = Decimal(120) * Decimal("0.0001") + Decimal(30) * Decimal("0.0002")
    assert row.cost == row.actual_cost == expected
    (held,) = reservations(world.engine)
    assert held.status == "committed" and held.final_amount == expected
    # The reservation covered the most expensive model the chain could reach.
    assert held.estimated_amount >= expected


@pytest.mark.parametrize(
    "category",
    [ProviderErrorCategory.TIMEOUT, ProviderErrorCategory.PROVIDER_ERROR],
)
def test_possibly_billed_failures_do_not_fall_back_and_keep_the_reservation(
    tmp_path, category
) -> None:
    world = World(tmp_path, primary_error=category)
    world.provision()
    with pytest.raises(GenerationUnavailable):
        world.ask()
    assert world.log == ["primary"]
    assert usage_rows(world.engine) == []
    (held,) = reservations(world.engine)
    assert held.status == "reserved"  # unknown provider cost keeps consuming budget


@pytest.mark.parametrize(
    "category",
    [ProviderErrorCategory.AUTHENTICATION, ProviderErrorCategory.INVALID_REQUEST],
)
def test_rejected_requests_do_not_fall_back_and_release_the_reservation(tmp_path, category) -> None:
    world = World(tmp_path, primary_error=category)
    world.provision()
    with pytest.raises(GenerationUnavailable):
        world.ask()
    assert world.log == ["primary"] and usage_rows(world.engine) == []
    (held,) = reservations(world.engine)
    assert held.status == "released"


def test_fallback_never_reaches_a_provider_the_tenant_policy_forbids(tmp_path) -> None:
    policy = TenantModelPolicy("tenant-a", allowed_providers=frozenset({"primary"}))
    world = World(tmp_path, primary_error=ProviderErrorCategory.RATE_LIMIT, policy=policy)
    world.provision()
    with pytest.raises(GenerationUnavailable):
        world.ask()
    assert world.log == ["primary"] and usage_rows(world.engine) == []
    assert reservations(world.engine)[0].status == "released"


def test_unaccountable_fallback_models_are_removed_from_the_chain(tmp_path) -> None:
    world = World(tmp_path, primary_error=ProviderErrorCategory.RATE_LIMIT)
    world.provision(models=(("primary", "cheap-v1", Decimal("0.00001"), Decimal("0.00002")),))
    with pytest.raises(GenerationUnavailable):
        world.ask()
    assert world.log == ["primary"]  # the unconfigured secondary model is never executed
    assert usage_rows(world.engine) == []


def test_finalization_is_idempotent_per_logical_query(tmp_path) -> None:
    world = World(tmp_path)
    world.provision()
    router = LLMModelRouter(
        [
            ModelSpec(
                "primary",
                "cheap-v1",
                "Cheap",
                input_cost_per_token=0.00001,
                output_cost_per_token=0.00002,
            )
        ]
    )
    decision = router.route("tenant-a", ComplexitySignals(estimated_input_tokens=100))
    correlation = uuid4()
    ticket = world.accounting.prepare(
        context(), decision, input_tokens=100, output_tokens=600, correlation_id=correlation
    )
    assert (
        world.accounting.prepare(
            context(), decision, input_tokens=100, output_tokens=600, correlation_id=correlation
        ).reservation_id
        == ticket.reservation_id
    )
    usage = LLMUsage(100, 20, 120, 0.0, 3.0, "cheap-v1")
    first = world.accounting.finalize(ticket, "primary", "cheap-v1", usage, 0, 3.0)
    second = world.accounting.finalize(ticket, "primary", "cheap-v1", usage, 0, 3.0)
    assert first == second
    (row,) = usage_rows(world.engine)
    assert (
        row.cost
        == first.cost
        == Decimal(100) * Decimal("0.00001") + Decimal(20) * Decimal("0.00002")
    )
    (held,) = reservations(world.engine)
    assert held.status == "committed" and held.final_amount == row.cost


def test_unreported_usage_is_rejected_before_any_write(tmp_path) -> None:
    world = World(tmp_path)
    world.provision()
    router = LLMModelRouter(
        [ModelSpec("primary", "cheap-v1", "Cheap", input_cost_per_token=0.00001)]
    )
    decision = router.route("tenant-a", ComplexitySignals(estimated_input_tokens=10))
    ticket = world.accounting.prepare(
        context(), decision, input_tokens=10, output_tokens=600, correlation_id=uuid4()
    )
    with pytest.raises(UsageUnavailable):
        world.accounting.finalize(
            ticket, "primary", "cheap-v1", LLMUsage(0, 0, 0, 0.0, 1.0, "x", reported=False), 0, 1
        )
    assert usage_rows(world.engine) == [] and reservations(world.engine)[0].status == "reserved"


def test_accounting_rejects_a_context_of_another_tenant(tmp_path) -> None:
    world = World(tmp_path)
    world.provision()
    router = LLMModelRouter([ModelSpec("primary", "cheap-v1", "Cheap")])
    decision = router.route("tenant-a", ComplexitySignals())
    with pytest.raises(AccountingNotConfigured):
        world.accounting.prepare(
            context("tenant-b"), decision, input_tokens=1, output_tokens=1, correlation_id=uuid4()
        )
    assert reservations(world.engine) == []
