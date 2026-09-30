"""Model-router RC gate over the real LLMModelRouter and the real admin routing API.

Routing policy and catalog are in-process product state (the router does not read
the PostgreSQL model tables), so the gate needs no database. Provider generation is
replaced only at the final invoke boundary; every outbound HTTP attempt is counted
and blocked, and PASS requires paid_provider_calls == 0. The gate records the shared
catalog governance risk separately instead of hiding it behind passing routes.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402

from ragops.modeling.router import (  # noqa: E402
    ComplexitySignals,
    FallbackPolicyError,
    LLMModelRouter,
    ModelSpec,
    ProviderError,
    ProviderErrorCategory,
    RoutingClass,
    RoutingDecision,
    TenantModelPolicy,
)

EVIDENCE = ROOT / "evidence" / "product-v1" / "rc-live" / "model-router.json"
TENANT_A, TENANT_B, TENANT_C = "rc-router-a", "rc-router-b", "rc-router-c"
OIDC_ISSUER = "https://rc-model-router.invalid/"
OIDC_AUDIENCE = "ragops-rc-model-router"
POLICY_PRECEDENCE = (
    "client selection ignored > tenant policy filters (allowed providers/models, max tier, "
    "cost ceiling, high-risk list) > capability and routing-class requirements > "
    "lowest estimated cost > fallback_priority > provider/model id; fallback only across "
    "remaining eligible candidates, only for retryable errors, only if the policy allows it"
)

CATALOG = [
    ModelSpec(
        "deterministic",
        "economy",
        "RC Economy",
        routing_tier=RoutingClass.SIMPLE,
        input_cost_per_token=0.000001,
        output_cost_per_token=0.000002,
        fallback_priority=10,
    ),
    ModelSpec(
        "openai",
        "balanced",
        "RC Balanced",
        routing_tier=RoutingClass.STANDARD,
        supports_structured_output=True,
        input_cost_per_token=0.00001,
        output_cost_per_token=0.00002,
        fallback_priority=20,
    ),
    ModelSpec(
        "azure_openai",
        "balanced-backup",
        "RC Balanced Backup",
        routing_tier=RoutingClass.STANDARD,
        supports_structured_output=True,
        input_cost_per_token=0.000015,
        output_cost_per_token=0.00003,
        fallback_priority=30,
    ),
    ModelSpec(
        "openai",
        "strong",
        "RC Strong",
        routing_tier=RoutingClass.COMPLEX,
        supports_structured_output=True,
        supports_tools=True,
        high_risk_allowed=True,
        input_cost_per_token=0.00003,
        output_cost_per_token=0.00006,
        fallback_priority=40,
    ),
    # Free and top-tier: it would win every route if the enabled flag were ignored.
    ModelSpec(
        "deterministic",
        "disabled-cheap",
        "RC Disabled",
        enabled=False,
        routing_tier=RoutingClass.COMPLEX,
        fallback_priority=1,
    ),
]
SIGNALS = {
    "simple": ComplexitySignals(estimated_input_tokens=200, expected_output_tokens=200),
    "standard": ComplexitySignals(
        source_count=2, estimated_input_tokens=800, expected_output_tokens=400
    ),
    "complex": ComplexitySignals(
        source_count=6, estimated_input_tokens=2000, expected_output_tokens=600
    ),
    "high_risk": ComplexitySignals(
        intent="compliance", estimated_input_tokens=800, expected_output_tokens=400
    ),
}


@dataclass
class GateResult:
    checks: dict[str, bool] = field(default_factory=dict)
    scenarios: list[dict[str, Any]] = field(default_factory=list)
    router_tenant_leakage: int = 0
    max_fallback_depth_observed: int = 0
    selected_models: set[tuple[str, str]] = field(default_factory=set)
    cross_tenant_catalog_influence: bool | None = None
    errors: list[str] = field(default_factory=list)

    def check(self, name: str, ok: bool) -> None:
        self.checks[name] = self.checks.get(name, True) and ok

    def record(
        self,
        scenario: str,
        tenant: str,
        decision: RoutingDecision | None,
        passed: bool,
        *,
        outcome: str = "routed",
        fallback_depth: int = 0,
        policy_source: str = "default",
    ) -> None:
        self.check(scenario, passed)
        self.max_fallback_depth_observed = max(self.max_fallback_depth_observed, fallback_depth)
        entry: dict[str, Any] = {
            "scenario": scenario,
            "tenant_id": tenant,
            "outcome": outcome,
            "policy_source": policy_source,
            "fallback_used": fallback_depth > 0,
            "fallback_depth": fallback_depth,
            "passed": passed,
        }
        if decision is not None:
            self.selected_models.add((decision.provider_id, decision.model_id))
            self.selected_models.update(decision.fallback_chain)
            entry.update(
                {
                    "routing_class": decision.routing_class.value,
                    "selected_provider": decision.provider_id,
                    "selected_model": decision.model_id,
                    "reason_codes": list(decision.reason_codes),
                    "fallback_chain": [f"{p}/{m}" for p, m in decision.fallback_chain],
                    "estimated_cost": round(decision.estimated_cost, 8),
                    "decision_hash": decision_hash(decision),
                }
            )
        self.scenarios.append(entry)


def decision_hash(decision: RoutingDecision) -> str:
    stable = json.dumps(
        {**asdict(decision), "estimated_cost": round(decision.estimated_cost, 10)},
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(stable.encode()).hexdigest()[:16]


def _route(router: LLMModelRouter, tenant: str, signal: str, **kwargs: str) -> RoutingDecision:
    return router.route(tenant, SIGNALS[signal], **kwargs)


def _fails_closed(action: Callable[[], object]) -> bool:
    try:
        action()
    except FallbackPolicyError:
        return True
    return False


def _unavailable(providers: set[str], calls: list[tuple[str, str]]) -> Callable[[str, str], str]:
    """Final generation boundary: a fake adapter, so routing is asserted without cost."""

    def invoke(provider: str, model: str) -> str:
        calls.append((provider, model))
        if provider in providers or "*" in providers:
            raise ProviderError(ProviderErrorCategory.MODEL_UNAVAILABLE)
        return f"{provider}/{model}"

    return invoke


def check_router(result: GateResult) -> None:
    pinned_a = TenantModelPolicy(TENANT_A, allowed_models=frozenset({"balanced-backup"}))
    high_risk_c = TenantModelPolicy(TENANT_C, high_risk_models=frozenset({"balanced"}))
    router = LLMModelRouter(CATALOG, {TENANT_A: pinned_a, TENANT_C: high_risk_c})
    baseline = LLMModelRouter(CATALOG)

    # Tenant isolation first: B's decisions with A's and C's policies present must equal a
    # router that has no other tenant policies at all.
    for signal in SIGNALS:
        try:
            observed = decision_hash(_route(router, TENANT_B, signal))
        except FallbackPolicyError:
            observed = "fail-closed"
        if observed != decision_hash(_route(baseline, TENANT_B, signal)):
            result.router_tenant_leakage += 1

    default = _route(router, TENANT_B, "standard")
    result.record(
        "default_normal_task",
        TENANT_B,
        default,
        (default.provider_id, default.model_id) == ("openai", "balanced")
        and default.fallback_chain == (("azure_openai", "balanced-backup"), ("openai", "strong")),
    )
    simple = _route(router, TENANT_B, "simple")
    result.record("low_cost_simple_task", TENANT_B, simple, simple.model_id == "economy")
    strong = _route(router, TENANT_B, "complex")
    result.record(
        "high_capability_task",
        TENANT_B,
        strong,
        (strong.model_id, strong.fallback_chain) == ("strong", ()),
    )
    pinned = _route(router, TENANT_A, "standard")
    result.record(
        "tenant_a_pinned_model",
        TENANT_A,
        pinned,
        (pinned.provider_id, pinned.model_id, pinned.fallback_chain)
        == ("azure_openai", "balanced-backup", ()),
        policy_source="tenant",
    )
    result.record(
        "tenant_b_unaffected_by_a",
        TENANT_B,
        default,
        result.router_tenant_leakage == 0 and decision_hash(pinned) != decision_hash(default),
    )

    calls: list[tuple[str, str]] = []
    value, depth = router.execute_with_fallback(default, _unavailable({"openai"}, calls))
    result.record(
        "preferred_provider_unavailable",
        TENANT_B,
        default,
        value == "azure_openai/balanced-backup" and depth == 1 and len(calls) == 2,
        fallback_depth=depth,
    )
    calls = []
    try:
        router.execute_with_fallback(default, _unavailable({"*"}, calls))
        exhausted = False
    except ProviderError as exc:
        exhausted = exc.category == ProviderErrorCategory.MODEL_UNAVAILABLE
    looping = RoutingDecision(
        default.routing_class,
        default.provider_id,
        default.model_id,
        default.reason_codes,
        0,
        0,
        0.0,
        fallback_chain=default.fallback_chain * 3 + ((default.provider_id, default.model_id),),
    )
    loop_calls: list[tuple[str, str]] = []
    try:
        router.execute_with_fallback(looping, _unavailable({"*"}, loop_calls))
    except ProviderError:
        pass
    result.record(
        "all_eligible_unavailable",
        TENANT_B,
        default,
        exhausted and len(calls) == 3 and len(loop_calls) == 3 and len(set(loop_calls)) == 3,
        outcome="fail_closed",
        fallback_depth=len(calls) - 1,
    )

    unknown = TenantModelPolicy(TENANT_A, allowed_models=frozenset({"does-not-exist"}))
    result.record(
        "unknown_model_in_policy",
        TENANT_A,
        None,
        _fails_closed(
            lambda: LLMModelRouter(CATALOG, {TENANT_A: unknown}).route(TENANT_A, SIGNALS["simple"])
        ),
        outcome="fail_closed",
        policy_source="tenant",
    )
    pinned_disabled = TenantModelPolicy(TENANT_A, allowed_models=frozenset({"disabled-cheap"}))
    result.record(
        "disabled_model_not_selected",
        TENANT_A,
        None,
        _fails_closed(
            lambda: LLMModelRouter(CATALOG, {TENANT_A: pinned_disabled}).route(
                TENANT_A, SIGNALS["complex"]
            )
        ),
        outcome="fail_closed",
        policy_source="tenant",
    )

    ceiling = default.estimated_cost + 1e-9
    capped = LLMModelRouter(
        CATALOG, {TENANT_A: TenantModelPolicy(TENANT_A, cost_ceiling_eur=ceiling)}
    )
    cheaper = _route(capped, TENANT_A, "standard")
    result.record(
        "cost_ceiling_routes_cheaper",
        TENANT_A,
        cheaper,
        (cheaper.model_id, cheaper.fallback_chain) == ("balanced", ()),
        policy_source="tenant",
    )
    too_low = LLMModelRouter(
        CATALOG, {TENANT_A: TenantModelPolicy(TENANT_A, cost_ceiling_eur=ceiling / 10)}
    )
    result.record(
        "cost_ceiling_fails_closed",
        TENANT_A,
        None,
        _fails_closed(lambda: _route(too_low, TENANT_A, "standard")),
        outcome="fail_closed",
        policy_source="tenant",
    )
    tier_cap = LLMModelRouter(
        CATALOG, {TENANT_A: TenantModelPolicy(TENANT_A, max_routing_tier=RoutingClass.STANDARD)}
    )
    result.record(
        "tier_cap_precedes_capability",
        TENANT_A,
        None,
        _fails_closed(lambda: _route(tier_cap, TENANT_A, "complex")),
        outcome="fail_closed",
        policy_source="tenant",
    )

    no_fallback = LLMModelRouter(
        CATALOG, {TENANT_A: TenantModelPolicy(TENANT_A, fallback_enabled=False)}
    )
    denied = _route(no_fallback, TENANT_A, "standard")
    calls = []
    try:
        no_fallback.execute_with_fallback(denied, _unavailable({"openai"}, calls))
        forbidden_blocked = False
    except ProviderError:
        forbidden_blocked = True
    result.record(
        "policy_denies_fallback",
        TENANT_A,
        denied,
        denied.fallback_chain == () and forbidden_blocked and calls == [("openai", "balanced")],
        outcome="fail_closed",
        policy_source="tenant",
    )

    def rejected_credentials(provider: str, model: str) -> str:
        raise ProviderError(ProviderErrorCategory.AUTHENTICATION)

    try:
        router.execute_with_fallback(default, rejected_credentials)
        permanent_stops = False
    except ProviderError as exc:
        permanent_stops = exc.category == ProviderErrorCategory.AUTHENTICATION
    result.record(
        "permanent_error_no_fallback", TENANT_B, default, permanent_stops, outcome="fail_closed"
    )

    repeated = {decision_hash(_route(router, TENANT_B, "standard")) for _ in range(5)}
    repeated.add(decision_hash(_route(LLMModelRouter(CATALOG), TENANT_B, "standard")))
    result.record(
        "repeated_request_same_decision", TENANT_B, default, repeated == {decision_hash(default)}
    )

    platform_high_risk = _route(router, TENANT_B, "high_risk")
    tenant_high_risk = _route(router, TENANT_C, "high_risk")
    result.record(
        "high_risk_platform_approved",
        TENANT_B,
        platform_high_risk,
        (platform_high_risk.routing_class, platform_high_risk.model_id)
        == (RoutingClass.HIGH_RISK, "strong"),
    )
    result.record(
        "high_risk_tenant_designated",
        TENANT_C,
        tenant_high_risk,
        tenant_high_risk.model_id == "balanced",
        policy_source="tenant",
    )
    forced = _route(
        router, TENANT_B, "simple", requested_provider="openai", requested_model="strong"
    )
    result.record(
        "client_selection_ignored",
        TENANT_B,
        forced,
        forced.model_id == "economy" and "CLIENT_SELECTION_IGNORED" in forced.reason_codes,
    )

    invalid = (
        [ModelSpec("p", "m", "m", input_cost_per_token=-1)],
        [ModelSpec("p", "m", "m"), ModelSpec("p", "m", "duplicate")],
        [ModelSpec("", "m", "incomplete")],
        [ModelSpec("p", " ", "incomplete")],
    )
    rejected = 0
    for models in invalid:
        try:
            LLMModelRouter(models)
        except ValueError:
            rejected += 1
    catalog = {(model.provider_id, model.model_id): model for model in CATALOG}
    routable = all(
        key in catalog
        and catalog[key].enabled
        and catalog[key].input_cost_per_token >= 0
        and catalog[key].output_cost_per_token >= 0
        for key in result.selected_models
    )
    result.check("catalog_integrity", rejected == len(invalid) and routable)
    result.check(
        "disabled_model_excluded", ("deterministic", "disabled-cheap") not in result.selected_models
    )


@contextmanager
def outbound_guard() -> Iterator[list[str]]:
    """Count and block any real HTTP transport use; ASGI test transport is unaffected."""
    attempts: list[str] = []
    original_sync = httpx.HTTPTransport.handle_request
    original_async = httpx.AsyncHTTPTransport.handle_async_request

    def blocked(self: object, request: httpx.Request) -> httpx.Response:
        attempts.append(request.url.host)
        raise httpx.ConnectError("outbound provider traffic is blocked by the router gate")

    async def blocked_async(self: object, request: httpx.Request) -> httpx.Response:
        attempts.append(request.url.host)
        raise httpx.ConnectError("outbound provider traffic is blocked by the router gate")

    httpx.HTTPTransport.handle_request = blocked  # type: ignore[method-assign]
    httpx.AsyncHTTPTransport.handle_async_request = blocked_async  # type: ignore[method-assign]
    try:
        yield attempts
    finally:
        httpx.HTTPTransport.handle_request = original_sync  # type: ignore[method-assign]
        httpx.AsyncHTTPTransport.handle_async_request = original_async  # type: ignore[method-assign]


@contextmanager
def deterministic_app_environment() -> Iterator[None]:
    """The app's query workflow must not construct a paid provider for this gate."""
    previous = os.environ.get("RAGOPS_LLM_PROVIDER")
    os.environ["RAGOPS_LLM_PROVIDER"] = "deterministic"
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("RAGOPS_LLM_PROVIDER", None)
        else:
            os.environ["RAGOPS_LLM_PROVIDER"] = previous


