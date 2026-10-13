"""Web frontend RC gate (ENT-12-UI): a real Chromium drives the Next.js app in apps/web.

The gate builds the production bundle, starts it with ``next start`` on a gate-owned loopback
port and points it at the gate-owned API, worker, PostgreSQL database, Redis namespace and
disposable Qdrant service of ``browser_e2e_gate.environment``. Sign-in goes through the local
Keycloak realm with Authorization Code + PKCE (enforced on the temporary client). Only synthetic
documents are used; the model provider is the deterministic local provider behind the paid-call
ledger. Keycloak users and the client are created for this run and removed afterwards.

Both UI languages (de-DE, en-US) are exercised: navigation, persistence of the preference
cookie, accessibility (axe-core, WCAG 2.2 AA rule set) and responsive screenshots. Security
journeys cover the BFF boundary (no token in the browser, CSRF, cache headers, allowlist),
tenant isolation, RBAC, session expiry, logout and an unreachable identity provider.

Evidence: ``evidence/product-v1/rc-live/web-e2e.json`` and screenshots below
``evidence/product-v1/rc-live/web-e2e/<run>/``; every artifact is scanned for credentials.
"""
# ruff: noqa: S310, S603, S607

from __future__ import annotations

import json
import re
import secrets
import shutil
import subprocess
import sys
import time
import urllib.parse
from contextlib import ExitStack, suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import browser_e2e_gate as e2e  # noqa: E402
from scripts import intelligence_fixtures as corpus  # noqa: E402
from scripts import ocr_fixtures as fx  # noqa: E402
from scripts import ocr_ingestion_gate as og  # noqa: E402
from scripts import rag_query_gate as rq  # noqa: E402

WEB = ROOT / "apps" / "web"
EVIDENCE = e2e.RC_LIVE / "web-e2e.json"
ARTIFACTS = e2e.RC_LIVE / "web-e2e"
DESKTOP = {"width": 1440, "height": 900}
TABLET = {"width": 834, "height": 1112}
MOBILE = {"width": 390, "height": 844}
VIEWPORTS = {"desktop": DESKTOP, "tablet": TABLET, "mobile": MOBILE}
UI_TIMEOUT_MS = 20_000
JOB_TIMEOUT_S = 150
# Generous for a UI tour; the limiter itself has its own gate (rate_limiting).
RATE_LIMIT = 600
SHORT_IDLE_SECONDS = 60
JWT = re.compile(r"eyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}")
AXE_TAGS = ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa"]
PAGES = {
    "cockpit": "/",
    "copilot": "/copilot",
    "knowledge": "/knowledge",
    "intelligence": "/intelligence",
    "claims": "/intelligence?tab=claims",
    "monitoring": "/monitoring",
    "finops": "/finops",
    "governance": "/governance",
}
NAV = {
    "de-DE": {
        "cockpit": "Enterprise-Cockpit",
        "governance": "Governance & Betrieb",
        "intelligence": "Dokumentenintelligenz",
    },
    "en-US": {
        "cockpit": "Enterprise Cockpit",
        "governance": "Governance & Operations",
        "intelligence": "Document Intelligence",
    },
}
UPLOAD_TITLE = "RC Web Rechnung Lindenhof"
HOSTILE_QUESTION = (
    '<img src=x onerror="window.__ragopsXss=1"><script>window.__ragopsXss=2</script>'
    " Welche Rechnungsnummer?"
)
QUESTION = "Welche Rechnungsnummer nennt die Rechnung von Versandhaus Lindenhof?"

REQUIRED = (
    "web_build_succeeded",
    "web_server_started",
    "unauthenticated_redirected_to_signin",
    "security_headers_present",
    "keycloak_login_pkce",
    "session_cookie_hardened",
    "no_tokens_in_browser",
    "bff_responses_not_cacheable",
    "csrf_missing_token_rejected",
    "csrf_cross_origin_rejected",
    "client_tenant_override_rejected",
    "forged_authorization_ignored",
    "bff_allowlist_enforced",
    "cockpit_shows_real_data",
    "language_switch_to_english",
    "language_persisted_after_reload",
    "language_preserved_on_navigation",
    "language_switch_keeps_tenant_and_session",
    "html_lang_matches_locale",
    "person_created_via_ui",
    "upload_via_ui_completed",
    "ocr_page_report_visible",
    "intelligence_document_visible",
    "source_provenance_visible",
    "source_text_not_translated",
    "review_confirm_persisted",
    "review_conflict_409_shown",
    "claims_legal_notice_both_languages",
    "copilot_cited_answer",
    "copilot_idempotency_key_sent",
    "copilot_xss_inert",
    "copilot_controlled_outcome",
    "viewer_admin_navigation_hidden",
    "viewer_admin_page_forbidden",
    "viewer_admin_api_forbidden",
    "viewer_review_controls_hidden",
    "viewer_review_api_forbidden",
    "tenant_b_list_isolated",
    "tenant_b_direct_access_denied",
    "session_expiry_handled",
    "logout_ends_session",
    "idp_unavailable_message",
    "keyboard_skip_link",
    "keyboard_mobile_menu",
    "responsive_no_horizontal_overflow",
    "accessibility_wcag22aa",
    "screenshots_both_languages",
    "no_third_party_requests",
    "no_document_text_in_console",
    "no_tokens_in_server_logs",
)


LIVE_15_CHECKS = (
    "keycloak_login_pkce",
    "session_cookie_hardened",
    "no_tokens_in_browser",
    "no_tokens_in_server_logs",
    "security_headers_present",
    "bff_responses_not_cacheable",
    "bff_allowlist_enforced",
    "csrf_missing_token_rejected",
    "csrf_cross_origin_rejected",
    "client_tenant_override_rejected",
    "forged_authorization_ignored",
    "viewer_admin_api_forbidden",
    "viewer_review_api_forbidden",
    "tenant_b_list_isolated",
    "tenant_b_direct_access_denied",
    "copilot_xss_inert",
    "review_conflict_409_shown",
    "session_expiry_handled",
    "logout_ends_session",
    "language_switch_keeps_tenant_and_session",
    "accessibility_wcag22aa",
)


@dataclass
class Web:
    url: str
    service: e2e.Service
    session_secret: str


def build_web(result: e2e.GateResult, evidence: dict[str, Any]) -> None:
    if not (WEB / "node_modules" / "next").exists():
        raise e2e.PrerequisiteMissing(
            "apps/web dependencies are not installed (npm ci in apps/web)"
        )
    if shutil.which("node") is None:
        raise e2e.PrerequisiteMissing("Node.js is not installed")
    started = time.perf_counter()
    env = {
        **e2e.base_env(),
        "NEXT_TELEMETRY_DISABLED": "1",
        "NODE_OPTIONS": "--max-old-space-size=1536",
    }
    completed = subprocess.run(
        ["node", "node_modules/next/dist/bin/next", "build"],
        cwd=WEB,
        env=env,
        capture_output=True,
        text=True,
        timeout=900,
        check=False,
    )
    evidence["web_build_seconds"] = round(time.perf_counter() - started, 1)
    evidence["node_version"] = subprocess.run(
        ["node", "--version"], capture_output=True, text=True, check=False
    ).stdout.strip()
    if not result.check("web_build_succeeded", completed.returncode == 0):
        raise RuntimeError("next build failed")


