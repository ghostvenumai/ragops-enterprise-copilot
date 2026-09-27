"""Normalized usage and cost accounting service."""

from __future__ import annotations

from decimal import Decimal
from uuid import UUID

from sqlalchemy.orm import Session

from ragops.modeling.router import ModelSpec, RoutingDecision
from ragops.persistence.models import UsageRecord


def estimate_cost(model: ModelSpec, input_tokens: int, output_tokens: int) -> float:
    if input_tokens < 0 or output_tokens < 0:
        raise ValueError("token counts cannot be negative")
    return model.estimated_cost(input_tokens, output_tokens)


class UsageService:
    def __init__(
        self,
        session: Session,
        tenant_id: str,
        user_id: UUID | None = None,
        model_id: UUID | None = None,
    ) -> None:
        if not tenant_id:
            raise ValueError("tenant is required")
        self.session, self.tenant_id, self.user_id, self.model_id = (
            session,
            tenant_id,
            user_id,
            model_id,
        )

    def record(
        self,
        decision: RoutingDecision,
        *,
        input_tokens: int,
        output_tokens: int,
        actual_cost: float | None,
        latency_ms: float,
        correlation_id: UUID,
        fallback_count: int = 0,
    ) -> UsageRecord:
        if input_tokens < 0 or output_tokens < 0 or latency_ms < 0 or fallback_count < 0:
            raise ValueError("usage values cannot be negative")
        record = UsageRecord(
            tenant_id=self.tenant_id,
            user_id=self.user_id or UUID(int=0),
            model_id=self.model_id or UUID(int=0),
            correlation_id=correlation_id,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost=Decimal(str(actual_cost if actual_cost is not None else decision.estimated_cost)),
            latency_ms=latency_ms,
            endpoint="rag",
            workflow="query",
            provider_id=decision.provider_id,
            model_id_text=decision.model_id,
            routing_class=decision.routing_class.value,
            routing_reason=",".join(decision.reason_codes),
            total_tokens=input_tokens + output_tokens,
            estimated_cost=Decimal(str(decision.estimated_cost)),
            actual_cost=Decimal(str(actual_cost)) if actual_cost is not None else None,
            fallback_count=fallback_count,
            request_id=str(correlation_id),
        )
        self.session.add(record)
        self.session.flush()
        return record