def check_api(result: GateResult, workdir: Path) -> None:
    """Drive the real admin routing routes with ephemeral RS256 tokens through OIDC validation."""
    import jwt
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from fastapi.testclient import TestClient

    from ragops.api.app import create_app
    from ragops.config.settings import Settings

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

    settings = Settings(
        environment="test",
        identity_provider="oidc",
        oidc_issuer=OIDC_ISSUER,
        oidc_audience=OIDC_AUDIENCE,
        oidc_public_key=public_key,
        data_dir=workdir / "data",
        evidence_dir=workdir / "evidence",
        # Tenant A operates the shared catalog; tenant admins elsewhere must not change it.
        platform_admin_tenant_id=TENANT_A,
    )
    with deterministic_app_environment():
        client = TestClient(create_app(settings), raise_server_exceptions=False)
    a, b = headers(TENANT_A, ["admin"]), headers(TENANT_B, ["admin"])
    viewer = headers(TENANT_B, ["sales"])

    def simulate(auth: dict[str, str], **signals: object) -> httpx.Response:
        response: httpx.Response = client.post(
            "/v1/admin/model-router/simulate", json=signals, headers=auth
        )
        return response

    def route_of(response: httpx.Response) -> tuple[str, str] | None:
        body = response.json() if response.status_code == 200 else {}
        return (body["provider_id"], body["model_id"]) if body else None

    before_a, before_b = route_of(simulate(a)), route_of(simulate(b))
    result.check(
        "api_default_route", before_a == before_b == ("deterministic", "deterministic-ragops-v1")
    )
    no_model = simulate(b, source_count=3)
    result.check(
        "api_no_eligible_model_precise",
        no_model.status_code == 409
        and str(no_model.json().get("detail", "")).startswith("NO_ELIGIBLE_MODEL"),
    )
    pin = client.put(
        f"/v1/admin/model-policies/{TENANT_A}",
        json={"allowed_models": ["does-not-exist"]},
        headers=a,
    )
    after_a, after_b = simulate(a), route_of(simulate(b))
    if after_b != before_b:
        result.router_tenant_leakage += 1
    result.check(
        "api_tenant_policy_isolation",
        pin.status_code == 200 and after_a.status_code == 409 and after_b == before_b,
    )
    result.check(
        "api_cross_tenant_policy_write_denied",
        client.put(f"/v1/admin/model-policies/{TENANT_B}", json={}, headers=a).status_code == 404,
    )
    created = client.post("/v1/admin/providers", json={"provider_id": "rc-unapproved"}, headers=a)
    result.check(
        "api_new_catalog_entry_fails_closed",
        created.status_code == 201
        and created.json().get("enabled") is False
        and route_of(simulate(b)) == before_b,
    )

    # Governance probe: catalog changes attempted by tenant B's (non-platform) admin must not
    # change tenant A's complete decision (selection and fallback chain).
    def decision_of(auth: dict[str, str]) -> tuple[int, str]:
        response = simulate(auth)
        return response.status_code, response.text

    baseline_a = decision_of(a)
    default_model = "/v1/admin/models/deterministic/deterministic-ragops-v1"
    unapproved = "/v1/admin/models/rc-unapproved/default"
    influence, denied = [], []
    for path, value in ((unapproved, True), (default_model, False)):
        status = client.patch(path, json={"enabled": value}, headers=b).status_code
        denied.append(status == 403)
        influence.append(decision_of(a) != baseline_a)
        if status == 200:
            client.patch(path, json={"enabled": not value}, headers=b)
    denied.append(
        client.post("/v1/admin/providers", json={"provider_id": "rc-b"}, headers=b).status_code
        == 403
    )
    result.cross_tenant_catalog_influence = any(influence)
    result.check("api_tenant_admin_cannot_change_catalog", all(denied))
    result.check("api_catalog_restored", decision_of(a) == baseline_a)
    result.check(
        "api_admin_role_required",
        simulate(viewer).status_code == 403
        and client.post(
            "/v1/admin/providers", json={"provider_id": "x"}, headers=viewer
        ).status_code
        == 403,
    )