def allow_web_client(env: e2e.Environment, ports: list[int]) -> None:
    """Register the web app's callback on the gate-only client and enforce PKCE S256 there."""
    uuid = env.admin.client_uuid(env.client_id)
    if uuid is None:
        raise e2e.PrerequisiteMissing("gate Keycloak client is missing")
    status, client = env.admin.call("GET", f"/clients/{uuid}")
    if status != 200 or not isinstance(client, dict):
        raise e2e.PrerequisiteMissing("gate Keycloak client cannot be read")
    callbacks = [f"http://localhost:{port}/auth/callback" for port in ports]
    signed_out = [f"http://localhost:{port}/signin?signedOut=1" for port in ports]
    client["redirectUris"] = sorted({*client.get("redirectUris", []), *callbacks})
    attributes = dict(client.get("attributes") or {})
    logout = [
        item for item in str(attributes.get("post.logout.redirect.uris", "")).split("##") if item
    ]
    attributes["post.logout.redirect.uris"] = "##".join(sorted({*logout, *signed_out}))
    attributes["pkce.code.challenge.method"] = "S256"
    client["attributes"] = attributes
    status, _ = env.admin.call("PUT", f"/clients/{uuid}", client)
    if status not in {200, 204}:
        raise e2e.PrerequisiteMissing("gate Keycloak client cannot be updated")


def start_web(
    env: e2e.Environment,
    name: str,
    port: int,
    *,
    issuer: str | None = None,
    idle: int | None = None,
) -> Web:
    issuer = issuer or f"{e2e.KEYCLOAK_URL}/realms/{e2e.REALM}"
    web_env = {
        **e2e.base_env(),
        "NODE_ENV": "production",
        "NEXT_TELEMETRY_DISABLED": "1",
        "PORT": str(port),
        "RAGOPS_WEB_API_URL": env.api_url,
        "RAGOPS_WEB_PUBLIC_URL": f"http://localhost:{port}",
        "RAGOPS_WEB_OIDC_ISSUER": issuer,
        "RAGOPS_WEB_OIDC_CLIENT_ID": env.client_id,
        "RAGOPS_WEB_OIDC_CLIENT_SECRET": env.client_secret,
        "RAGOPS_WEB_OIDC_AUDIENCE": e2e.API_CLIENT,
        "RAGOPS_WEB_OIDC_ROLES_CLAIM": e2e.ROLES_CLAIM,
        "RAGOPS_WEB_OIDC_TENANT_CLAIM": "tenant_id",
        "RAGOPS_WEB_SESSION_SECRET": secrets.token_urlsafe(48),
    }
    session_secret = web_env["RAGOPS_WEB_SESSION_SECRET"]
    if idle is not None:
        web_env["RAGOPS_WEB_SESSION_IDLE_SECONDS"] = str(idle)
    next_bin = str(WEB / "node_modules" / "next" / "dist" / "bin" / "next")
    command = ["node", next_bin, "start", str(WEB), "-H", "127.0.0.1", "-p", str(port)]
    service = e2e.Service(name, command, web_env, env.work / "logs" / f"{name}.log")
    env.services.append(service)
    url = f"http://localhost:{port}"
    if not e2e.wait_until(
        lambda: e2e.http_status(f"http://127.0.0.1:{port}/signin") == 200, 60, 0.5
    ):
        raise RuntimeError(f"{name} did not start")
    return Web(url, service, session_secret)


# --------------------------------------------------------------------------- browser helpers


class Browser:
    def __init__(self, playwright: Any, result: e2e.GateResult, run_dir: Path) -> None:
        self.browser = playwright.chromium.launch(channel="chromium", headless=True)
        self.result, self.run_dir = result, run_dir
        self.hosts: set[str] = set()
        self.console: list[str] = []
        self.dialogs: list[str] = []
        self.version = str(self.browser.version)
        self.pages: list[Any] = []
        self.step = "start"

    def context(self, viewport: dict[str, int], locale: str = "de-DE") -> tuple[Any, Any]:
        context = self.browser.new_context(viewport=viewport, locale=locale)
        page = context.new_page()
        self.pages.append(page)
        page.set_default_timeout(UI_TIMEOUT_MS)
        page.on(
            "request", lambda request: self.hosts.add(urllib.parse.urlsplit(request.url).netloc)
        )
        page.on("console", lambda message: self.console.append(f"{message.type}: {message.text}"))
        page.on(
            "pageerror", lambda error: self.result.page_errors.append(e2e._sanitize(str(error)))
        )  # noqa: SLF001
        page.on("dialog", self._dialog)
        return context, page

    def _dialog(self, dialog: Any) -> None:
        # Any alert/confirm/prompt is unexpected: record it and dismiss so the page continues.
        self.dialogs.append(str(dialog.message))
        dialog.dismiss()

    def screenshot(self, page: Any, name: str) -> None:
        path = self.run_dir / f"{len(self.result.screenshots) + 1:02d}-{name}.png"
        page.screenshot(path=str(path), full_page=False)
        self.result.screenshots.append(path.name)

    def capture_failure(self) -> None:
        """On an aborted journey keep a screenshot of every page that is still open."""
        for index, page in enumerate(self.pages):
            with suppress(Exception):
                if not page.is_closed():
                    self.screenshot(page, f"failure-{self.step}-{index}")

    def close(self) -> None:
        self.browser.close()


def keycloak_login(page: Any, user: e2e.GateUser) -> None:
    page.wait_for_url(f"{e2e.KEYCLOAK_URL}/**")
    page.locator("#username").fill(user.username)
    page.locator("#password").fill(user.password)
    page.locator("#kc-login").click()


def sign_in(page: Any, web: Web, user: e2e.GateUser, path: str = "/") -> None:
    page.goto(f"{web.url}{path}")
    page.wait_for_url(f"{web.url}/signin**")
    page.get_by_test_id("signin").click()
    keycloak_login(page, user)
    page.wait_for_url(f"{web.url}{path if path != '/' else ''}**")
    # Visible at every width (the tenant badge and the menu button depend on the viewport).
    page.get_by_test_id("logout").wait_for()


