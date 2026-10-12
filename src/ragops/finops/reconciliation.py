"""Reservation reconciliation for the productive query path (ENT-11.6).

A query reservation that is still ``reserved`` after its request ended is a reconciliation
case. Its classification follows only from durable PostgreSQL state of one tenant: the
reservation, the execution ledger (``QueryExecution``), the usage record and the idempotency
record of the same logical correlation. Nothing here can call a provider.

* ``already_accounted`` – a usage record exists: it is authoritative; the reservation is
  committed with its cost and no second usage record is written.
* ``receipt_available`` – the provider answered and its reported usage, priced with the
  execution-time snapshot, is durable: it is booked exactly once as the workflow would have.
* ``pre_provider`` – the ledger proves the provider boundary was never crossed: the
  reservation is released and an executing idempotency record becomes ``retryable``.
* ``unknown_billing`` – the provider may have run and no receipt exists (timeouts, ambiguous
  errors, unreported usage, crashes after the marker, ENT-11.5 holds without a ledger): the
  hold stays. Age is never evidence of non-billing; provider-native billing evidence would
  be a later extension.
* ``invalid`` – contradictory or forged state (a receipt whose cost does not follow from its
  own tokens and prices, a ledger detached from its reservation, usage that contradicts the
  receipt, closed reservations with open ledgers): nothing is changed.
* ``in_flight`` – younger than the grace period; a live request may still settle it. The
  grace period protects liveness only; safety comes from the locked, conditional writes.

Every action re-reads and re-classifies the case under row locks (reservation first, then
ledger, the same order as the workflow) inside one transaction, so concurrent reconcilers
produce at most one financial resolution; a datastore failure rolls back and changes nothing.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from ragops.finops.query_accounting import (
    OPEN_EXECUTION_STATES,
    AccountingError,
    ExecutionState,
    parse_reservation_key,
    receipt_cost,
    settle_receipt,
)
from ragops.finops.reservation import BudgetReservationService, ReservationError
from ragops.modeling.usage import UsageService
from ragops.persistence.database import transaction
from ragops.persistence.models import (
    BudgetReservation,
    QueryExecution,
    QueryIdempotencyRecord,
    UsageRecord,
)
from ragops.workflows.query_idempotency import (
    DEFAULT_LEASE_SECONDS,
    DEFAULT_TTL_SECONDS,
    MAX_TTL_SECONDS,
    MIN_TTL_SECONDS,
    RECONCILED_DETAIL,
    RECONCILED_STATUS,
    IdempotencyState,
)

__all__ = [
    "AUTO_SAFE",
    "Case",
    "CaseNotFound",
    "Classification",
    "Outcome",
    "ReconciliationService",
    "ReconciliationUnavailable",
    "SQLAlchemyError",
    "UsageService",
]

MAX_GRACE_SECONDS = 86_400
MAX_BATCH = 500
QUERY_KEY_PREFIX = "query:"


class Classification(StrEnum):
    RESOLVED = "resolved"
    ALREADY_ACCOUNTED = "already_accounted"
    RECEIPT_AVAILABLE = "receipt_available"
    PRE_PROVIDER = "pre_provider"
    UNKNOWN_BILLING = "unknown_billing"
    INVALID = "invalid"
    IN_FLIGHT = "in_flight"


AUTO_SAFE = frozenset(
    {
        Classification.ALREADY_ACCOUNTED,
        Classification.RECEIPT_AVAILABLE,
        Classification.PRE_PROVIDER,
    }
)
REVIEW = frozenset({Classification.UNKNOWN_BILLING, Classification.INVALID})
_I = IdempotencyState
# Idempotency states that agree with an open ledger row; anything else is a contradiction.
_COMPATIBLE = {
    ExecutionState.PREPARED.value: {_I.CLAIMED, _I.EXECUTING, _I.RETRYABLE},
    ExecutionState.PROVIDER_MAY_RUN.value: {_I.EXECUTING, _I.AMBIGUOUS_HELD},
    ExecutionState.RECEIPT_RECORDED.value: {_I.EXECUTING, _I.AMBIGUOUS_HELD},
}
_SETTLEABLE_IDEMPOTENCY = (_I.EXECUTING.value, _I.AMBIGUOUS_HELD.value)


class CaseNotFound(LookupError):
    """No query reservation of this tenant matches; other tenants are never consulted."""


class ReconciliationUnavailable(RuntimeError):
    """The datastore failed; the transaction was rolled back and nothing changed."""


@dataclass(frozen=True)
class Case:
    """Identifiers, states and counts of one case; never content, keys or credentials."""

    reservation_id: UUID
    correlation_id: UUID | None
    attempt: int | None
    classification: Classification
    reason: str
    reservation_status: str
    ledger_state: str | None
    usage_recorded: bool
    idempotency_state: str | None
    ambiguity_reason: str | None
    legacy: bool
    age_seconds: int

    @property
    def review_required(self) -> bool:
        return self.classification in REVIEW

    @property
    def auto_safe(self) -> bool:
        return self.classification in AUTO_SAFE

    def as_dict(self) -> dict[str, Any]:
        return {
            "reservation_id": str(self.reservation_id),
            "correlation_id": str(self.correlation_id) if self.correlation_id else None,
            "attempt": self.attempt,
            "classification": self.classification.value,
            "reason": self.reason,
            "auto_safe": self.auto_safe,
            "review_required": self.review_required,
            "reservation_status": self.reservation_status,
            "ledger_state": self.ledger_state,
            "usage_recorded": self.usage_recorded,
            "idempotency_state": self.idempotency_state,
            "ambiguity_reason": self.ambiguity_reason,
            "legacy": self.legacy,
            "age_seconds": self.age_seconds,
        }


@dataclass(frozen=True)
class Outcome:
    reservation_id: UUID
    classification: Classification
    action: str  # committed | released | none
    changed: bool
    reason: str

    @property
    def review_required(self) -> bool:
        return self.classification in REVIEW

    def as_dict(self) -> dict[str, Any]:
        return {
            "reservation_id": str(self.reservation_id),
            "classification": self.classification.value,
            "action": self.action,
            "changed": self.changed,
            "review_required": self.review_required,
            "reason": self.reason,
        }


@dataclass
class _Evidence:
    reservation: BudgetReservation
    correlation_id: UUID | None
    attempt: int | None
    execution: QueryExecution | None
    usage: UsageRecord | None
    idempotency: list[QueryIdempotencyRecord]


def _utc(value: datetime) -> datetime:
    # SQLite returns naive datetimes for timezone-aware columns; they are stored as UTC.
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _money(value: Any) -> Decimal | None:
    return None if value is None else Decimal(value)


def classify(evidence: _Evidence, now: datetime, grace: timedelta) -> tuple[Classification, str]:
    """Deterministic classification of one case from durable state only."""
    reservation, execution, usage = evidence.reservation, evidence.execution, evidence.usage
    if evidence.correlation_id is None:
        return Classification.INVALID, "reservation key is not a query key"
    if len(evidence.idempotency) > 1:
        return Classification.INVALID, "several idempotency records share the correlation"
    idempotency = evidence.idempotency[0].state if evidence.idempotency else None
    if execution is not None and (
        execution.reservation_id != reservation.id
        or execution.correlation_id != evidence.correlation_id
        or execution.attempt != evidence.attempt
    ):
        return Classification.INVALID, "ledger does not match its reservation"

    # ---------------------------------------------------------------- terminal states
    if execution is not None and execution.state == ExecutionState.RELEASED.value:
        if reservation.status == "released":
            return Classification.RESOLVED, "released"
        return Classification.INVALID, "released ledger with an unreleased reservation"
    if execution is not None and execution.state == ExecutionState.ACCOUNTED.value:
        if (
            reservation.status == "committed"
            and usage is not None
            and _money(reservation.final_amount) == _money(usage.cost)
        ):
            return Classification.RESOLVED, "accounted"
        return Classification.INVALID, "accounted ledger without matching usage and commit"
    if reservation.status != "reserved":
        if execution is not None:
            return Classification.INVALID, "open ledger with a closed reservation"
        if reservation.status == "committed" and (
            usage is None or _money(reservation.final_amount) != _money(usage.cost)
        ):
            return Classification.INVALID, "committed reservation without matching usage"
        return Classification.RESOLVED, reservation.status

    # ---------------------------------------------------------------- open: reserved
    if execution is not None and idempotency is not None:
        if _I(idempotency) not in _COMPATIBLE[execution.state]:
            return Classification.INVALID, "idempotency state contradicts the ledger"
    if usage is not None:
        if execution is not None and execution.state == ExecutionState.PREPARED.value:
            return Classification.INVALID, "usage booked before the provider boundary"
        if execution is not None and execution.state == ExecutionState.RECEIPT_RECORDED.value:
            mismatch = (
                usage.input_tokens != execution.input_tokens
                or usage.output_tokens != execution.output_tokens
                or _money(usage.cost) != _money(execution.cost)
                or usage.model_id != execution.model_configuration_id
            )
            if mismatch:
                return Classification.INVALID, "usage contradicts the receipt"
        if usage.user_id is None or _money(usage.cost) is None:
            return Classification.INVALID, "usage record is incomplete"
        return _fresh(execution, now, grace) or (
            Classification.ALREADY_ACCOUNTED,
            "usage record is authoritative",
        )
    if execution is None:
        return Classification.UNKNOWN_BILLING, "legacy hold without provider-boundary evidence"
    if execution.state == ExecutionState.PREPARED.value:
        return _fresh(execution, now, grace) or (
            Classification.PRE_PROVIDER,
            "provider boundary was never crossed",
        )
    if execution.state == ExecutionState.PROVIDER_MAY_RUN.value:
        return (
            Classification.UNKNOWN_BILLING,
            execution.ambiguity_reason or "provider may have run without a receipt",
        )
    # receipt_recorded: the receipt must explain its own cost with its own price snapshot.
    expected = receipt_cost(execution)
    if expected is None or _money(execution.cost) != expected:
        return Classification.INVALID, "receipt cost does not follow from tokens and prices"
    return _fresh(execution, now, grace) or (
        Classification.RECEIPT_AVAILABLE,
        "durable usage receipt without usage record",
    )


def _fresh(
    execution: QueryExecution | None, now: datetime, grace: timedelta
) -> tuple[Classification, str] | None:
    if execution is not None and now - _utc(execution.updated_at) < grace:
        return Classification.IN_FLIGHT, "younger than the grace period"
    return None


class ReconciliationService:
    """Operator-side reconciliation inside exactly one tenant; never invokes a provider."""

    def __init__(
        self,
        engine: Callable[[], Engine],
        tenant_id: str,
        *,
        grace_seconds: int = DEFAULT_LEASE_SECONDS,
        idempotency_ttl_seconds: int = DEFAULT_TTL_SECONDS,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        if not tenant_id:
            raise ValueError("tenant is required")
        if not 0 <= grace_seconds <= MAX_GRACE_SECONDS:
            raise ValueError("grace period is out of bounds")
        if not MIN_TTL_SECONDS <= idempotency_ttl_seconds <= MAX_TTL_SECONDS:
            raise ValueError("idempotency retention is out of bounds")
        self.engine, self.tenant_id, self.clock = engine, tenant_id, clock
        self.grace = timedelta(seconds=grace_seconds)
        self.ttl = timedelta(seconds=idempotency_ttl_seconds)

    # ------------------------------------------------------------------ read

    def cases(self, limit: int = 100) -> list[Case]:
        """Open query reservations and open ledgers of this tenant, oldest first."""
        if not 1 <= limit <= MAX_BATCH:
            raise ValueError("limit is out of bounds")
        try:
            with transaction(self.engine()) as session:
                tenant = self.tenant_id
                held = select(BudgetReservation.id, BudgetReservation.created_at).where(
                    BudgetReservation.tenant_id == tenant,
                    BudgetReservation.status == "reserved",
                    BudgetReservation.idempotency_key.startswith(QUERY_KEY_PREFIX),
                )
                open_ledger = select(
                    QueryExecution.reservation_id, QueryExecution.created_at
                ).where(
                    QueryExecution.tenant_id == tenant,
                    QueryExecution.state.in_([state.value for state in OPEN_EXECUTION_STATES]),
                )
                found: dict[UUID, datetime] = {}
                for key, created in [*session.execute(held), *session.execute(open_ledger)]:
                    found.setdefault(key, _utc(created))
                ordered = sorted(found, key=lambda key: (found[key], str(key)))[:limit]
                return [self._case(session, self._evidence(session, key)) for key in ordered]
        except (SQLAlchemyError, OSError) as exc:
            raise ReconciliationUnavailable("reconciliation datastore unavailable") from exc

    def inspect(self, identifier: UUID) -> Case:
        try:
            with transaction(self.engine()) as session:
                key = self._locate(session, identifier)
                return self._case(session, self._evidence(session, key))
        except (SQLAlchemyError, OSError) as exc:
            raise ReconciliationUnavailable("reconciliation datastore unavailable") from exc

    # ------------------------------------------------------------------ write

    def reconcile(self, identifier: UUID) -> Outcome:
        """Classify under lock and perform the one safe action, if any; idempotent."""
        try:
            with transaction(self.engine()) as session:
                key = self._locate(session, identifier)
                evidence = self._evidence(session, key, lock=True)
                now = self.clock()
                classification, reason = classify(evidence, now, self.grace)
                action = self._act(session, evidence, classification, now)
                return Outcome(key, classification, action, action != "none", reason)
        except (CaseNotFound, ValueError):
            raise
        except (SQLAlchemyError, ReservationError, AccountingError, OSError) as exc:
            raise ReconciliationUnavailable("reconciliation could not be completed") from exc

    def reconcile_auto(self, limit: int = 50) -> list[Outcome]:
        """Act on at most ``limit`` cases that durable evidence makes safe; skip the rest."""
        if not 1 <= limit <= MAX_BATCH:
            raise ValueError("limit is out of bounds")
        safe = [case for case in self.cases(MAX_BATCH) if case.auto_safe][:limit]
        return [self.reconcile(case.reservation_id) for case in safe]

    # ------------------------------------------------------------------ internals

    def _locate(self, session: Session, identifier: UUID) -> UUID:
        """Reservation, ledger or correlation id of this tenant's query; nothing else."""
        if not isinstance(identifier, UUID):
            raise CaseNotFound("case not found")
        tenant = self.tenant_id
        reservation = session.scalar(
            select(BudgetReservation.id).where(
                BudgetReservation.tenant_id == tenant,
                BudgetReservation.id == identifier,
                BudgetReservation.idempotency_key.startswith(QUERY_KEY_PREFIX),
            )
        )
        if reservation is not None:
            return reservation
        by_ledger = session.scalar(
            select(QueryExecution.reservation_id)
            .where(
                QueryExecution.tenant_id == tenant,
                (QueryExecution.id == identifier) | (QueryExecution.correlation_id == identifier),
            )
            .order_by(QueryExecution.attempt.desc())
            .limit(1)
        )
        if by_ledger is not None:
            return by_ledger
        legacy = session.scalar(
            select(BudgetReservation.id).where(
                BudgetReservation.tenant_id == tenant,
                BudgetReservation.idempotency_key == f"{QUERY_KEY_PREFIX}{identifier}",
            )
        )
        if legacy is not None:
            return legacy
        raise CaseNotFound("case not found")

    def _evidence(self, session: Session, key: UUID, *, lock: bool = False) -> _Evidence:
        tenant = self.tenant_id
        query = select(BudgetReservation).where(
            BudgetReservation.tenant_id == tenant, BudgetReservation.id == key
        )
        reservation = session.scalar(query.with_for_update() if lock else query)
        if reservation is None:
            raise CaseNotFound("case not found")
        parsed = parse_reservation_key(reservation.idempotency_key)
        correlation, attempt = parsed if parsed is not None else (None, None)
        ledger = select(QueryExecution).where(
            QueryExecution.tenant_id == tenant, QueryExecution.reservation_id == reservation.id
        )
        execution = session.scalar(ledger.with_for_update() if lock else ledger)
        usage = None
        idempotency: list[QueryIdempotencyRecord] = []
        if correlation is not None:
            usage = session.scalar(
                select(UsageRecord).where(
                    UsageRecord.tenant_id == tenant, UsageRecord.correlation_id == correlation
                )
            )
            records = select(QueryIdempotencyRecord).where(
                QueryIdempotencyRecord.tenant_id == tenant,
                QueryIdempotencyRecord.correlation_id == correlation,
            )
            idempotency = list(session.scalars(records.with_for_update() if lock else records))
        return _Evidence(reservation, correlation, attempt, execution, usage, idempotency)

    def _case(self, session: Session, evidence: _Evidence) -> Case:
        now = self.clock()
        classification, reason = classify(evidence, now, self.grace)
        execution = evidence.execution
        since = _utc(execution.updated_at if execution else evidence.reservation.created_at)
        return Case(
            reservation_id=evidence.reservation.id,
            correlation_id=evidence.correlation_id,
            attempt=evidence.attempt,
            classification=classification,
            reason=reason,
            reservation_status=evidence.reservation.status,
            ledger_state=execution.state if execution else None,
            usage_recorded=evidence.usage is not None,
            idempotency_state=evidence.idempotency[0].state if evidence.idempotency else None,
            ambiguity_reason=execution.ambiguity_reason if execution else None,
            legacy=execution is None,
            age_seconds=max(0, int((now - since).total_seconds())),
        )

    def _act(
        self,
        session: Session,
        evidence: _Evidence,
        classification: Classification,
        now: datetime,
    ) -> str:
        reservation, execution = evidence.reservation, evidence.execution
        if classification is Classification.RECEIPT_AVAILABLE:
            assert execution is not None
            settle_receipt(session, execution, reservation, now)
            self._resolve(execution, classification, now)
            self._settle_idempotency(evidence, now)
            return "committed"
        if classification is Classification.ALREADY_ACCOUNTED:
            assert evidence.usage is not None
            BudgetReservationService(session, self.tenant_id).commit(
                reservation, Decimal(evidence.usage.cost)
            )
            if execution is not None:
                execution.state = ExecutionState.ACCOUNTED.value
                self._resolve(execution, classification, now)
            self._settle_idempotency(evidence, now)
            session.flush()
            return "committed"
        if classification is Classification.PRE_PROVIDER:
            assert execution is not None
            BudgetReservationService(session, self.tenant_id).release(reservation)
            execution.state = ExecutionState.RELEASED.value
            self._resolve(execution, classification, now)
            for record in evidence.idempotency:
                if record.state == _I.EXECUTING.value:
                    record.state = _I.RETRYABLE.value
                    record.status_code, record.response = None, None
                    record.updated_at, record.expires_at = now, now + self.ttl
            session.flush()
            return "released"
        return "none"

    @staticmethod
    def _resolve(execution: QueryExecution, classification: Classification, now: datetime) -> None:
        execution.resolution = classification.value
        execution.resolved_at = execution.updated_at = now

    def _settle_idempotency(self, evidence: _Evidence, now: datetime) -> None:
        """A billed logical query never executes again: its key replays a stable 409."""
        for record in evidence.idempotency:
            if record.state in _SETTLEABLE_IDEMPOTENCY:
                record.state = _I.RECONCILED.value
                record.status_code = RECONCILED_STATUS
                record.response = {"detail": RECONCILED_DETAIL}
                record.updated_at, record.expires_at = now, now + self.ttl
