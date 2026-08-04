"""Audit logging with redaction."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from ragops.security.pii import mask_pii
from ragops.storage.models import AuditEvent, QueryUser


class AuditLogger:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def record(
        self,
        *,
        correlation_id: str,
        event_type: str,
        user: QueryUser,
        outcome: str,
        details: dict[str, str | int | float | bool],
    ) -> AuditEvent:
        clean_details = {
            key: mask_pii(value) if isinstance(value, str) else value
            for key, value in details.items()
        }
        event = AuditEvent(
            event_id=str(uuid4()),
            correlation_id=correlation_id,
            event_type=event_type,
            tenant_id=user.tenant_id,
            user_id=user.user_id,
            timestamp=datetime.now(UTC),
            outcome=outcome,
            details=clean_details,
        )
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps(
                    {
                        "event_id": event.event_id,
                        "correlation_id": event.correlation_id,
                        "event_type": event.event_type,
                        "tenant_id": event.tenant_id,
                        "user_id": event.user_id,
                        "timestamp": event.timestamp.isoformat(),
                        "outcome": event.outcome,
                        "details": event.details,
                    },
                    sort_keys=True,
                )
                + "\n"
            )
        return event