def bff(
    page: Any,
    path: str,
    method: str = "GET",
    body: Any = None,
    headers: dict[str, str] | None = None,
    csrf: bool = True,
) -> dict[str, Any]:
    """fetch() from inside the page, i.e. exactly what the browser itself can do."""
    reply: dict[str, Any] = page.evaluate(
        """async ([path, method, body, headers, csrf]) => {
            const h = new Headers(headers || {});
            if (method !== 'GET' && csrf) {
              const s = await fetch('/api/session', {cache: 'no-store'});
              if (s.ok) h.set('x-csrf-token', (await s.json()).csrfToken);
            }
            if (body !== null) h.set('content-type', 'application/json');
            const init = {method, headers: h, cache: 'no-store'};
            if (body !== null) init.body = JSON.stringify(body);
            const r = await fetch('/api/bff' + path, init);
            let payload = null; const text = await r.text();
            try { payload = JSON.parse(text); } catch { payload = text; }
            return {status: r.status, cacheControl: r.headers.get('cache-control'), body: payload};
        }""",
        [path, method, body, headers or {}, csrf],
    )
    return reply


def no_horizontal_overflow(page: Any) -> bool:
    return bool(page.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1"))


def axe(page: Any) -> list[dict[str, Any]]:
    page.add_script_tag(path=str(WEB / "node_modules" / "axe-core" / "axe.min.js"))
    report = page.evaluate(
        "async (tags) => (await axe.run(document, {runOnly: {type: 'tag', values: tags}}))"
        ".violations"
        ".map(v => ({id: v.id, impact: v.impact, nodes: v.nodes.length, help: v.help}))",
        AXE_TAGS,
    )
    return list(report)


def set_locale(page: Any, locale: str) -> None:
    page.get_by_test_id(f"locale-{locale}").click()
    page.wait_for_function("(l) => document.documentElement.lang === l", arg=locale)


def wait_settled(page: Any) -> None:
    page.wait_for_load_state("networkidle")
    with suppress(Exception):
        page.wait_for_function(
            "() => !document.querySelector('[role=status][aria-label]')", timeout=10_000
        )


# --------------------------------------------------------------------------- data helpers


def job_for(env: e2e.Environment, label: str, title: str) -> dict[str, Any]:
    _, jobs = env.api("/v1/ingestion/jobs", label)
    return next((job for job in jobs or [] if job.get("title") == title), {})


def upload_api(env: e2e.Environment, label: str, letter_key: str, scanned: bool) -> dict[str, Any]:
    letter = corpus.by_key(letter_key)
    pages = [list(page) for page in letter.pages]
    content = fx.scanned_pdf(pages) if scanned else fx.native_pdf(pages)
    status, accepted = og.upload(
        env, label, f"RC Web {letter_key}.pdf", "application/pdf", content, f"rc-web-{letter_key}"
    )
    if status != 202 or not accepted:
        raise RuntimeError(f"support upload {letter_key} was not accepted")
    job = og.wait_for_job(env, label, accepted["job_id"])
    return {**accepted, "status": job.get("status")}


# --------------------------------------------------------------------------- journeys


def journey_entry_and_security(
    b: Browser, web: Web, env: e2e.Environment, result: e2e.GateResult, evidence: dict[str, Any]
) -> tuple[Any, Any]:
    context, page = b.context(DESKTOP)
    response = page.goto(f"{web.url}/")
    result.check(
        "unauthenticated_redirected_to_signin",
        "/signin" in page.url and page.get_by_test_id("signin").is_visible(),
    )
    headers = {
        key.lower(): value for key, value in (response.all_headers() if response else {}).items()
    }
    csp = headers.get("content-security-policy", "")
    result.check(
        "security_headers_present",
        "nonce-" in csp
        and "frame-ancestors 'none'" in csp
        and "object-src 'none'" in csp
        and headers.get("x-frame-options") == "DENY"
        and headers.get("x-content-type-options") == "nosniff"
        and headers.get("referrer-policy") == "no-referrer"
        and "no-store" in headers.get("cache-control", ""),
    )
    b.screenshot(page, "signin-de-desktop")
    set_locale(page, "en-US")
    b.screenshot(page, "signin-en-desktop")
    set_locale(page, "de-DE")

    page.get_by_test_id("signin").click()
    login_url = page.url
    keycloak_login(page, env.users["a-admin"])
    page.wait_for_url(f"{web.url}/**")
    page.get_by_test_id("tenant-badge").wait_for()
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(login_url).query)
    result.check(
        "keycloak_login_pkce",
        query.get("code_challenge_method") == ["S256"]
        and bool(query.get("code_challenge"))
        and "client_secret" not in login_url,
    )
    cookies = {cookie["name"]: cookie for cookie in context.cookies()}
    session = cookies.get("__Host-ragops-session", {})
    result.check(
        "session_cookie_hardened",
        bool(session)
        and session.get("httpOnly") is True
        and session.get("secure") is True
        and session.get("sameSite") == "Lax"
        and session.get("path") == "/"
        and "__Host-ragops-login" not in cookies,
    )
    evidence["browser_cookie_names"] = sorted(cookies)
    return context, page


def journey_token_boundary(
    b: Browser, page: Any, context: Any, web: Web, env: e2e.Environment, result: e2e.GateResult
) -> None:
    bodies: list[str] = []

    def capture(response: Any) -> None:
        if response.url.startswith(web.url) and response.request.resource_type in {
            "document",
            "fetch",
            "xhr",
            "script",
        }:
            with suppress(Exception):
                bodies.append(response.text())

    page.on("response", capture)
    page.goto(f"{web.url}/")
    wait_settled(page)
    page.goto(f"{web.url}/governance")
    wait_settled(page)
    page.remove_listener("response", capture)
    storage = page.evaluate(
        "() => JSON.stringify({c: document.cookie, l: {...localStorage}, s: {...sessionStorage}})"
    )
    readable_cookie = page.evaluate("() => document.cookie")
    result.check(
        "no_tokens_in_browser",
        not JWT.search(storage)
        and "__Host-ragops-session" not in readable_cookie
        and not any(JWT.search(body) for body in bodies)
        and not any(env.client_secret in body for body in bodies),
    )
    listed = bff(page, "/v1/workspaces")
    result.check(
        "bff_responses_not_cacheable",
        listed["status"] == 200 and "no-store" in (listed["cacheControl"] or ""),
    )
    no_csrf = bff(page, "/v1/query", "POST", {"question": "x", "top_k": 3}, csrf=False)
    result.check(
        "csrf_missing_token_rejected",
        no_csrf["status"] == 403 and no_csrf["body"].get("error") == "csrf_rejected",
    )
    override = bff(page, "/v1/workspaces?tenant_id=tenant-beta")
    result.check("client_tenant_override_rejected", override["status"] == 400)
    forged = bff(
        page,
        "/v1/workspaces",
        headers={"Authorization": "Bearer forged.token.value", "X-Tenant-Id": e2e.TENANT_B},
    )
    tenants = (
        {item.get("tenant_id") for item in forged["body"]}
        if isinstance(forged["body"], list)
        else set()
    )
    result.check(
        "forged_authorization_ignored", forged["status"] == 200 and tenants == {e2e.TENANT_A}
    )
    blocked = [
        bff(page, "/v1/admin/providers", "POST", {"provider_id": "evil"})["status"],
        bff(page, "/v1/documents/ingest", "POST", {})["status"],
        bff(page, "/v1/admin/finops/usage")["status"],
    ]
    result.check("bff_allowlist_enforced", blocked == [404, 404, 404])
    # A different origin (127.0.0.1 is another site than localhost) cannot use the session.
    other = context.new_page()
    other.goto(f"{web.url.replace('localhost', '127.0.0.1')}/signin")
    cross = other.evaluate(
        """async (target) => { try {
              const r = await fetch(target + '/api/bff/v1/query', {
                method: 'POST', credentials: 'include',
                headers: {'content-type': 'application/json'},
                body: JSON.stringify({question: 'x', top_k: 3})});
              return r.status; } catch (e) { return -1; } }""",
        web.url,
    )
    other.close()
    result.check("csrf_cross_origin_rejected", cross in {-1, 401, 403})


