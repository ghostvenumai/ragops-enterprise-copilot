"""Durable idempotency for the productive query path.

A client that repeats ``POST /v1/query`` with the same ``Idempotency-Key`` repeats one
logical query. The scope is the verified tenant, a digest of the authenticated principal,
the endpoint and an HMAC-SHA256 of the key under a server secret
(``RAGOPS_IDEMPOTENCY_HMAC_KEY``); the raw key is never stored or logged, and a database dump
alone does not allow guessing predictable keys offline. Request fingerprints stay plain
SHA-256: they are not secret and only decide request equality. PostgreSQL is the
authority: a unique constraint over the scope lets exactly one request claim it.

States (``TRANSITIONS`` lists every allowed write):

* ``claimed`` – the owner runs retrieval, context building and routing; nothing is billed.
  A claim whose lease ran out may be taken over; the old owner is fenced by ``attempt``.
* ``executing`` – the owner crossed the billing boundary (budget reservation, provider).
  It never becomes executable again; after its lease it needs reconciliation.
* ``succeeded`` – the response envelope is replayed until the record expires.
* ``retryable`` – failed before any provider call; the same key executes again with the
  same logical correlation id.
* ``failed`` – a deterministic failure (for example a provider rejection that was certainly
  not billed) is replayed until the record expires.
* ``ambiguous_held`` – the provider may have billed and the outcome was not booked; the
  reservation stays held and the key never executes again (operator reconciliation).

Expired ``succeeded``, ``failed`` and ``retryable`` records start a new logical execution
and are purged; ``claimed``, ``executing`` and ``ambiguous_held`` records never expire on
their own. This bounds duplicate provider execution to at most one per key and retention
window; it is not a global exactly-once guarantee and it does not deduplicate requests that
use different keys.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import unicodedata
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import delete, select, update
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from ragops.auth.identity import AuthenticatedUserContext
from ragops.config.settings import validate_idempotency_secret
from ragops.modeling.router import ProviderErrorCategory
from ragops.persistence.database import transaction
from ragops.persistence.models import QueryIdempotencyRecord
from ragops.workflows.vector_query import NOT_BILLED, ExecutionStage

ENDPOINT = "POST /v1/query"
HEADER = "Idempotency-Key"
REPLAY_HEADER = "Idempotency-Replayed"
SCOPE_DESCRIPTION = "tenant+principal+endpoint+key_hash"
FINGERPRINT_ALGORITHM = "sha256/canonical-json/v1"
KEY_DIGEST_ALGORITHM = "HMAC-SHA256"
# One day covers client and gateway retry windows; the bounds keep retention finite.
DEFAULT_TTL_SECONDS = 86_400
MIN_TTL_SECONDS, MAX_TTL_SECONDS = 300, 604_800
DEFAULT_LEASE_SECONDS = 300
_KEY = re.compile(r"[A-Za-z0-9._~:+/=-]{1,128}")


class IdempotencyState(StrEnum):
    CLAIMED = "claimed"
    EXECUTING = "executing"
    SUCCEEDED = "succeeded"
    RETRYABLE = "retryable"
    FAILED = "failed"
    AMBIGUOUS_HELD = "ambiguous_held"


_S = IdempotencyState
TRANSITIONS: dict[IdempotencyState, frozenset[IdempotencyState]] = {
    _S.CLAIMED: frozenset({_S.CLAIMED, _S.EXECUTING, _S.SUCCEEDED, _S.RETRYABLE, _S.FAILED}),
    _S.EXECUTING: frozenset({_S.SUCCEEDED, _S.RETRYABLE, _S.FAILED, _S.AMBIGUOUS_HELD}),
    _S.SUCCEEDED: frozenset({_S.CLAIMED}),  # only after expiry, as a new logical query
    _S.FAILED: frozenset({_S.CLAIMED}),  # only after expiry, as a new logical query
    _S.RETRYABLE: frozenset({_S.CLAIMED}),
    _S.AMBIGUOUS_HELD: frozenset(),
}
_FINISHED = (_S.SUCCEEDED, _S.FAILED, _S.RETRYABLE)


class Decision(StrEnum):
    EXECUTE = "execute"
    REPLAY = "replay"
    CONFLICT = "conflict"
    IN_PROGRESS = "in_progress"
    RECONCILIATION_REQUIRED = "reconciliation_required"


class InvalidIdempotencyKey(ValueError):
    pass


class IdempotencyUnavailable(RuntimeError):
    """The durable store could not be read or written; the request fails closed."""


class IdempotencyLost(RuntimeError):
    """The record is no longer owned by this attempt or not in a state that allows the write."""


def parse_key(values: Sequence[str]) -> str | None:
    """The header is optional; when present it is one bounded, opaque ASCII token."""
    if not values:
        return None
    if len(values) != 1 or not _KEY.fullmatch(values[0]):
        raise InvalidIdempotencyKey("invalid idempotency key")
    return values[0]


def _digest(domain: str, *parts: str) -> str:
    return hashlib.sha256("\0".join((domain, *parts)).encode()).hexdigest()


class KeyDigest:
    """HMAC-SHA256 of a client key under the server secret; the secret is never shown."""

    algorithm = KEY_DIGEST_ALGORITHM

    def __init__(self, secret: str) -> None:
        validate_idempotency_secret(secret)
        self._secret = secret.encode()

    def __call__(self, key: str) -> str:
        message = b"ragops-idempotency-key-v1\x00" + key.encode()
        return hmac.new(self._secret, message, hashlib.sha256).hexdigest()

    def __repr__(self) -> str:
        return "KeyDigest(HMAC-SHA256, secret=<redacted>)"


def principal_digest(context: AuthenticatedUserContext) -> str:
    return _digest("ragops-principal-v1", context.issuer or "", context.subject or context.user_id)


def request_fingerprint(question: str, top_k: int) -> str:
    """Digest of the request semantics only: no identity, token, key or trace values."""
    canonical = json.dumps(
        {
            "endpoint": ENDPOINT,
            "question": " ".join(unicodedata.normalize("NFC", question).split()),
            "top_k": top_k,
            "version": 1,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def failure_state(
    stage: ExecutionStage, category: ProviderErrorCategory | None
) -> IdempotencyState:
    """Before the provider nothing was billed; a possibly billed failure is never retried."""
    if stage in (ExecutionStage.PREPARE, ExecutionStage.RESERVE):
        return _S.RETRYABLE
    if stage is ExecutionStage.PROVIDER and category in NOT_BILLED:
        return _S.FAILED
    if stage is ExecutionStage.COMPLETE:
        return _S.FAILED
    return _S.AMBIGUOUS_HELD


@dataclass(frozen=True)
class IdempotencyScope:
    tenant_id: str
    principal: str
    endpoint: str
    key_hash: str

    @classmethod
    def for_query(
        cls, context: AuthenticatedUserContext, key: str, digest: KeyDigest
    ) -> IdempotencyScope:
        return cls(context.tenant_id, principal_digest(context), ENDPOINT, digest(key))


@dataclass(frozen=True)
class Claim:
    decision: Decision
    record_id: UUID
    correlation_id: UUID
    attempt: int
    status_code: int | None = None
    body: dict[str, Any] | None = None
    reason: str = ""


def _rowcount(result: Any) -> int:
    """Rows changed by an UPDATE or DELETE; reliable on PostgreSQL and SQLite."""
    return int(result.rowcount)


def _utc(value: datetime) -> datetime:
    # SQLite returns naive datetimes for timezone-aware columns; they are stored as UTC.
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


class QueryIdempotencyStore:
    def __init__(
        self,
        engine: Callable[[], Engine],
        *,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
        lease_seconds: int = DEFAULT_LEASE_SECONDS,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        if not MIN_TTL_SECONDS <= ttl_seconds <= MAX_TTL_SECONDS:
            raise ValueError("idempotency retention is out of bounds")
        if not 1 <= lease_seconds <= ttl_seconds:
            raise ValueError("idempotency lease is out of bounds")
        self.engine, self.clock = engine, clock
        self.ttl, self.lease = timedelta(seconds=ttl_seconds), timedelta(seconds=lease_seconds)

    # ------------------------------------------------------------------ claim

    def claim(self, scope: IdempotencyScope, fingerprint: str) -> Claim:
        now = self.clock()
        try:
            with transaction(self.engine()) as session:
                # Insert first: the unique constraint, not this code, decides the winner.
                inserted = self._insert(session, scope, fingerprint, now)
                row = session.scalar(
                    select(QueryIdempotencyRecord)
                    .where(*self._match(scope))
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
                if row is None:
                    raise IdempotencyUnavailable("idempotency record could not be read")
                if inserted:
                    claim = self._claim_of(row, Decision.EXECUTE, "new")
                else:
                    claim = self._decide(row, fingerprint, now)
        except (IdempotencyUnavailable, IdempotencyLost):
            raise
        except (SQLAlchemyError, OSError) as exc:
            raise IdempotencyUnavailable("idempotency store unavailable") from exc
        if inserted:
            self._purge_quietly(scope.tenant_id)
        return claim

    def _insert(
        self, session: Session, scope: IdempotencyScope, fingerprint: str, now: datetime
    ) -> bool:
        values = {
            "tenant_id": scope.tenant_id,
            "id": uuid4(),
            "created_at": now,
            "principal": scope.principal,
            "endpoint": scope.endpoint,
            "key_hash": scope.key_hash,
            "request_fingerprint": fingerprint,
            "correlation_id": uuid4(),
            "state": _S.CLAIMED.value,
            "attempt": 1,
            "updated_at": now,
            "lease_expires_at": now + self.lease,
            "expires_at": now + self.ttl,
        }
        scope_columns = ["tenant_id", "principal", "endpoint", "key_hash"]
        dialect = session.get_bind().dialect.name
        if dialect == "postgresql":
            statement: Any = (
                postgresql_insert(QueryIdempotencyRecord)
                .values(**values)
                .on_conflict_do_nothing(index_elements=scope_columns)
            )
        elif dialect == "sqlite":
            statement = (
                sqlite_insert(QueryIdempotencyRecord)
                .values(**values)
                .on_conflict_do_nothing(index_elements=scope_columns)
            )
        else:
            raise IdempotencyUnavailable("unsupported idempotency store")
        # RETURNING yields a row only for the insert that won; rowcount is driver-dependent.
        returning = statement.returning(QueryIdempotencyRecord.id)
        return session.execute(returning).first() is not None

    def _decide(self, row: QueryIdempotencyRecord, fingerprint: str, now: datetime) -> Claim:
        state = _S(row.state)
        if state in _FINISHED and _utc(row.expires_at) <= now:
            return self._restart(row, fingerprint, now, "expired", new_identity=True)
        if row.request_fingerprint != fingerprint:
            return self._claim_of(row, Decision.CONFLICT)
        if state in (_S.SUCCEEDED, _S.FAILED):
            return self._claim_of(row, Decision.REPLAY)
        if state is _S.RETRYABLE:
            return self._restart(row, fingerprint, now, "retryable", new_identity=False)
        lease_over = _utc(row.lease_expires_at) <= now
        if state is _S.CLAIMED:
            if lease_over:  # abandoned before the billing boundary: safe to take over
                return self._restart(row, fingerprint, now, "lease_expired", new_identity=False)
            return self._claim_of(row, Decision.IN_PROGRESS)
        if state is _S.EXECUTING and not lease_over:
            return self._claim_of(row, Decision.IN_PROGRESS)
        return self._claim_of(row, Decision.RECONCILIATION_REQUIRED)

    def _restart(
        self,
        row: QueryIdempotencyRecord,
        fingerprint: str,
        now: datetime,
        reason: str,
        *,
        new_identity: bool,
    ) -> Claim:
        """Take over a locked record; an expired one starts a new logical query."""
        if new_identity:
            row.correlation_id = uuid4()
        row.request_fingerprint = fingerprint
        row.state = _S.CLAIMED.value
        row.attempt += 1
        row.status_code, row.response = None, None
        row.updated_at, row.lease_expires_at = now, now + self.lease
        row.expires_at = now + self.ttl
        return self._claim_of(row, Decision.EXECUTE, reason)

    @staticmethod
    def _claim_of(row: QueryIdempotencyRecord, decision: Decision, reason: str = "") -> Claim:
        replay = decision is Decision.REPLAY
        return Claim(
            decision=decision,
            record_id=row.id,
            correlation_id=row.correlation_id,
            attempt=row.attempt,
            status_code=row.status_code if replay else None,
            body=dict(row.response) if replay and row.response is not None else None,
            reason=reason or row.state,
        )

    @staticmethod
    def _match(scope: IdempotencyScope) -> tuple[Any, ...]:
        record = QueryIdempotencyRecord
        return (
            record.tenant_id == scope.tenant_id,
            record.principal == scope.principal,
            record.endpoint == scope.endpoint,
            record.key_hash == scope.key_hash,
        )

    # ------------------------------------------------------------------ owner writes

    def enter_billing(self, scope: IdempotencyScope, claim: Claim) -> None:
        """Durably cross the billing boundary; afterwards the key never executes again."""
        self._transition(
            scope, claim, (_S.CLAIMED,), _S.EXECUTING, lease_expires_at=self.clock() + self.lease
        )

    def succeed(
        self, scope: IdempotencyScope, claim: Claim, status_code: int, body: dict[str, Any]
    ) -> None:
        self._finish(scope, claim, _S.SUCCEEDED, status_code, body)

    def fail(
        self, scope: IdempotencyScope, claim: Claim, status_code: int, body: dict[str, Any]
    ) -> None:
        self._finish(scope, claim, _S.FAILED, status_code, body)

    def retryable(self, scope: IdempotencyScope, claim: Claim) -> None:
        self._finish(scope, claim, _S.RETRYABLE, None, None)

    def hold(self, scope: IdempotencyScope, claim: Claim) -> None:
        self._transition(
            scope, claim, (_S.EXECUTING,), _S.AMBIGUOUS_HELD, status_code=None, response=None
        )

    def _finish(
        self,
        scope: IdempotencyScope,
        claim: Claim,
        state: IdempotencyState,
        status_code: int | None,
        body: dict[str, Any] | None,
    ) -> None:
        self._transition(
            scope,
            claim,
            (_S.CLAIMED, _S.EXECUTING),
            state,
            status_code=status_code,
            response=body,
            expires_at=self.clock() + self.ttl,
        )

    def _transition(
        self,
        scope: IdempotencyScope,
        claim: Claim,
        sources: tuple[IdempotencyState, ...],
        target: IdempotencyState,
        **values: Any,
    ) -> None:
        assert all(target in TRANSITIONS[source] for source in sources)
        record = QueryIdempotencyRecord
        try:
            with transaction(self.engine()) as session:
                changed = _rowcount(
                    session.execute(
                        update(record)
                        .where(
                            record.tenant_id == scope.tenant_id,
                            record.id == claim.record_id,
                            record.attempt == claim.attempt,
                            record.state.in_([source.value for source in sources]),
                        )
                        .values(state=target.value, updated_at=self.clock(), **values)
                        .execution_options(synchronize_session=False)
                    )
                )
                if changed != 1:
                    raise IdempotencyLost("idempotency record is no longer owned")
        except IdempotencyLost:
            raise
        except (SQLAlchemyError, OSError) as exc:
            raise IdempotencyUnavailable("idempotency store unavailable") from exc

    # ------------------------------------------------------------------ retention

    def purge_expired(self, tenant_id: str) -> int:
        """Delete expired finished records of one tenant; ambiguous records are kept."""
        record = QueryIdempotencyRecord
        try:
            with transaction(self.engine()) as session:
                return _rowcount(
                    session.execute(
                        delete(record)
                        .where(
                            record.tenant_id == tenant_id,
                            record.state.in_([state.value for state in _FINISHED]),
                            record.expires_at <= self.clock(),
                        )
                        .execution_options(synchronize_session=False)
                    )
                )
        except (SQLAlchemyError, OSError) as exc:
            raise IdempotencyUnavailable("idempotency store unavailable") from exc

    def _purge_quietly(self, tenant_id: str) -> None:
        try:
            self.purge_expired(tenant_id)
        except IdempotencyUnavailable:
            pass  # retention catches up on the next claim; the claim itself is durable