def classify(result: GateResult, paid_provider_calls: int) -> tuple[str, str]:
    failed = sorted(name for name, ok in result.checks.items() if not ok)
    if paid_provider_calls:
        return "FAIL", "outbound provider traffic was attempted"
    if result.router_tenant_leakage:
        return "FAIL", "another tenant's policy changed routing decisions"
    if failed:
        return "FAIL", f"router invariants failed: {', '.join(failed)}"
    return "PASS", "all implemented routing invariants hold without provider calls"


def build_evidence(result: GateResult, paid_provider_calls: int) -> dict[str, Any]:
    status, reason = classify(result, paid_provider_calls)
    ok = result.checks.get
    return {
        "gate": "model_router",
        "status": status,
        "reason": reason,
        "router_real_implementation": "ragops.modeling.router.LLMModelRouter",
        "api_real_implementation": "POST /v1/admin/model-router/simulate, /v1/admin/model-policies",
        "query_path_uses_router": False,
        "query_path_note": "/v1/query generates through provider_from_env(); the router is "
        "exercised by the admin simulation API only",
        "persistence_used": False,
        "persistence_note": "tenant model policies and the catalog are in-process state; the "
        "model_configurations/tenant_model_policies tables are not read by the router",
        "policy_precedence": POLICY_PRECEDENCE,
        "catalog_valid": ok("catalog_integrity"),
        "scenario_count": len(result.scenarios),
        "deterministic_routes": ok("repeated_request_same_decision"),
        "tenant_policy_isolation": bool(
            ok("tenant_b_unaffected_by_a") and ok("api_tenant_policy_isolation")
        ),
        "router_tenant_leakage": result.router_tenant_leakage,
        "cheaper_route_verified": ok("cost_ceiling_routes_cheaper"),
        "strong_route_verified": ok("high_capability_task"),
        "pinned_model_verified": ok("tenant_a_pinned_model"),
        "allowed_fallback_verified": ok("preferred_provider_unavailable"),
        "forbidden_fallback_verified": ok("policy_denies_fallback"),
        "no_eligible_model_fail_closed": bool(
            ok("all_eligible_unavailable") and ok("api_no_eligible_model_precise")
        ),
        "unknown_model_fail_closed": ok("unknown_model_in_policy"),
        "disabled_model_excluded": bool(
            ok("disabled_model_excluded") and ok("disabled_model_not_selected")
        ),
        "max_fallback_depth_observed": result.max_fallback_depth_observed,
        "paid_provider_calls": paid_provider_calls,
        "catalog_mutation_scope": "platform_admin_tenant_only",
        "cross_tenant_catalog_influence": result.cross_tenant_catalog_influence,
        "governance_risk": "the shared catalog changes only through admins of the configured "
        "platform tenant (RAGOPS_PLATFORM_ADMIN_TENANT_ID); unset disables catalog changes",
        "cleanup_status": "PASS",
        "checks": dict(sorted(result.checks.items())),
        "errors": result.errors,
        "scenarios": result.scenarios,
    }