def journey_language(b: Browser, page: Any, context: Any, web: Web, result: e2e.GateResult) -> None:
    page.goto(f"{web.url}/")
    page.get_by_role("heading", name=NAV["de-DE"]["cockpit"]).wait_for()
    tenant_before = page.get_by_test_id("tenant-badge").get_attribute("data-tenant")
    session_before = next(
        c["value"] for c in context.cookies() if c["name"] == "__Host-ragops-session"
    )
    set_locale(page, "en-US")
    page.get_by_role("heading", name=NAV["en-US"]["cockpit"]).wait_for()
    result.check(
        "language_switch_to_english",
        page.get_by_test_id("nav-governance").inner_text().strip() == NAV["en-US"]["governance"]
        and page.url.rstrip("/") == web.url,
    )
    session_after = next(
        c["value"] for c in context.cookies() if c["name"] == "__Host-ragops-session"
    )
    result.check(
        "language_switch_keeps_tenant_and_session",
        page.get_by_test_id("tenant-badge").get_attribute("data-tenant") == tenant_before
        and tenant_before == e2e.TENANT_A
        and session_after == session_before,
    )
    page.reload()
    page.get_by_role("heading", name=NAV["en-US"]["cockpit"]).wait_for()
    result.check(
        "language_persisted_after_reload", page.evaluate("document.documentElement.lang") == "en-US"
    )
    page.get_by_test_id("nav-intelligence").click()
    page.get_by_role("heading", name=NAV["en-US"]["intelligence"], exact=True).wait_for()
    locale_cookie: dict[str, Any] = next(
        (c for c in context.cookies() if c["name"] == "ragops-locale"), {}
    )
    result.check(
        "language_preserved_on_navigation",
        page.get_by_test_id("tab-claims").inner_text().strip() == "Claims"
        and locale_cookie.get("value") == "en-US"
        and locale_cookie.get("httpOnly") is False,
    )
    set_locale(page, "de-DE")
    page.get_by_role("heading", name=NAV["de-DE"]["intelligence"], exact=True).wait_for()
    result.check(
        "html_lang_matches_locale", page.evaluate("document.documentElement.lang") == "de-DE"
    )


def journey_documents(
    b: Browser,
    page: Any,
    web: Web,
    env: e2e.Environment,
    result: e2e.GateResult,
    evidence: dict[str, Any],
) -> dict[str, str]:
    # Person profile through the UI, so debtor attribution has a profile to match.
    page.goto(f"{web.url}/intelligence?tab=persons")
    page.locator("#person-label").fill("Person A")
    page.get_by_role("button", name="Anlegen", exact=True).click()
    page.get_by_role("cell", name="Person A", exact=True).wait_for()
    _, persons = env.api("/v1/intelligence/persons", "a-admin")
    person = next((item for item in persons or [] if item.get("label") == "Person A"), None)
    result.check("person_created_via_ui", person is not None)
    if person:
        page.get_by_role("row").filter(has_text="Person A").get_by_role(
            "button", name="Alias hinzufügen", exact=True
        ).click()
        page.locator("#alias-value").fill(corpus.PERSONS["Person A"][0])
        page.get_by_role("dialog").get_by_role("button", name="Speichern", exact=True).click()
        page.get_by_role("dialog").wait_for(state="hidden")

    # Upload through the UI: a real multipart POST through the BFF to the API and worker.
    letter = corpus.by_key("invoice_c1")
    pdf = env.work / "rc-web-invoice.pdf"
    pdf.write_bytes(fx.native_pdf([list(p) for p in letter.pages]))
    page.goto(f"{web.url}/knowledge")
    page.wait_for_function("() => document.querySelector('#collection')?.value")
    page.get_by_test_id("upload-title").fill(UPLOAD_TITLE)
    page.get_by_test_id("upload-file").set_input_files(str(pdf))
    page.get_by_test_id("upload-submit").click()
    row = page.locator(f"[data-testid=job-row][data-title='{UPLOAD_TITLE}']")
    row.wait_for()
    finished = e2e.wait_until(
        lambda: row.get_attribute("data-status") in {"completed", "failed"}, JOB_TIMEOUT_S, 1.0
    )
    status = row.get_attribute("data-status")
    result.check("upload_via_ui_completed", finished and status == "completed")
    b.screenshot(page, "knowledge-de-desktop")

    # Two more synthetic letters of the same claim through the API, one of them a scan (OCR).
    reminder = upload_api(env, "a-admin", "reminder_c1", scanned=True)
    inkasso = upload_api(env, "a-admin", "inkasso_c1", scanned=False)
    evidence["support_uploads"] = [reminder["status"], inkasso["status"]]
    page.reload()
    scan_row = page.locator("[data-testid=job-row][data-title='RC Web reminder_c1.pdf']")
    scan_row.get_by_role("button").first.click()
    dialog = page.get_by_test_id("document-details")
    dialog.wait_for()
    dialog.locator("[data-testid=page-row]").first.wait_for()
    result.check(
        "ocr_page_report_visible",
        reminder["status"] == "completed" and dialog.locator("[data-testid=page-row]").count() >= 1,
    )
    b.screenshot(page, "ocr-page-report-de-desktop")
    page.keyboard.press("Escape")

    job = job_for(env, "a-admin", UPLOAD_TITLE)
    return {
        "document_id": str(job.get("document_id", "")),
        "reminder": str(reminder["document_id"]),
        "inkasso": str(inkasso["document_id"]),
    }


