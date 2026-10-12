"""Durable query idempotency: key contract, fingerprint, state machine and store semantics."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import inspect as sa_inspect
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session
from tests.security.finops_helpers import migrated_sqlite

from ragops.auth.identity import AuthenticatedUserContext
from ragops.modeling.router import ProviderErrorCategory
from ragops.persistence.database import transaction
from ragops.persistence.models import QueryIdempotencyRecord, Tenant
from ragops.workflows import query_idempotency as idem
from ragops.workflows.query_idempotency import (
    Decision,
    ExecutionStage,
    IdempotencyLost,
    IdempotencyScope,
    IdempotencyState,
    IdempotencyUnavailable,
    InvalidIdempotencyKey,
    QueryIdempotencyStore,
)

KEY = "retry-7f3a:order/42+x=="
HMAC_KEY = "unit-test-hmac-secret-" + "0123456789-abcdefghij"
DIGEST = idem.KeyDigest(HMAC_KEY)


def context(tenant: str = "tenant-a", subject: str = "user-a") -> AuthenticatedUserContext:
    return AuthenticatedUserContext(
        user_id=subject, tenant_id=tenant, roles=("viewer",), issuer="https://idp", subject=subject
    )


# --------------------------------------------------------------------------- key contract


def test_absent_key_is_backward_compatible() -> None:
    assert idem.parse_key([]) is None


@pytest.mark.parametrize("key", ["a", KEY, "A" * 128, "550e8400-e29b-41d4-a716-446655440000"])
def test_bounded_opaque_keys_are_accepted(key) -> None:
    assert idem.parse_key([key]) == key


@pytest.mark.parametrize(
    "values",
    [[""], ["A" * 129], ["has space"], ["tab\tkey"], ["umlaut-ä"], ["semi;colon"], ["a", "b"]],
    ids=["empty", "oversized", "space", "control", "non-ascii", "separator", "repeated"],
)
def test_malformed_oversized_or_repeated_keys_are_rejected(values) -> None:
    with pytest.raises(InvalidIdempotencyKey):
        idem.parse_key(values)


def test_the_key_digest_is_an_hmac_with_the_server_secret() -> None:
    import hashlib
    import hmac

    digest = DIGEST(KEY)
    expected = hmac.new(
        HMAC_KEY.encode(), b"ragops-idempotency-key-v1\x00" + KEY.encode(), hashlib.sha256
    ).hexdigest()
    assert digest == expected and len(digest) == 64 and KEY not in digest
    assert digest != hashlib.sha256(KEY.encode()).hexdigest()
    assert digest != hashlib.sha256(b"ragops-idempotency-key-v1\x00" + KEY.encode()).hexdigest()
    assert idem.KEY_DIGEST_ALGORITHM == "HMAC-SHA256"


def test_same_key_and_secret_give_the_same_digest_another_secret_does_not() -> None:
    assert DIGEST(KEY) == idem.KeyDigest(HMAC_KEY)(KEY) != DIGEST(KEY + "1")
    other = idem.KeyDigest("another-unit-test-secret-9876543210-zyxwvu")
    assert other(KEY) != DIGEST(KEY)


@pytest.mark.parametrize(
    "secret",
    ["", "short-secret", "a" * 64, "abababababababababababababababababab"],
    ids=["empty", "too-short", "one-character", "two-characters"],
)
def test_a_weak_server_secret_is_rejected(secret) -> None:
    with pytest.raises(ValueError, match="RAGOPS_IDEMPOTENCY_HMAC_KEY") as raised:
        idem.KeyDigest(secret)
    assert secret not in str(raised.value) or not secret


def test_the_server_secret_never_appears_in_a_representation() -> None:
    assert HMAC_KEY not in repr(DIGEST) and HMAC_KEY not in str(DIGEST)
    scope = IdempotencyScope.for_query(context(), KEY, DIGEST)
    assert HMAC_KEY not in repr(scope) and KEY not in repr(scope)


def test_scope_is_tenant_user_endpoint_and_key_hash() -> None:
    scope = IdempotencyScope.for_query(context(), KEY, DIGEST)
    assert scope.tenant_id == "tenant-a" and scope.endpoint == idem.ENDPOINT
    assert scope.key_hash == DIGEST(KEY)
    other_user = IdempotencyScope.for_query(context(subject="user-b"), KEY, DIGEST)
    assert scope.principal != other_user.principal
    assert "user-a" not in scope.principal and len(scope.principal) == 64
    other_tenant = IdempotencyScope.for_query(context("tenant-b"), KEY, DIGEST)
    assert other_tenant.tenant_id == "tenant-b" and other_tenant != scope


# --------------------------------------------------------------------------- fingerprint


def test_equivalent_requests_have_the_same_fingerprint() -> None:
    base = idem.request_fingerprint("Welche Regel gilt?", 3)
    assert base == idem.request_fingerprint("  Welche   Regel\ngilt?  ", 3)
    assert base == idem.request_fingerprint("Welche Regel gilt?", 3)
    # NFC and NFD spellings of the same text are one request.
    assert idem.request_fingerprint("Prüfung", 3) == idem.request_fingerprint("Prüfung", 3)
    assert len(base) == 64 and "Regel" not in base


@pytest.mark.parametrize(
    ("question", "top_k"),
    [("Welche Regel gilt nicht?", 3), ("Welche Regel gilt?", 4), ("welche regel gilt?", 3)],
)
def test_a_semantic_change_changes_the_fingerprint(question, top_k) -> None:
    assert idem.request_fingerprint(question, top_k) != idem.request_fingerprint(
        "Welche Regel gilt?", 3
    )


def test_transport_values_are_not_part_of_the_fingerprint() -> None:
    import inspect

    parameters = list(inspect.signature(idem.request_fingerprint).parameters)
    assert parameters == ["question", "top_k"]
    assert idem.FINGERPRINT_ALGORITHM.startswith("sha256")


# --------------------------------------------------------------------------- state machine


def test_states_and_transitions_are_explicit() -> None:
    assert {state.value for state in IdempotencyState} == {
        "claimed",
        "executing",
        "succeeded",
        "retryable",
        "failed",
        "ambiguous_held",
        "reconciled",
    }
    allowed = idem.TRANSITIONS
    # Once the billing boundary is crossed the record never returns to an executable state.
    assert IdempotencyState.CLAIMED not in allowed[IdempotencyState.EXECUTING]
    # An ambiguous record only leaves its hold when reconciliation booked the billed usage.
    assert allowed[IdempotencyState.AMBIGUOUS_HELD] == frozenset({IdempotencyState.RECONCILED})
    assert IdempotencyState.AMBIGUOUS_HELD in allowed[IdempotencyState.EXECUTING]
    assert IdempotencyState.AMBIGUOUS_HELD not in allowed[IdempotencyState.CLAIMED]


@pytest.mark.parametrize(
    ("stage", "category", "expected"),
    [
        (ExecutionStage.PREPARE, None, IdempotencyState.RETRYABLE),
        (ExecutionStage.RESERVE, None, IdempotencyState.RETRYABLE),
        (ExecutionStage.PROVIDER, ProviderErrorCategory.RATE_LIMIT, IdempotencyState.FAILED),
        (ExecutionStage.PROVIDER, ProviderErrorCategory.AUTHENTICATION, IdempotencyState.FAILED),
        (ExecutionStage.PROVIDER, ProviderErrorCategory.TIMEOUT, IdempotencyState.AMBIGUOUS_HELD),
        (
            ExecutionStage.PROVIDER,
            ProviderErrorCategory.PROVIDER_ERROR,
            IdempotencyState.AMBIGUOUS_HELD,
        ),
        (ExecutionStage.PROVIDER, None, IdempotencyState.AMBIGUOUS_HELD),
        (ExecutionStage.ACCOUNTING, None, IdempotencyState.AMBIGUOUS_HELD),
        (ExecutionStage.COMPLETE, None, IdempotencyState.FAILED),
    ],
)
def test_failure_disposition_follows_the_billing_boundary(stage, category, expected) -> None:
    assert idem.failure_state(stage, category) is expected


# --------------------------------------------------------------------------- store


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: int) -> None:
        self.now += timedelta(seconds=seconds)


@pytest.fixture
def store(tmp_path):
    engine, _ = migrated_sqlite(tmp_path / "idempotency.db")
    with transaction(engine) as session:
        session.add_all([Tenant(id=t, name=t) for t in ("tenant-a", "tenant-b")])
    clock = Clock()
    return QueryIdempotencyStore(lambda: engine, ttl_seconds=3600, lease_seconds=60, clock=clock)


def rows(store) -> list[QueryIdempotencyRecord]:
    with Session(store.engine()) as session:
        return list(session.scalars(select(QueryIdempotencyRecord)))


SCOPE = IdempotencyScope.for_query(context(), KEY, DIGEST)
FP = idem.request_fingerprint("Welche Regel gilt?", 3)


def test_the_first_claim_executes_and_stores_only_digests(store) -> None:
    claim = store.claim(SCOPE, FP)
    assert claim.decision is Decision.EXECUTE and claim.attempt == 1
    (row,) = rows(store)
    assert row.state == "claimed" and row.request_fingerprint == FP
    assert row.key_hash == SCOPE.key_hash == DIGEST(KEY)
    assert row.key_hash != hashlib.sha256(KEY.encode()).hexdigest()
    assert row.correlation_id == claim.correlation_id
    stored = " ".join(str(getattr(row, c.key)) for c in sa_inspect(row).mapper.column_attrs)
    assert KEY not in stored and "user-a" not in stored


def test_the_schema_has_no_raw_key_column_and_a_scope_unique_constraint(store) -> None:
    columns = {column.name for column in QueryIdempotencyRecord.__table__.columns}
    assert "idempotency_key" not in columns and "key" not in columns
    uniques = [
        tuple(column.name for column in constraint.columns)
        for constraint in QueryIdempotencyRecord.__table__.constraints
        if constraint.__class__.__name__ == "UniqueConstraint"
    ]
    assert ("tenant_id", "principal", "endpoint", "key_hash") in uniques


def test_the_database_rejects_a_duplicate_scope(store) -> None:
    store.claim(SCOPE, FP)
    (row,) = rows(store)
    with pytest.raises(IntegrityError), transaction(store.engine()) as session:
        session.add(
            QueryIdempotencyRecord(
                tenant_id=row.tenant_id,
                principal=row.principal,
                endpoint=row.endpoint,
                key_hash=row.key_hash,
                request_fingerprint=FP,
                correlation_id=uuid4(),
                state="claimed",
                attempt=1,
                updated_at=row.updated_at,
                lease_expires_at=row.lease_expires_at,
                expires_at=row.expires_at,
            )
        )


def test_a_concurrent_claim_sees_in_progress(store) -> None:
    first = store.claim(SCOPE, FP)
    second = store.claim(SCOPE, FP)
    assert second.decision is Decision.IN_PROGRESS and second.record_id == first.record_id
    store.enter_billing(SCOPE, first)
    assert store.claim(SCOPE, FP).decision is Decision.IN_PROGRESS
    assert len(rows(store)) == 1


def test_success_is_replayed_with_the_original_correlation(store) -> None:
    first = store.claim(SCOPE, FP)
    store.enter_billing(SCOPE, first)
    store.succeed(SCOPE, first, 200, {"answer": "x", "correlation_id": str(first.correlation_id)})
    replay = store.claim(SCOPE, FP)
    assert replay.decision is Decision.REPLAY and replay.status_code == 200
    assert replay.correlation_id == first.correlation_id
    assert replay.body == {"answer": "x", "correlation_id": str(first.correlation_id)}


def test_same_key_with_a_different_request_conflicts_in_every_state(store) -> None:
    other = idem.request_fingerprint("Andere Frage?", 3)
    first = store.claim(SCOPE, FP)
    assert store.claim(SCOPE, other).decision is Decision.CONFLICT
    store.succeed(SCOPE, first, 200, {})
    assert store.claim(SCOPE, other).decision is Decision.CONFLICT
    (row,) = rows(store)
    assert row.request_fingerprint == FP and row.state == "succeeded"


def test_tenants_and_users_never_share_a_record(store) -> None:
    first = store.claim(SCOPE, FP)
    store.succeed(SCOPE, first, 200, {"answer": "tenant-a"})
    tenant_b = IdempotencyScope.for_query(context("tenant-b"), KEY, DIGEST)
    user_b = IdempotencyScope.for_query(context(subject="user-b"), KEY, DIGEST)
    other = idem.request_fingerprint("Ganz andere Frage?", 3)
    for scope in (tenant_b, user_b):
        claim = store.claim(scope, other)  # a different payload is no conflict elsewhere
        assert claim.decision is Decision.EXECUTE and claim.body is None
        assert claim.correlation_id != first.correlation_id
    assert len(rows(store)) == 3


def test_retryable_reexecutes_with_the_same_logical_correlation(store) -> None:
    first = store.claim(SCOPE, FP)
    store.enter_billing(SCOPE, first)
    store.retryable(SCOPE, first)
    again = store.claim(SCOPE, FP)
    assert again.decision is Decision.EXECUTE and again.reason == "retryable"
    assert again.correlation_id == first.correlation_id and again.attempt == 2
    assert store.claim(SCOPE, FP).decision is Decision.IN_PROGRESS


def test_ambiguous_and_expired_executing_records_are_never_reexecuted(store) -> None:
    first = store.claim(SCOPE, FP)
    store.enter_billing(SCOPE, first)
    store.clock.advance(61)  # the lease of a record past the billing boundary ran out
    assert store.claim(SCOPE, FP).decision is Decision.RECONCILIATION_REQUIRED
    store.hold(SCOPE, first)  # the owner may still record the ambiguity
    store.clock.advance(10 * 3600)  # far beyond the retention window
    assert store.claim(SCOPE, FP).decision is Decision.RECONCILIATION_REQUIRED
    assert store.purge_expired("tenant-a") == 0
    (row,) = rows(store)
    assert row.state == "ambiguous_held"


def test_failed_is_replayed_as_a_stable_failure(store) -> None:
    first = store.claim(SCOPE, FP)
    store.enter_billing(SCOPE, first)
    store.fail(SCOPE, first, 503, {"detail": "generation unavailable"})
    replay = store.claim(SCOPE, FP)
    assert (replay.decision, replay.status_code, replay.body) == (
        Decision.REPLAY,
        503,
        {"detail": "generation unavailable"},
    )


def test_an_abandoned_claim_before_the_boundary_is_fenced_and_reclaimed(store) -> None:
    stale = store.claim(SCOPE, FP)
    store.clock.advance(61)
    fresh = store.claim(SCOPE, FP)
    assert fresh.decision is Decision.EXECUTE and fresh.reason == "lease_expired"
    assert fresh.correlation_id == stale.correlation_id and fresh.attempt == 2
    with pytest.raises(IdempotencyLost):
        store.enter_billing(SCOPE, stale)  # the old owner can no longer reach the provider
    with pytest.raises(IdempotencyLost):
        store.succeed(SCOPE, stale, 200, {})
    store.enter_billing(SCOPE, fresh)


def test_illegal_transitions_are_rejected(store) -> None:
    first = store.claim(SCOPE, FP)
    with pytest.raises(IdempotencyLost):
        store.hold(SCOPE, first)  # ambiguity requires a crossed billing boundary
    store.enter_billing(SCOPE, first)
    store.hold(SCOPE, first)
    for write in (store.retryable, store.enter_billing):
        with pytest.raises(IdempotencyLost):
            write(SCOPE, first)
    with pytest.raises(IdempotencyLost):
        store.succeed(SCOPE, first, 200, {})


def test_expired_safe_records_start_a_new_logical_execution(store) -> None:
    first = store.claim(SCOPE, FP)
    store.succeed(SCOPE, first, 200, {"answer": "old"})
    store.clock.advance(3601)
    other = idem.request_fingerprint("Neue Frage?", 3)
    renewed = store.claim(SCOPE, other)
    assert renewed.decision is Decision.EXECUTE and renewed.reason == "expired"
    assert renewed.correlation_id != first.correlation_id and renewed.body is None
    (row,) = rows(store)
    assert row.request_fingerprint == other and row.response is None and row.status_code is None


def test_purge_removes_only_expired_safe_records_of_the_tenant(store) -> None:
    done = store.claim(SCOPE, FP)
    store.succeed(SCOPE, done, 200, {"answer": "x"})
    held_scope = IdempotencyScope.for_query(context(), "held-key", DIGEST)
    held = store.claim(held_scope, FP)
    store.enter_billing(held_scope, held)
    store.hold(held_scope, held)
    foreign_scope = IdempotencyScope.for_query(context("tenant-b"), KEY, DIGEST)
    store.succeed(foreign_scope, store.claim(foreign_scope, FP), 200, {})
    store.clock.advance(3601)
    assert store.purge_expired("tenant-a") == 1
    assert sorted((row.tenant_id, row.state) for row in rows(store)) == [
        ("tenant-a", "ambiguous_held"),
        ("tenant-b", "succeeded"),
    ]


def test_a_database_failure_is_reported_never_ignored(store) -> None:
    with store.engine().begin() as connection:
        connection.execute(text("DROP TABLE query_idempotency_records"))
    with pytest.raises(IdempotencyUnavailable):
        store.claim(SCOPE, FP)


def test_an_unreachable_database_is_reported(tmp_path) -> None:
    def broken():
        raise OperationalError("connect", {}, Exception("down"))

    with pytest.raises(IdempotencyUnavailable):
        QueryIdempotencyStore(broken).claim(SCOPE, FP)


@pytest.mark.parametrize("ttl", [0, 299, 604_801])
def test_retention_is_bounded(ttl) -> None:
    with pytest.raises(ValueError):
        QueryIdempotencyStore(lambda: None, ttl_seconds=ttl)  # type: ignore[arg-type,return-value]


def test_default_retention_is_one_day() -> None:
    assert idem.DEFAULT_TTL_SECONDS == 86_400


def test_retention_is_configurable_and_validated(monkeypatch) -> None:
    from ragops.config.settings import Settings

    assert Settings().query_idempotency_ttl_seconds == idem.DEFAULT_TTL_SECONDS
    monkeypatch.setenv("RAGOPS_QUERY_IDEMPOTENCY_TTL_SECONDS", "3600")
    assert Settings.from_env().query_idempotency_ttl_seconds == 3600
    for value in (299, 604_801):
        with pytest.raises(ValueError, match="RAGOPS_QUERY_IDEMPOTENCY_TTL_SECONDS"):
            Settings(query_idempotency_ttl_seconds=value)


def test_the_hmac_secret_is_configured_from_the_environment_or_a_file(
    monkeypatch, tmp_path
) -> None:
    from ragops.config.settings import Settings

    assert Settings().query_idempotency_hmac_key is None
    monkeypatch.setenv("RAGOPS_IDEMPOTENCY_HMAC_KEY", HMAC_KEY)
    settings = Settings.from_env()
    assert settings.query_idempotency_hmac_key == HMAC_KEY
    assert HMAC_KEY not in repr(settings)
    monkeypatch.delenv("RAGOPS_IDEMPOTENCY_HMAC_KEY")
    secret_file = tmp_path / "hmac"
    secret_file.write_text(HMAC_KEY + "\n", encoding="utf-8")
    monkeypatch.setenv("RAGOPS_IDEMPOTENCY_HMAC_KEY_FILE", str(secret_file))
    assert Settings.from_env().query_idempotency_hmac_key == HMAC_KEY
    monkeypatch.setenv("RAGOPS_IDEMPOTENCY_HMAC_KEY", HMAC_KEY)
    with pytest.raises(ValueError, match="exactly one"):
        Settings.from_env()


def test_a_weak_configured_hmac_secret_fails_at_startup() -> None:
    from ragops.config.settings import Settings

    with pytest.raises(ValueError, match="RAGOPS_IDEMPOTENCY_HMAC_KEY"):
        Settings(query_idempotency_hmac_key="too-short")
