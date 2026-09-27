from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from uuid import uuid4

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session

from ragops.modeling.router import ModelSpec, RoutingClass, RoutingDecision
from ragops.modeling.usage import UsageService, estimate_cost
from ragops.persistence.models import ModelConfiguration, ProviderConfiguration, Tenant, User


def test_cost_estimate_and_usage_record(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'usage.db'}")

    @event.listens_for(engine, "connect")
    def fk(connection: object, _: object) -> None:
        connection.execute("PRAGMA foreign_keys=ON")

    with engine.begin() as connection:
        cfg = Config("alembic.ini")
        cfg.attributes["connection"] = connection
        command.upgrade(cfg, "head")
    with Session(engine) as session:
        tenant = Tenant(id="tenant-a", name="A")
        user = User(tenant_id="tenant-a", issuer="test", subject="u")
        provider = ProviderConfiguration(tenant_id="tenant-a", name="p", kind="test")
        session.add(tenant)
        session.flush()
        session.add_all([user, provider])
        session.flush()
        model = ModelConfiguration(
            tenant_id="tenant-a",
            provider_id=provider.id,
            name="m",
            input_price=Decimal("0"),
            output_price=Decimal("0"),
        )
        session.add(model)
        session.flush()
        spec = ModelSpec("p", "m", "M", input_cost_per_token=0.01, output_cost_per_token=0.02)
        decision = RoutingDecision(
            RoutingClass.SIMPLE,
            "p",
            "m",
            ("SIMPLE_FACTUAL_QUERY",),
            10,
            20,
            estimate_cost(spec, 10, 20),
        )
        record = UsageService(session, "tenant-a", user.id, model.id).record(
            decision,
            input_tokens=10,
            output_tokens=20,
            actual_cost=0.5,
            latency_ms=2.0,
            correlation_id=uuid4(),
        )
        assert record.total_tokens == 30 and record.estimated_cost == Decimal("0.5")
    engine.dispose()