def journey_intelligence(
    b: Browser,
    page: Any,
    web: Web,
    env: e2e.Environment,
    result: e2e.GateResult,
    documents: dict[str, str],
) -> None:
    document_id = documents["document_id"]
    page.goto(f"{web.url}/intelligence")
    link = page.get_by_role("link", name=UPLOAD_TITLE)
    link.wait_for()
    result.check("intelligence_document_visible", link.is_visible())
    b.screenshot(page, "intelligence-de-desktop")
    link.click()
    page.get_by_test_id("facts-table").wait_for()
    b.screenshot(page, "intelligence-detail-de-desktop")

    # Provenance of the invoice number: raw source text, page and the honest no-viewer note.
    row = page.locator("[data-testid=fact-row][data-fact-type=invoice_number]").first
    row.get_by_test_id("fact-source").click()
    source = page.get_by_test_id("source-dialog")
    raw = source.get_by_test_id("source-raw")
    raw.wait_for()
    raw_de = raw.inner_text()
    result.check(
        "source_provenance_visible",
        "VL-2026-0147" in raw_de
        and source.get_by_test_id("source-page").inner_text().strip() == "1",
    )
    b.screenshot(page, "source-provenance-de-desktop")
    page.keyboard.press("Escape")
    set_locale(page, "en-US")
    row.get_by_test_id("fact-source").click()
    raw_en = page.get_by_test_id("source-raw").inner_text()
    result.check(
        "source_text_not_translated",
        raw_en == raw_de
        and "original text" in page.get_by_test_id("source-dialog").inner_text().lower(),
    )
    b.screenshot(page, "source-provenance-en-desktop")
    page.keyboard.press("Escape")
    set_locale(page, "de-DE")

    # Confirm the creditor through the review dialog and verify the persisted decision.
    _, detail = env.api(f"/v1/intelligence/documents/{document_id}", "a-admin")
    fact = next(
        (f for f in (detail or {}).get("facts", []) if f.get("fact_type") == "creditor"), None
    )
    if fact is None:
        result.check("review_confirm_persisted", False)
        result.check("review_conflict_409_shown", False)
        return
    target = page.locator("[data-testid=fact-row][data-fact-type=creditor]").first
    target.get_by_test_id("fact-review").click()
    dialog = page.get_by_test_id("review-dialog")
    dialog.get_by_test_id("review-action").select_option("confirm")
    dialog.get_by_test_id("review-submit").click()
    dialog.wait_for(state="hidden")
    target.get_by_test_id("fact-review-state").filter(has_text="Bestätigt").wait_for()
    _, after = env.api(f"/v1/intelligence/facts/{fact['id']}", "a-admin")
    result.check(
        "review_confirm_persisted",
        isinstance(after, dict)
        and after.get("review_state") == "confirmed"
        and after.get("review_version") == fact["review_version"] + 1,
    )

    # Optimistic concurrency: another reviewer decides first; this browser holds a stale version.
    debtor = next(
        (f for f in detail.get("facts", []) if f.get("fact_type") == "invoice_number"), None
    )
    if debtor is None:
        result.check("review_conflict_409_shown", False)
        return
    stale = page.locator("[data-testid=fact-row][data-fact-type=invoice_number]").first
    stale.get_by_test_id("fact-review").click()
    dialog.wait_for()
    status, _ = env.api(
        "/v1/intelligence/reviews",
        "a-admin",
        method="POST",
        data={
            "entity_type": "fact",
            "entity_id": debtor["id"],
            "action": "confirm",
            "expected_version": debtor["review_version"],
            "reason_category": "verified_against_source",
        },
    )
    dialog.get_by_test_id("review-action").select_option("reject")
    dialog.get_by_test_id("review-reason").select_option("extraction_error")
    dialog.get_by_test_id("review-submit").click()
    conflict = dialog.get_by_text("Zwischenzeitlich geändert")
    conflict.wait_for()
    b.screenshot(page, "review-conflict-de-desktop")
    _, final = env.api(f"/v1/intelligence/facts/{debtor['id']}", "a-admin")
    result.check(
        "review_conflict_409_shown",
        status == 200
        and conflict.is_visible()
        and isinstance(final, dict)
        and final.get("review_state") == "confirmed",
    )
    page.keyboard.press("Escape")

    # Claims: the legal notice is present in both languages.
    page.goto(f"{web.url}/intelligence?tab=claims")
    notice = page.get_by_test_id("legal-notice").first
    notice.wait_for()
    de_text = notice.inner_text()
    b.screenshot(page, "claims-de-desktop")
    set_locale(page, "en-US")
    page.get_by_test_id("legal-notice").first.filter(has_text="not a legal assessment").wait_for()
    en_text = page.get_by_test_id("legal-notice").first.inner_text()
    b.screenshot(page, "claims-en-desktop")
    set_locale(page, "de-DE")
    result.check(
        "claims_legal_notice_both_languages",
        "keine rechtliche Bewertung" in de_text and "not a legal assessment" in en_text,
    )


def journey_copilot(b: Browser, page: Any, web: Web, result: e2e.GateResult) -> None:
    sent_keys: list[str] = []

    def capture(request: Any) -> None:
        if request.url.endswith("/api/bff/v1/query") and request.method == "POST":
            sent_keys.append(request.headers.get("idempotency-key", ""))

    page.on("request", capture)
    page.goto(f"{web.url}/copilot")
    # The local catalog has one SIMPLE-tier deterministic model; one source keeps the question
    # in that class (as the rag_query gate does with a single document).
    page.get_by_test_id("top-k").select_option("1")
    page.get_by_test_id("question").fill(QUESTION)
    page.get_by_test_id("ask").click()
    answer = page.locator(
        "[data-testid=answer], [data-testid^=refusal-], [data-testid=answer-error]"
    ).last
    answer.wait_for(timeout=60_000)
    citations = page.get_by_test_id("citations").last
    cited = citations.count() > 0 and citations.locator("li").count() > 0
    pages_shown = page.get_by_test_id("citation-page").count() > 0
    result.check(
        "copilot_cited_answer",
        page.get_by_test_id("answer-text").count() > 0 and cited and pages_shown,
    )
    b.screenshot(page, "copilot-de-desktop")
    page.get_by_test_id("question").fill(HOSTILE_QUESTION)
    page.get_by_test_id("ask").click()
    e2e.wait_until(
        lambda: page.get_by_test_id("turn").count() >= 2
        and page.get_by_test_id("answer-pending").count() == 0,
        60,
        0.5,
    )
    # Eight sources make it a STANDARD-class question: the API refuses it (no eligible model)
    # and the UI must explain that refusal instead of failing.
    page.get_by_test_id("top-k").select_option("8")
    page.get_by_test_id("question").fill(
        "Welche Beträge nennen alle Schreiben von Versandhaus Lindenhof?"
    )
    page.get_by_test_id("ask").click()
    e2e.wait_until(
        lambda: page.get_by_test_id("turn").count() >= 3
        and page.get_by_test_id("answer-pending").count() == 0,
        60,
        0.5,
    )
    last = page.get_by_test_id("turn").last
    refused = last.locator("[data-testid^=refusal-]").count() == 1
    answered = last.get_by_test_id("answer").count() == 1
    result.check(
        "copilot_controlled_outcome",
        (refused or answered) and last.get_by_test_id("answer-error").count() == 0,
    )
    evidence_outcome = "refusal" if refused else "answer" if answered else "error"
    b.screenshot(page, f"copilot-{evidence_outcome}-de-desktop")
    page.remove_listener("request", capture)
    xss = page.evaluate(
        "() => window.__ragopsXss === undefined && !document.querySelector("
        "'[data-testid=conversation] img, [data-testid=conversation] script')"
    )
    result.check("copilot_xss_inert", bool(xss) and not b.dialogs)
    result.check(
        "copilot_idempotency_key_sent",
        len(sent_keys) == 3
        and all(re.fullmatch(r"ui-[0-9a-f-]{36}", key) for key in sent_keys)
        and len(set(sent_keys)) == 3,
    )
    set_locale(page, "en-US")
    b.screenshot(page, "copilot-en-desktop")
    set_locale(page, "de-DE")