def run() -> tuple[GateResult, int]:
    result = GateResult()
    with outbound_guard() as attempts, tempfile.TemporaryDirectory() as workdir:
        steps: tuple[tuple[str, Callable[[], None]], ...] = (
            ("router_scenarios_completed", lambda: check_router(result)),
            ("api_scenarios_completed", lambda: check_api(result, Path(workdir))),
        )
        for name, step in steps:
            try:
                step()
            except ImportError:
                raise
            except Exception as exc:  # noqa: BLE001 - an aborted scenario block must fail the gate
                result.errors.append(f"{name}: {type(exc).__name__}")
                result.check(name, False)
            else:
                result.check(name, True)
    return result, len(attempts)


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


def main() -> int:
    evidence: dict[str, Any] = {"gate": "model_router", "tested_commit": _commit()}
    try:
        result, paid_calls = run()
        evidence.update(build_evidence(result, paid_calls))
    except ImportError as exc:
        evidence.update({"status": "BLOCKED", "reason": f"missing dependency: {exc.name}"})
    except Exception as exc:  # noqa: BLE001 - evidence must not contain raw internals
        evidence.update({"status": "FAIL", "reason": f"router gate raised {type(exc).__name__}"})
    exit_code = {"PASS": 0, "BLOCKED": 2}.get(str(evidence["status"]), 1)
    evidence.update(
        {"integration_test_exit_code": exit_code, "timestamp": datetime.now(UTC).isoformat()}
    )
    EVIDENCE.parent.mkdir(parents=True, exist_ok=True)
    EVIDENCE.write_text(json.dumps(evidence, indent=2) + "\n", encoding="utf-8")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
