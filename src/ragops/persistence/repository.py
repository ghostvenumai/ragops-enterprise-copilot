"""Bounded tenant-scoped repository for the production schema foundation."""

from __future__ import annotations

from typing import TypeVar
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from ragops.persistence.models import Base, TenantOwned

Record = TypeVar("Record", bound=TenantOwned)


class TenantRepository:
    """Requires an authoritative tenant from the future identity service.

    No bypass for administrators, no client-supplied ORM filters and no generic
    update/delete of historical records. Caller owns the transaction boundary.
    This repository is deliberately not wired to the unauthenticated demo API.
    """

    def __init__(self, session: Session, tenant_id: str) -> None:
        if not tenant_id or len(tenant_id) > 64:
            raise ValueError("A tenant boundary is required")
        self.session = session
        self.tenant_id = tenant_id

    def get(self, model: type[Record], record_id: UUID) -> Record | None:
        self._check_model(model)
        return self.session.scalar(
            select(model).where(model.tenant_id == self.tenant_id, model.id == record_id)
        )

    def list(self, model: type[Record], *, limit: int = 100) -> list[Record]:
        self._check_model(model)
        if not 1 <= limit <= 200:
            raise ValueError("Repository limit must be between 1 and 200")
        return list(
            self.session.scalars(
                select(model)
                .where(model.tenant_id == self.tenant_id)
                .order_by(model.created_at, model.id)
                .limit(limit)
            )
        )

    def add(self, record: Record) -> Record:
        self._check_model(type(record))
        if record.tenant_id != self.tenant_id:
            raise ValueError("Cross-tenant write denied")
        self.session.add(record)
        self.session.flush()
        return record

    @staticmethod
    def _check_model(model: type[Record]) -> None:
        if not issubclass(model, Base) or not issubclass(model, TenantOwned):
            raise TypeError("Only tenant-owned records are supported")