def journey_viewer(
    b: Browser, web: Web, env: e2e.Environment, result: e2e.GateResult, documents: dict[str, str]
) -> None:
    context, page = b.context(DESKTOP)
    sign_in(page, web, env.users["a-viewer"])
    nav = page.get_by_test_id("sidebar")
    result.check(
        "viewer_admin_navigation_hidden",
        nav.get_by_test_id("nav-cockpit").is_visible()
        and nav.get_by_test_id("nav-governance").count() == 0
        and nav.get_by_test_id("nav-finops").count() == 0
        and nav.get_by_test_id("nav-monitoring").count() == 0,
    )
    page.goto(f"{web.url}/governance")
    result.check("viewer_admin_page_forbidden", page.get_by_test_id("forbidden-page").is_visible())
    b.screenshot(page, "viewer-forbidden-de-desktop")
    audit = bff(page, "/v1/audit-events")
    budgets = bff(
        page,
        "/v1/admin/budgets",
        "POST",
        {"budget_amount": 5, "currency": "EUR", "enforcement_mode": "monitor"},
    )
    result.check("viewer_admin_api_forbidden", audit["status"] == 403 and budgets["status"] == 403)
    page.goto(f"{web.url}/intelligence/documents/{documents['document_id']}")
    page.get_by_test_id("facts-table").wait_for()
    result.check(
        "viewer_review_controls_hidden",
        page.get_by_test_id("fact-review").count() == 0
        and page.get_by_test_id("review-document").count() == 0,
    )
    _, detail = env.api(f"/v1/intelligence/documents/{documents['document_id']}", "a-admin")
    fact: dict[str, Any] = next(iter((detail or {}).get("facts", [])), {})
    review = bff(
        page,
        "/v1/intelligence/reviews",
        "POST",
        {
            "entity_type": "fact",
            "entity_id": fact.get("id", "x"),
            "action": "reject",
            "expected_version": fact.get("review_version", 1),
            "reason_category": "other",
        },
    )
    result.check("viewer_review_api_forbidden", review["status"] == 403)
    context.close()


def journey_tenant_b(
    b: Browser, web: Web, env: e2e.Environment, result: e2e.GateResult, documents: dict[str, str]
) -> None:
    context, page = b.context(MOBILE)
    sign_in(page, web, env.users["b-admin"])
    listed = bff(page, "/v1/intelligence/documents")
    ids = (
        {item.get("document_id") for item in listed["body"]}
        if isinstance(listed["body"], list)
        else {"?"}
    )
    jobs = bff(page, "/v1/ingestion/jobs")
    titles = (
        {item.get("title") for item in jobs["body"]} if isinstance(jobs["body"], list) else {"?"}
    )
    leaked = len(ids & set(documents.values())) + (1 if UPLOAD_TITLE in titles else 0)
    result.tenant_leakage += leaked
    page.goto(f"{web.url}/intelligence")
    page.wait_for_load_state("networkidle")
    result.check(
        "tenant_b_list_isolated",
        listed["status"] == 200
        and jobs["status"] == 200
        and leaked == 0
        and page.get_by_text(UPLOAD_TITLE).count() == 0,
    )
    direct = bff(page, f"/v1/intelligence/documents/{documents['document_id']}")
    page.goto(f"{web.url}/intelligence/documents/{documents['document_id']}")
    page.get_by_text("Nicht gefunden").first.wait_for()
    b.screenshot(page, "tenant-b-denied-de-mobile")
    result.check(
        "tenant_b_direct_access_denied",
        direct["status"] == 404 and page.get_by_test_id("facts-table").count() == 0,
    )
    context.close()


def journey_session_expiry(
    b: Browser, short: Web, env: e2e.Environment, result: e2e.GateResult
) -> None:
    context, page = b.context(DESKTOP)
    sign_in(page, short, env.users["a-support"], "/copilot")
    page.wait_for_load_state("networkidle")
    # No request for longer than the idle limit; the next API call meets an ended session.
    time.sleep(SHORT_IDLE_SECONDS + 5)
    page.get_by_test_id("question").fill("Gibt es offene Fristen?")
    page.get_by_test_id("ask").click()
    dialog = page.get_by_test_id("session-expired-dialog")
    dialog.wait_for()
    b.screenshot(page, "session-expired-de-desktop")
    page.get_by_test_id("session-expired-signin").click()
    # The identity provider session is still alive: a fresh BFF session starts without a
    # password.
    page.wait_for_url(f"{short.url}/copilot**")
    page.get_by_test_id("question").wait_for()
    session = page.evaluate("async () => (await fetch('/api/session')).status")
    result.check("session_expiry_handled", session == 200)
    context.close()


def journey_logout(b: Browser, page: Any, context: Any, web: Web, result: e2e.GateResult) -> None:
    page.goto(f"{web.url}/")
    page.get_by_test_id("logout").click()
    page.wait_for_url(f"{web.url}/signin?signedOut=1**")
    signed_out = page.get_by_text("Abgemeldet").first.is_visible()
    b.screenshot(page, "signed-out-de-desktop")
    session_status = page.evaluate("async () => (await fetch('/api/session')).status")
    page.goto(f"{web.url}/")
    redirected = "/signin" in page.url
    page.get_by_test_id("signin").click()
    # The provider session ended too: the login form is shown again.
    page.wait_for_url(f"{e2e.KEYCLOAK_URL}/**")
    form = page.locator("#username").is_visible()
    result.check(
        "logout_ends_session", signed_out and session_status == 401 and redirected and form
    )
    context.close()


def journey_idp_down(b: Browser, down: Web, result: e2e.GateResult) -> None:
    context, page = b.context(DESKTOP)
    page.goto(f"{down.url}/signin")
    page.get_by_test_id("signin").click()
    page.wait_for_url(f"{down.url}/signin?error=idp_unavailable**")
    # Next.js renders its own (empty) route announcer with role=alert; read the page's alert.
    message = page.locator("main [role=alert]").inner_text()
    b.screenshot(page, "idp-unavailable-de-desktop")
    set_locale(page, "en-US")
    english = page.locator("main [role=alert]").inner_text()
    result.check(
        "idp_unavailable_message",
        "Anmeldedienst nicht erreichbar" in message and "Sign-in service unavailable" in english,
    )
    context.close()


