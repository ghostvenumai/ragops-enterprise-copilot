"""Rate-limiting RC gate: the production limiter and FastAPI wiring against real Redis.

Redis is resolved like the redis_worker gate (configured URL or the published rc-redis
port). All limiter keys live under a unique ragops:rc:rate-limit:<run-id> prefix; the
gate only reads Redis directly for verification, deletes nothing outside its prefix
and leaves an unrelated sentinel key untouched. The HTTP scenarios drive the real
app with ephemeral OIDC tokens and a counted deterministic provider; outbound HTTP
is blocked, so paid_provider_calls must stay 0.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ragops.ops.rate_limit import (  # noqa: E402
    RateLimiterUnavailable,
    RedisRateLimiter,
    rate_limit_key,
)

EVIDENCE = ROOT / "evidence" / "product-v1" / "rc-live" / "rate-limiting.json"
DEFAULT_REDIS_URL = "redis://redis:6379/0"  # compose service; mapped to the rc-redis port
OIDC_ISSUER = "https://rc-rate-limit.invalid/"
OIDC_AUDIENCE = "ragops-rc-rate-limit"
LIMIT, WINDOW = 5, 60
CONCURRENCY_LIMIT, CONCURRENCY_ATTEMPTS = 10, 25
API_LIMIT = 3
SENSITIVE_IDENTIFIERS = ("tenant-alpha", "tenant-beta", "rc-rl-a", "rc-rl-b", "rc-user")


@dataclass
class RateLimitResult:
    checks: dict[str, bool] = field(default_factory=dict)
    metrics: dict[str, Any] = field(default_factory=dict)
    rate_limit_tenant_leakage: int = 0
    provider_invocations: int = 0
    errors: list[str] = field(default_factory=list)

    def check(self, name: str, ok: bool) -> None:
        self.checks[name] = self.checks.get(name, True) and ok


def _limiter(
    url: str, namespace: str, limit: int = LIMIT, window: int = WINDOW
) -> RedisRateLimiter:
    return RedisRateLimiter(url, limit=limit, window_seconds=window, namespace=namespace)


def check_boundaries(url: str, namespace: str, redis: Any, result: RateLimitResult) -> None:
    limiter = _limiter(url, f"{namespace}:boundary")
    results = [limiter.check("rc-rl-a", "rc-user", "rag") for _ in range(LIMIT + 1)]
    key = rate_limit_key(f"{namespace}:boundary", "rc-rl-a", "rc-user", "rag")
    result.check("below_limit_verified", all(r.allowed for r in results[: LIMIT - 1]))
    result.check(
        "exact_capacity_verified", results[LIMIT - 1].allowed and results[LIMIT - 1].remaining == 0
    )
    denied = results[LIMIT]
    result.check("over_limit_verified", not denied.allowed and denied.remaining == 0)
    result.check("retry_after_verified", 1 <= denied.retry_after <= WINDOW)
    ttl_before = int(redis.pttl(key))
    storm = [limiter.check("rc-rl-a", "rc-user", "rag") for _ in range(20)]
    ttl_after = int(redis.pttl(key))
    # Retry storms after 429 neither inflate the counter nor extend the window.
    result.check(
        "retry_storm_safe",
        not any(r.allowed for r in storm)
        and all(r.remaining == 0 for r in storm)
        and int(redis.get(key)) == LIMIT
        and 0 < ttl_after <= ttl_before,
    )
    result.check("ttl_verified", 0 < ttl_before <= WINDOW * 1000)
    result.metrics.update(
        {
            "requests_total": len(results) + len(storm),
            "allowed_count": sum(r.allowed for r in results),
        }
    )

    short = _limiter(url, f"{namespace}:reset", limit=2, window=1)
    short_key = rate_limit_key(f"{namespace}:reset", "rc-rl-a", "rc-user", "rag")
    exhausted = [short.check("rc-rl-a", "rc-user", "rag").allowed for _ in range(3)]
    deadline = time.monotonic() + 3
    while redis.exists(short_key) and time.monotonic() < deadline:
        time.sleep(
            max(int(redis.pttl(short_key)), 10) / 1000 + 0.02
        )  # authoritative PTTL, no fixed sleep
    after = short.check("rc-rl-a", "rc-user", "rag")
    result.check(
        "reset_or_refill_verified",
        exhausted == [True, True, False] and after.allowed and after.remaining == 1,
    )


def check_concurrency(url: str, namespace: str, redis: Any, result: RateLimitResult) -> None:
    concurrency_namespace = f"{namespace}:concurrency"
    instances = [_limiter(url, concurrency_namespace, CONCURRENCY_LIMIT) for _ in range(2)]
    barrier = threading.Barrier(CONCURRENCY_ATTEMPTS)
    outcomes: list[str] = []
    lock = threading.Lock()

    def attempt(index: int) -> None:
        barrier.wait()
        try:
            allowed = instances[index % 2].check("rc-rl-a", "rc-user", "rag").allowed
            outcome = "allowed" if allowed else "denied"
        except RateLimiterUnavailable:
            outcome = "error"
        with lock:
            outcomes.append(outcome)

    threads = [threading.Thread(target=attempt, args=(i,)) for i in range(CONCURRENCY_ATTEMPTS)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    key = rate_limit_key(concurrency_namespace, "rc-rl-a", "rc-user", "rag")
    allowed, denied, errors = (outcomes.count(name) for name in ("allowed", "denied", "error"))
    final = int(redis.get(key) or 0)
    result.metrics.update(
        {
            "concurrency_attempts": CONCURRENCY_ATTEMPTS,
            "concurrency_allowed": allowed,
            "concurrency_denied": denied,
            "error_count": errors,
            "final_counter": final,
            "max_observed_allowed": allowed,
            "limiter_instances": len(instances),
        }
    )
    result.check(
        "atomicity_verified",
        allowed == CONCURRENCY_LIMIT
        and denied == CONCURRENCY_ATTEMPTS - CONCURRENCY_LIMIT
        and errors == 0,
    )
    result.check(
        "distributed_instances_verified",
        final == CONCURRENCY_LIMIT and allowed <= CONCURRENCY_LIMIT,
    )


def check_isolation(url: str, namespace: str, result: RateLimitResult) -> None:
    limiter = _limiter(url, f"{namespace}:isolation", limit=3)
    for _ in range(4):
        limiter.check("rc-rl-a", "rc-user", "rag")
    others = {
        "tenant": ("rc-rl-b", "rc-user", "rag"),
        "route": ("rc-rl-a", "rc-user", "upload"),
        "user": ("rc-rl-a", "rc-user-2", "rag"),
    }
    for name, identity in others.items():
        full = [limiter.check(*identity).allowed for _ in range(3)] == [True, True, True]
        if not full:
            result.rate_limit_tenant_leakage += 1
        result.check(
            "route_isolation_verified" if name == "route" else "tenant_isolation_verified", full
        )
    collide_a = limiter.check("rc-rl-a:b", "c", "rag").allowed
    collide_b = limiter.check("rc-rl-a", "b:c", "rag").allowed
    result.check("delimiter_collision_prevented", collide_a and collide_b)


def check_sensitive_keys(redis: Any, namespace: str, result: RateLimitResult) -> list[str]:
    keys = [
        key.decode() if isinstance(key, bytes) else key
        for key in redis.scan_iter(match=f"{namespace}*", count=500)
    ]
    exposed = [key for key in keys if any(item in key for item in SENSITIVE_IDENTIFIERS)]
    immortal = [key for key in keys if int(redis.pttl(key)) == -1]
    result.metrics.update(
        {"namespace_key_count": len(keys), "immortal_keys_detected": bool(immortal)}
    )
    result.check("sensitive_key_material_absent", not exposed)
    result.check("no_immortal_keys", not immortal)
    return keys


@contextmanager
def _counted_provider(result: RateLimitResult) -> Iterator[None]:
    from ragops.llm.providers import DeterministicTestProvider

    original = DeterministicTestProvider.generate

    def counted(
        self: DeterministicTestProvider, question: str, citations: Any, context: str
    ) -> Any:
        result.provider_invocations += 1
        return original(self, question, citations, context)

    DeterministicTestProvider.generate = counted  # type: ignore[method-assign]
    try:
        yield
    finally:
        DeterministicTestProvider.generate = original  # type: ignore[method-assign]


def _closed_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def check_api(
    url: str, namespace: str, workdir: Path, result: RateLimitResult
) -> dict[str, list[str]]:
    import jwt
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from fastapi.testclient import TestClient

    from ragops.api.app import create_app
    from ragops.config.settings import Settings
    from scripts.model_router_gate import deterministic_app_environment

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_key = (
        key.public_key()
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        .decode()
    )

    def headers(tenant: str, roles: list[str]) -> dict[str, str]:
        claims = {
            "iss": OIDC_ISSUER,
            "aud": OIDC_AUDIENCE,
            "sub": f"rc-user-{tenant}",
            "tenant_id": tenant,
            "roles": roles,
            "exp": datetime.now(UTC) + timedelta(minutes=5),
        }
        return {"Authorization": f"Bearer {jwt.encode(claims, key, algorithm='RS256')}"}

    def app(redis_url: str, suffix: str) -> TestClient:
        settings = Settings(
            environment="test",
            identity_provider="oidc",
            oidc_issuer=OIDC_ISSUER,
            oidc_audience=OIDC_AUDIENCE,
            oidc_public_key=public_key,
            evidence_dir=workdir / suffix,
            rate_limit_backend="redis",
            rate_limit_redis_url=redis_url,
            rate_limit_namespace=f"{namespace}:{suffix}",
            rate_limit_requests=API_LIMIT,
            rate_limit_window_seconds=WINDOW,
            rate_limit_timeout_seconds=0.2,
        )
        with deterministic_app_environment():
            return TestClient(create_app(settings), raise_server_exceptions=False)

    body = {"question": "Welche Verfügbarkeit gilt für Atlas?"}
    alpha, beta = headers("tenant-alpha", ["sales"]), headers("tenant-beta", ["sales"])
    client = app(url, "api")
    with _counted_provider(result):
        allowed = [client.post("/v1/query", json=body, headers=alpha) for _ in range(API_LIMIT)]
        invocations_after_allowed = result.provider_invocations
        denied = client.post("/v1/query", json=body, headers=alpha)
        spoof_body = client.post(
            "/v1/query", json={**body, "tenant_id": "tenant-beta", "user_id": "x"}, headers=alpha
        )
        spoof_header = client.post(
            "/v1/query", json=body, headers={**alpha, "X-Tenant-ID": "tenant-beta"}
        )
        invocations_after_rejections = result.provider_invocations
        other = client.post("/v1/query", json=body, headers=beta)
        anonymous = client.post("/v1/query", json=body)
        health = client.get("/health")
        simulation = client.post(
            "/v1/admin/model-router/simulate", json={}, headers=headers("tenant-alpha", ["admin"])
        )
        invocations_before_outage = result.provider_invocations
        outage = app(f"redis://127.0.0.1:{_closed_port()}/0", "outage")
        started = time.monotonic()
        unavailable = outage.post("/v1/query", json=body, headers=alpha)
        outage_seconds = time.monotonic() - started
        outage_invocations = result.provider_invocations - invocations_before_outage
        recovered = app(url, "recovery").post("/v1/query", json=body, headers=alpha)

    result.check(
        "query_path_enforced",
        [r.status_code for r in allowed] == [200] * API_LIMIT and denied.status_code == 429,
    )
    result.check(
        "rate_limit_headers_verified",
        [r.headers.get("RateLimit-Remaining") for r in allowed]
        == [str(API_LIMIT - i - 1) for i in range(API_LIMIT)]
        and all(r.headers.get("RateLimit-Limit") == str(API_LIMIT) for r in allowed)
        and denied.headers.get("RateLimit-Remaining") == "0",
    )
    result.check("http_429_verified", denied.status_code == 429)
    result.check("retry_after_verified", 1 <= int(denied.headers.get("Retry-After", "0")) <= WINDOW)
    # The 429 and both spoofed requests must stop before the provider is invoked.
    result.check(
        "denied_before_provider",
        invocations_after_allowed == API_LIMIT
        and invocations_after_rejections == invocations_after_allowed,
    )
    result.check(
        "spoofed_identity_rejected",
        spoof_body.status_code == 429 and spoof_header.status_code == 429,
    )
    if other.status_code != 200:
        result.rate_limit_tenant_leakage += 1
    result.check("tenant_isolation_verified", other.status_code == 200)
    result.check("auth_precedes_rate_limit", anonymous.status_code == 401)
    result.check("exempt_routes_verified", health.status_code == 200)
    result.check("admin_simulation_limited", "RateLimit-Limit" in simulation.headers)
    result.check(
        "redis_outage_verified",
        unavailable.status_code == 503
        and unavailable.headers.get("Retry-After") == "1"
        and outage_invocations == 0,
    )
    result.check("bounded_timeout_verified", outage_seconds < 2.0)
    result.check("recovery_verified", recovered.status_code == 200)

    routes: dict[str, list[str]] = {"enforced": [], "exempt": []}
    for route in client.app.routes:  # type: ignore[attr-defined]
        dependant = getattr(route, "dependant", None)
        if dependant is None or route.path.startswith(("/docs", "/redoc", "/openapi")):
            continue
        names = {dependency.call.__qualname__ for dependency in dependant.dependencies}
        routes["enforced" if any("limited" in name for name in names) else "exempt"].append(
            route.path
        )
    result.check("exemptions_narrow", sorted(routes["exempt"]) == ["/health", "/metrics", "/ready"])
    return {name: sorted(set(paths)) for name, paths in routes.items()}


REQUIRED = (
    "below_limit_verified",
    "exact_capacity_verified",
    "over_limit_verified",
    "retry_after_verified",
    "retry_storm_safe",
    "ttl_verified",
    "reset_or_refill_verified",
    "atomicity_verified",
    "distributed_instances_verified",
    "tenant_isolation_verified",
    "route_isolation_verified",
    "delimiter_collision_prevented",
    "sensitive_key_material_absent",
    "no_immortal_keys",
    "query_path_enforced",
    "rate_limit_headers_verified",
    "http_429_verified",
    "denied_before_provider",
    "spoofed_identity_rejected",
    "auth_precedes_rate_limit",
    "exempt_routes_verified",
    "admin_simulation_limited",
    "redis_outage_verified",
    "bounded_timeout_verified",
    "recovery_verified",
    "exemptions_narrow",
    "cleanup_verified",
    "unrelated_redis_data_unchanged",
)


def classify(result: RateLimitResult, paid_provider_calls: int) -> tuple[str, str]:
    missing = [name for name in REQUIRED if name not in result.checks]
    failed = sorted(name for name, ok in result.checks.items() if not ok)
    if paid_provider_calls:
        return "FAIL", "outbound provider traffic was attempted"
    if result.rate_limit_tenant_leakage:
        return "FAIL", "one tenant, user or route consumed another bucket"
    if int(result.metrics.get("max_observed_allowed", 0)) > CONCURRENCY_LIMIT:
        return "FAIL", "concurrent requests exceeded the configured capacity"
    if failed or missing:
        return "FAIL", f"rate-limit invariants failed: {', '.join(failed + missing)}"
    return "PASS", "production rate limiting enforced through real Redis on the request path"


def run(
    redis: Any, url: str, namespace: str, sentinel: str
) -> tuple[RateLimitResult, int, dict[str, list[str]]]:
    from scripts.model_router_gate import outbound_guard

    result = RateLimitResult()
    routes: dict[str, list[str]] = {"enforced": [], "exempt": []}
    sentinel_value = uuid4().hex
    redis.set(sentinel, sentinel_value, ex=600)
    steps: list[tuple[str, Callable[[], object]]] = [
        ("boundaries", lambda: check_boundaries(url, namespace, redis, result)),
        ("concurrency", lambda: check_concurrency(url, namespace, redis, result)),
        ("isolation", lambda: check_isolation(url, namespace, result)),
    ]
    with outbound_guard() as attempts, tempfile.TemporaryDirectory() as workdir:
        for name, step in steps:
            try:
                step()
            except Exception as exc:  # noqa: BLE001 - an aborted block must fail the gate
                result.errors.append(f"{name}: {type(exc).__name__}")
                result.check(f"{name}_completed", False)
        try:
            routes = check_api(url, namespace, Path(workdir), result)
        except Exception as exc:  # noqa: BLE001
            result.errors.append(f"api: {type(exc).__name__}")
            result.check("api_completed", False)
        check_sensitive_keys(redis, namespace, result)
    for key in list(redis.scan_iter(match=f"{namespace}*", count=500)):
        redis.delete(key)
    result.check("cleanup_verified", not list(redis.scan_iter(match=f"{namespace}*", count=500)))
    stored = redis.get(sentinel)
    stored = stored.decode() if isinstance(stored, bytes) else stored
    result.check("unrelated_redis_data_unchanged", stored == sentinel_value)
    return result, len(attempts), routes


def build_evidence(
    result: RateLimitResult, paid_calls: int, routes: dict[str, list[str]]
) -> dict[str, Any]:
    status, reason = classify(result, paid_calls)
    ok = result.checks.get
    return {
        "status": status,
        "reason": reason,
        "algorithm": "fixed window (Redis Lua: GET, INCR, PEXPIRE, PTTL in one script)",
        "window_edge_behavior": "not sliding: up to 2x limit across two adjacent windows",
        "policy_source": "Settings: RAGOPS_RATE_LIMIT_REQUESTS / RAGOPS_RATE_LIMIT_WINDOW_SECONDS",
        "policy_precedence": "one global policy applied per (tenant, user, endpoint class) bucket; "
        "no per-tenant or per-route overrides are implemented",
        "configured_limit": LIMIT,
        "configured_window_seconds": WINDOW,
        "configured_burst": None,
        "identity_scope": "verified tenant_id and user_id (OIDC sub) plus endpoint class; "
        "SHA-256 pseudonymized in Redis keys",
        "enforced_routes": routes["enforced"],
        "exempt_routes": routes["exempt"],
        "query_path_enforced": bool(ok("query_path_enforced")),
        "requests_total": result.metrics.get("requests_total"),
        "allowed_count": result.metrics.get("allowed_count"),
        "throttled_count": (result.metrics.get("requests_total") or 0)
        - (result.metrics.get("allowed_count") or 0),
        "error_count": result.metrics.get("error_count"),
        "below_limit_verified": ok("below_limit_verified"),
        "exact_capacity_verified": ok("exact_capacity_verified"),
        "over_limit_verified": ok("over_limit_verified"),
        "http_429_verified": ok("http_429_verified"),
        "retry_after_verified": ok("retry_after_verified"),
        "rate_limit_headers_verified": ok("rate_limit_headers_verified"),
        "ttl_verified": ok("ttl_verified"),
        "reset_or_refill_verified": ok("reset_or_refill_verified"),
        "immortal_keys_detected": result.metrics.get("immortal_keys_detected"),
        "atomicity_verified": ok("atomicity_verified"),
        "distributed_instances_verified": ok("distributed_instances_verified"),
        "max_observed_allowed": result.metrics.get("max_observed_allowed"),
        "concurrency": {
            key: result.metrics.get(key)
            for key in (
                "concurrency_attempts",
                "concurrency_allowed",
                "concurrency_denied",
                "final_counter",
                "limiter_instances",
            )
        },
        "rate_limit_tenant_leakage": result.rate_limit_tenant_leakage,
        "route_isolation_verified": ok("route_isolation_verified"),
        "spoofed_identity_rejected": ok("spoofed_identity_rejected"),
        "redis_failure_mode": "fail closed: 503 + Retry-After 1, stops before the provider",
        "redis_outage_verified": ok("redis_outage_verified"),
        "bounded_timeout_verified": ok("bounded_timeout_verified"),
        "recovery_verified": ok("recovery_verified"),
        "provider_invocations": result.provider_invocations,
        "paid_provider_calls": paid_calls,
        "sensitive_key_material_detected": not ok("sensitive_key_material_absent"),
        "unrelated_redis_data_unchanged": ok("unrelated_redis_data_unchanged"),
        "cleanup_status": "PASS" if ok("cleanup_verified") else "FAIL",
        "checks": dict(sorted(result.checks.items())),
        "errors": result.errors,
    }


def _commit() -> str:
    try:
        completed = subprocess.run(  # noqa: S603
            ["git", "rev-parse", "HEAD"],  # noqa: S607
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return "unknown"
    return completed.stdout.strip() or "unknown"


def _finish(evidence: dict[str, Any], status: str, reason: str, exit_code: int) -> int:
    evidence.update(
        {
            "status": status,
            "reason": reason,
            "exit_code": exit_code,
            "timestamp": datetime.now(UTC).isoformat(),
        }
    )
    EVIDENCE.parent.mkdir(parents=True, exist_ok=True)
    EVIDENCE.write_text(json.dumps(evidence, indent=2) + "\n", encoding="utf-8")
    return exit_code


def main() -> int:
    run_id = uuid4().hex[:10]
    namespace = f"ragops:rc:rate-limit:{run_id}"
    evidence: dict[str, Any] = {
        "gate": "rate_limiting",
        "tested_commit": _commit(),
        "redis_backend": "redis",
        "redis_reachable": False,
        "redis_version": None,
        "namespace_prefix": "ragops:rc:rate-limit:<run-id>",
        "paid_provider_calls": 0,
        "cleanup_status": "NOT_RUN",
    }
    try:
        import redis as redis_module

        from scripts.tenant_isolation_gate import _redis_endpoint
    except ImportError as exc:
        return _finish(evidence, "BLOCKED", f"missing dependency: {exc.name}", 2)
    url = _redis_endpoint(os.getenv("RAGOPS_REDIS_URL") or DEFAULT_REDIS_URL, evidence)
    if url is None:
        return _finish(evidence, "BLOCKED", "RC Redis is unavailable", 2)
    client = redis_module.Redis.from_url(url, socket_timeout=2, socket_connect_timeout=2)
    try:
        server: dict[str, Any] = client.info("server")  # type: ignore[assignment]
        evidence["redis_version"] = str(server.get("redis_version"))
        evidence["redis_reachable"] = True
    except Exception:  # noqa: BLE001
        return _finish(evidence, "BLOCKED", "RC Redis did not answer", 2)
    sentinel = f"ragops:rc:rate-limit-sentinel:{run_id}"
    result, paid_calls, routes = run(client, url, namespace, sentinel)
    evidence.update(build_evidence(result, paid_calls, routes))
    status, reason = str(evidence.pop("status")), str(evidence.pop("reason"))
    return _finish(evidence, status, reason, 0 if status == "PASS" else 1)


if __name__ == "__main__":
    raise SystemExit(main())
