"""Synthetic accounting fixtures for the productive query path (users, models, budgets)."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from ragops.persistence.database import transaction
from ragops.persistence.models import (
    BudgetReservation,
    ModelConfiguration,
    ProviderConfiguration,
    Tenant,
    TenantBudget,
    UsageRecord,
    User,
)

DETERMINISTIC_MODEL = "deterministic-ragops-v1"
INPUT_PRICE = Decimal("0.00001")
OUTPUT_PRICE = Decimal("0.00002")


def migrated_sqlite(path: Path) -> tuple[Engine, str]:
    url = f"sqlite:///{path}"
    engine = create_engine(url)
    with engine.begin() as connection:
        cfg = Config("alembic.ini")
        cfg.attributes["connection"] = connection
        command.upgrade(cfg, "head")
    return engine, url


def month(today: date | None = None) -> tuple[date, date]:
    today = today or datetime.now(UTC).date()
    start = today.replace(day=1)
    return start, (start + timedelta(days=32)).replace(day=1)


def provision(
    engine: Engine,
    tenant: str,
    *,
    issuer: str,
    subject: str,
    models: tuple[tuple[str, str, Decimal, Decimal], ...] = (
        ("deterministic", DETERMINISTIC_MODEL, INPUT_PRICE, OUTPUT_PRICE),
    ),
    kinds: dict[str, str] | None = None,
    budget: Decimal | None = Decimal("100"),
    mode: str = "hard_limit",
    user: bool = True,
) -> dict[str, UUID]:
    """Tenant, user, provider/model configurations and a current-month budget."""
    ids: dict[str, UUID] = {}
    with transaction(engine) as session:
        if session.get(Tenant, tenant) is None:
            session.add(Tenant(id=tenant, name=tenant))
            session.flush()
        if user:
            row = User(tenant_id=tenant, issuer=issuer, subject=subject)
            session.add(row)
            session.flush()
            ids["user"] = row.id
        providers: dict[str, ProviderConfiguration] = {}
        for provider_id, model_id, input_price, output_price in models:
            if provider_id not in providers:
                providers[provider_id] = ProviderConfiguration(
                    tenant_id=tenant,
                    name=provider_id,
                    kind=(kinds or {}).get(provider_id, "deterministic"),
                )
                session.add(providers[provider_id])
                session.flush()
            model = ModelConfiguration(
                tenant_id=tenant,
                provider_id=providers[provider_id].id,
                name=model_id,
                input_price=input_price,
                output_price=output_price,
            )
            session.add(model)
            session.flush()
            ids[f"model:{provider_id}:{model_id}"] = model.id
        if budget is not None:
            start, end = month()
            row = TenantBudget(
                tenant_id=tenant,
                period_start=start,
                period_end=end,
                budget_amount=budget,
                hard_limit_threshold=Decimal("1"),
                enforcement_mode=mode,
            )
            session.add(row)
            session.flush()
            ids["budget"] = row.id
    return ids


def exhaust_budget(engine: Engine, tenant: str, ids: dict[str, UUID], amount: Decimal) -> None:
    """Book prior spend so that the hard limit is already reached."""
    model_id = next(value for key, value in ids.items() if key.startswith("model:"))
    with transaction(engine) as session:
        session.add(
            UsageRecord(
                tenant_id=tenant,
                user_id=ids["user"],
                model_id=model_id,
                correlation_id=uuid4(),
                input_tokens=1,
                output_tokens=1,
                cost=amount,
                latency_ms=1.0,
                endpoint="rag",
                workflow="query",
            )
        )


def usage_rows(engine: Engine, tenant: str | None = None) -> list[UsageRecord]:
    with Session(engine) as session:
        query = select(UsageRecord).order_by(UsageRecord.created_at)
        if tenant is not None:
            query = query.where(UsageRecord.tenant_id == tenant)
        return list(session.scalars(query))


def reservations(engine: Engine, tenant: str | None = None) -> list[BudgetReservation]:
    with Session(engine) as session:
        query = select(BudgetReservation)
        if tenant is not None:
            query = query.where(BudgetReservation.tenant_id == tenant)
        return list(session.scalars(query))