def journey_keyboard(b: Browser, web: Web, env: e2e.Environment, result: e2e.GateResult) -> None:
    context, page = b.context(DESKTOP)
    sign_in(page, web, env.users["a-admin"])
    page.keyboard.press("Tab")
    skip = page.get_by_role("link", name="Zum Inhalt springen")
    focused = skip.evaluate("(el) => el === document.activeElement")
    skip.press("Enter")
    main_focused = page.evaluate(
        "() => document.activeElement && document.activeElement.id === 'main'"
    )
    result.check("keyboard_skip_link", bool(focused) and bool(main_focused))
    context.close()
    context, page = b.context(MOBILE)
    sign_in(page, web, env.users["a-admin"])
    menu = page.get_by_test_id("open-menu")
    menu.focus()
    page.keyboard.press("Enter")
    nav = page.get_by_role("dialog").get_by_test_id("nav-copilot")
    nav.wait_for()
    b.screenshot(page, "mobile-menu-de-mobile")
    page.keyboard.press("Escape")
    page.get_by_role("dialog").wait_for(state="hidden")
    result.check("keyboard_mobile_menu", menu.evaluate("(el) => el === document.activeElement"))
    context.close()


def journey_tour(
    b: Browser,
    web: Web,
    env: e2e.Environment,
    result: e2e.GateResult,
    evidence: dict[str, Any],
    documents: dict[str, str],
) -> None:
    """Every page in both languages: axe (WCAG 2.2 AA tags), overflow and screenshots."""
    violations: list[dict[str, Any]] = []
    overflow: list[str] = []
    shots = {"de-DE": 0, "en-US": 0}
    pages = {**PAGES, "document": f"/intelligence/documents/{documents['document_id']}"}
    for viewport_name, viewport in VIEWPORTS.items():
        context, page = b.context(viewport)
        sign_in(page, web, env.users["a-admin"])
        for locale in ("de-DE", "en-US"):
            context.add_cookies([{"name": "ragops-locale", "value": locale, "url": web.url}])
            for name, path in pages.items():
                page.goto(f"{web.url}{path}")
                wait_settled(page)
                if viewport_name != "desktop" and not no_horizontal_overflow(page):
                    overflow.append(f"{name}@{viewport_name}/{locale}")
                if viewport_name == "desktop" or name in {
                    "cockpit",
                    "copilot",
                    "document",
                    "knowledge",
                }:
                    b.screenshot(page, f"{name}-{locale[:2]}-{viewport_name}")
                    shots[locale] += 1
                if viewport_name in {"desktop", "mobile"}:
                    for violation in axe(page):
                        violations.append(
                            {**violation, "page": name, "viewport": viewport_name, "locale": locale}
                        )
        context.close()
    context, page = b.context(MOBILE)
    page.goto(f"{web.url}/signin")
    for violation in axe(page):
        violations.append({**violation, "page": "signin", "viewport": "mobile", "locale": "de-DE"})
    if not no_horizontal_overflow(page):
        overflow.append("signin@mobile")
    context.close()
    evidence["accessibility_violations"] = violations
    evidence["overflow_pages"] = overflow
    evidence["screenshots_per_locale"] = shots
    result.check("accessibility_wcag22aa", not violations)
    result.check("responsive_no_horizontal_overflow", not overflow)
    result.check("screenshots_both_languages", shots["de-DE"] >= 10 and shots["en-US"] >= 10)


def journey_cockpit(
    b: Browser, page: Any, web: Web, env: e2e.Environment, result: e2e.GateResult
) -> None:
    page.goto(f"{web.url}/")
    metric = page.get_by_test_id("metric-jobs")
    e2e.wait_until(lambda: metric.inner_text().strip() not in {"…", ""}, 20, 0.5)
    _, jobs = env.api("/v1/ingestion/jobs", "a-admin")
    shown = metric.inner_text().strip()
    result.check(
        "cockpit_shows_real_data",
        isinstance(jobs, list)
        and shown == str(len(jobs))
        and page.get_by_test_id("readiness-status").is_visible(),
    )
    b.screenshot(page, "cockpit-de-desktop")


def run_journeys(
    b: Browser,
    web: Web,
    short: Web,
    down: Web,
    env: e2e.Environment,
    result: e2e.GateResult,
    evidence: dict[str, Any],
) -> None:
    b.step = "entry_and_security"
    context, page = journey_entry_and_security(b, web, env, result, evidence)
    b.step = "token_boundary"
    journey_token_boundary(b, page, context, web, env, result)
    b.step = "language"
    journey_language(b, page, context, web, result)
    b.step = "documents"
    documents = journey_documents(b, page, web, env, result, evidence)
    b.step = "intelligence"
    journey_intelligence(b, page, web, env, result, documents)
    b.step = "copilot"
    journey_copilot(b, page, web, result)
    b.step = "cockpit"
    journey_cockpit(b, page, web, env, result)
    b.step = "viewer"
    journey_viewer(b, web, env, result, documents)
    b.step = "tenant_b"
    journey_tenant_b(b, web, env, result, documents)
    b.step = "keyboard"
    journey_keyboard(b, web, env, result)
    b.step = "tour"
    journey_tour(b, web, env, result, evidence, documents)
    b.step = "idp_down"
    journey_idp_down(b, down, result)
    b.step = "session_expiry"
    journey_session_expiry(b, short, env, result)
    b.step = "logout"
    journey_logout(b, page, context, web, result)


# --------------------------------------------------------------------------- gate


def classify(result: e2e.GateResult) -> tuple[str, str]:
    missing = [name for name in REQUIRED if name not in result.checks]
    failed = sorted(name for name, ok in result.checks.items() if not ok)
    if result.paid_provider_calls:
        return "FAIL", "a paid provider was constructed during the web run"
    if result.tenant_leakage:
        return "FAIL", "tenant data reached another tenant's browser session"
    if failed or missing:
        return "FAIL", f"web e2e invariants failed: {', '.join(failed + missing)}"
    return "PASS", "the web frontend passed every journey in both languages through the real stack"


def run(result: e2e.GateResult, evidence: dict[str, Any]) -> None:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise e2e.PrerequisiteMissing(
            "playwright is not installed (pip install -e .[e2e])"
        ) from exc
    if not (WEB / "node_modules" / "axe-core" / "axe.min.js").exists():
        raise e2e.PrerequisiteMissing("axe-core is missing in apps/web/node_modules")
    build_web(result, evidence)
    run_dir = ARTIFACTS / str(evidence["run_id"])
    run_dir.mkdir(parents=True, exist_ok=True)
    secrets_seen: list[str] = []
    # The web copilot sends an Idempotency-Key with every question; like a production API, the
    # gate API therefore has the HMAC key that durable query idempotency requires.
    hmac_key = secrets.token_urlsafe(48)
    with ExitStack() as stack:
        env = stack.enter_context(
            e2e.environment(
                result,
                evidence,
                query_mode="vector",
                with_dashboard=False,
                idp_down_dashboard=False,
                rate_limit=RATE_LIMIT,
                api_env={"RAGOPS_IDEMPOTENCY_HMAC_KEY": hmac_key},
            )
        )
        playwright = stack.enter_context(sync_playwright())
        ports = [e2e.free_port(), e2e.free_port(), e2e.free_port()]
        allow_web_client(env, ports)
        # As an operator would: user rows, the deterministic model and a budget per tenant,
        # without which the productive query path refuses to answer (fail closed).
        store = rq.Store(env)
        try:
            rq.provision_accounting(
                env,
                store,
                [rq.Tenancy("a", e2e.TENANT_A, "", ""), rq.Tenancy("b", e2e.TENANT_B, "", "")],
            )
        finally:
            store.close()
        web = start_web(env, "web", ports[0])
        short = start_web(env, "web-short-idle", ports[1], idle=SHORT_IDLE_SECONDS)
        down = start_web(
            env,
            "web-idp-down",
            ports[2],
            issuer=f"http://127.0.0.1:{e2e.free_port()}/realms/{e2e.REALM}",
        )
        result.check("web_server_started", True)
        secrets_seen = [
            env.client_secret,
            web.session_secret,
            hmac_key,
            short.session_secret,
            down.session_secret,
            *(user.password for user in env.users.values()),
        ]
        evidence["web_start_command_sanitized"] = (
            "node node_modules/next/dist/bin/next start -H 127.0.0.1 -p <gate-port> (apps/web)"
        )
        try:
            b = Browser(playwright, result, run_dir)
        except Exception as exc:  # noqa: BLE001 - a missing browser build is a prerequisite
            raise e2e.PrerequisiteMissing("Chromium for Playwright is not installed") from exc
        evidence["browser_version"] = b.version
        try:
            try:
                run_journeys(b, web, short, down, env, result, evidence)
            except Exception:
                evidence["failed_step"] = b.step
                b.capture_failure()
                raise
        finally:
            allowed = {
                urllib.parse.urlsplit(url).netloc
                for url in (web.url, short.url, down.url, e2e.KEYCLOAK_URL)
            }
            allowed |= {item.replace("localhost", "127.0.0.1") for item in allowed}
            third_party = sorted(host for host in b.hosts if host and host not in allowed)
            evidence["third_party_hosts"] = third_party
            result.check("no_third_party_requests", not third_party)
            markers = ["VL-2026-0147", "Anna Beispiel", "Rosenweg", "Lindenallee"]
            result.check(
                "no_document_text_in_console",
                not any(marker in line for line in b.console for marker in markers),
            )
            evidence["console_messages"] = len(b.console)
            # Error lines only, sanitized and bounded; document markers are checked above.
            evidence["console_errors"] = [
                e2e._sanitize(line)[:400]  # noqa: SLF001
                for line in b.console
                if line.startswith("error:") and not any(marker in line for marker in markers)
            ][:20]
            b.close()
            web_logs = "".join(
                (env.work / "logs" / f"{name}.log").read_text(errors="replace")
                for name in ("web", "web-short-idle", "web-idp-down")
            )
            evidence["web_log_contains_jwt"] = bool(JWT.search(web_logs))
            result.check(
                "no_tokens_in_server_logs",
                not JWT.search(web_logs) and env.client_secret not in web_logs,
            )
    hits = e2e.scan_artifacts(run_dir, secrets_seen)
    for name in hits:
        (run_dir / name).unlink(missing_ok=True)
    evidence["sensitive_artifacts_removed"] = hits
    result.check("no_sensitive_artifacts", not hits)
    result.screenshots = [name for name in result.screenshots if name not in hits]
    result.check("no_page_errors", not result.page_errors)
    evidence["artifact_dir"] = str(run_dir.relative_to(ROOT))


def build_evidence(result: e2e.GateResult, evidence: dict[str, Any]) -> dict[str, Any]:
    status, reason = classify(result)
    return {
        "status": status,
        "reason": reason,
        "browser_engine": "chromium",
        "locales": ["de-DE", "en-US"],
        "viewports": VIEWPORTS,
        "axe_tags": AXE_TAGS,
        "checks": result.checks,
        "required_checks": list(REQUIRED),
        "tenant_leakage": result.tenant_leakage,
        "cross_tenant_leakage": result.tenant_leakage,
        "provider_invocations": result.provider_invocations,
        "paid_provider_calls": result.paid_provider_calls,
        "page_errors": result.page_errors,
        "errors": result.errors,
        "screenshots": result.screenshots,
        # Flat copies of the checks the security gate binds as control LIVE-15.
        **{name: bool(result.checks.get(name, False)) for name in LIVE_15_CHECKS},
        "locales_verified": 2
        if all(
            result.checks.get(name, False)
            for name in (
                "language_switch_to_english",
                "language_persisted_after_reload",
                "screenshots_both_languages",
            )
        )
        else 0,
        "cleanup_status": "PASS"
        if all(
            result.checks.get(name, False)
            for name in (
                "processes_stopped",
                "keycloak_gate_objects_removed",
                "redis_gate_keys_removed",
                "gate_database_removed",
                "unrelated_postgres_data_unchanged",
                "unrelated_redis_data_unchanged",
            )
        )
        else "FAIL",
    }


def _commit() -> str:
    completed = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True, check=False
    )
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
    serialized = json.dumps(evidence, indent=2, default=str) + "\n"
    if (
        e2e.SECRET_PATTERNS.search(serialized)
        or JWT.search(serialized)
        or re.search(r"client_secret|password=", serialized)
    ):
        evidence = {
            key: evidence.get(key) for key in ("gate", "tested_commit", "run_id", "timestamp")
        }
        evidence.update(
            {
                "status": "FAIL",
                "reason": "evidence secret scan failed",
                "exit_code": 1,
                "secret_scan_passed": False,
            }
        )
        serialized = json.dumps(evidence, indent=2) + "\n"
        exit_code = 1
    EVIDENCE.parent.mkdir(parents=True, exist_ok=True)
    EVIDENCE.write_text(serialized, encoding="utf-8")
    return exit_code


def main() -> int:
    evidence: dict[str, Any] = {
        "gate": "web_e2e",
        "tested_commit": _commit(),
        "run_id": uuid4().hex[:10],
    }
    result = e2e.GateResult()
    try:
        run(result, evidence)
    except e2e.PrerequisiteMissing as exc:
        return _finish(evidence, "BLOCKED", str(exc), 2)
    except Exception as exc:  # noqa: BLE001 - evidence must not carry raw service detail
        first = str(exc).splitlines()[0] if str(exc) else ""
        result.errors.append(f"gate: {type(exc).__name__}: {e2e._sanitize(first)}")  # noqa: SLF001
        result.check("gate_completed", False)
    evidence.update(build_evidence(result, evidence))
    evidence["secret_scan_passed"] = True
    status, reason = str(evidence.pop("status")), str(evidence.pop("reason"))
    return _finish(evidence, status, reason, 0 if status == "PASS" else 1)


if __name__ == "__main__":
    raise SystemExit(main())
