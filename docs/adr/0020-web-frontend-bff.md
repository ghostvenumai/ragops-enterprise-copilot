# ADR 0020: Enterprise web frontend with a Backend-for-Frontend (ENT-12-UI)

## Status

Accepted (ENT-12-UI). The Streamlit dashboard stays the supported UI until a cut-over is
decided on the basis of the parity evidence below.

## Context

The Streamlit dashboard (`apps/dashboard`) proved the product journeys, but it renders on the
server per session, has limited control over layout, accessibility and routing, and keeps the
signed-in user's access token in its own process state. The product needs a professional,
responsive, accessible and bilingual (de-DE, en-US) enterprise interface for all modules —
cockpit, AI copilot, knowledge base, document intelligence, monitoring, FinOps and governance —
without moving any authority out of FastAPI and without exposing tokens to the browser.

Constraints of this host and this milestone: Node.js 18.19 (no Node 20), little free memory,
no paid model calls, synthetic data only, no change to the live Keycloak realm, the reverse
proxy or the production deployment.

## Decision

**Separate app, same repository.** `apps/web` is a Next.js 15 (App Router) application in
TypeScript strict mode with Tailwind CSS 3.4, shadcn/ui-style components on Radix primitives,
Lucide icons, Recharts, TanStack Query and Zod. Versions are pinned exactly and locked by
`package-lock.json`. Tailwind 4 and Next 16 were not chosen because they require Node 20.

**Backend-for-Frontend boundary.** The browser talks only to the Next.js server:

- Sign-in is OpenID Connect Authorization Code with PKCE (S256) against the configured issuer.
  The confidential client secret, the code verifier and all tokens stay in the Node process.
  State, nonce, verifier and return path travel in a short-lived (10 min) encrypted
  `__Host-ragops-login` cookie (JWE `dir`/`A256GCM`, key derived from
  `RAGOPS_WEB_SESSION_SECRET`).
- The callback verifies the ID token (signature, issuer, audience, nonce) and the access token
  with the same rules as the API (signature via JWKS, issuer, audience, `typ`, `azp` = this
  client, tenant claim, roles claim). The session is created only after both checks.
- Sessions are server-side (`MemorySessionStore`): the browser holds an opaque 256-bit
  identifier in `__Host-ragops-session` (HttpOnly, Secure, SameSite=Lax, path `/`); the store
  keys entries by its SHA-256, enforces an idle and an absolute lifetime and refreshes the
  access token shortly before expiry (one refresh per session at a time). A refresh that
  changes subject or tenant ends the session. A new identifier is issued at every sign-in.
- `/api/bff/*` forwards only allowlisted method/path/query combinations
  (`src/server/api-routes.ts`) to FastAPI with the session's bearer token. Unknown operations,
  unknown query parameters (for example `tenant_id`) and oversized bodies are answered by the
  BFF without contacting the API; browser cookies and `Authorization` headers are never
  forwarded. Shared-catalog mutations (platform administration) are not in the allowlist.
- Every state-changing request needs the exact public `Origin` (and, when sent,
  `Sec-Fetch-Site: same-origin`) plus the session's synchronizer CSRF token in
  `x-csrf-token`. Logout is a CSRF-protected POST that ends the local session first and then
  the provider session (`end_session_endpoint` with `id_token_hint`).
- Responses carry `Cache-Control: no-store, private`; only `Retry-After`, `RateLimit-*` and
  `Idempotency-Replayed` pass through. Upstream errors are reduced to a stable code and, at
  most, a short generic FastAPI `detail`; validation arrays (which can echo input) are dropped.
- FastAPI remains the authority for tenant, role, access level and every resource check. UI
  role checks only hide controls.

**Rendering safety.** Model answers, document values and source excerpts are rendered as text
nodes only. ESLint forbids `dangerouslySetInnerHTML`. Pages are dynamic and served with a
per-request nonce Content-Security-Policy (`script-src 'self' 'nonce-…' 'strict-dynamic'`,
`frame-ancestors 'none'`, `object-src 'none'`, `base-uri 'none'`), `X-Frame-Options: DENY`,
`nosniff`, `Referrer-Policy: no-referrer` and `no-store`. No third-party origin is contacted
(no font, analytics or telemetry CDN).

**Contracts.** Every response the UI reads is parsed with a Zod schema written against the
FastAPI routes; a response that does not match is not rendered ("unexpected response").
Amounts stay exact decimal strings and are formatted digit by digit.

**Internationalisation.** `next-intl` without locale routing: the locale comes from the
non-sensitive `ragops-locale` cookie (de-DE default, en-US), so switching never changes URLs,
sessions, tenants or cached data. Catalogs live in `apps/web/messages/*.json`; the German
catalog types all keys at compile time, a test enforces key and placeholder parity, and a
TypeScript-AST test rejects literal UI text in components. Source evidence, document values,
audit records and model answers are never translated; the copilot says so.

**Document intelligence semantics.** Reviews send `expected_version` and show the 409
conflict instead of retrying; corrections are typed (decimal with two places, ISO date, text)
and stored by the API beside the original fact. Amounts are labelled as statements from
documents; the "potentially current" amount is shown as "latest stated (not confirmed)" and a
consolidated total only when the API marks the summary complete. The original file is not
served by the API, so no page image or highlight is shown; the provenance view says so
instead of approximating it.

## Consequences

- The session store is per process. A horizontally scaled deployment needs a shared store
  (for example Redis with encryption at rest) behind the same interface; until then the web
  app runs as a single instance and a restart signs users out.
- FinOps per-tenant usage, forecast and alert endpoints of the API currently return static
  placeholders; the UI does not present them as metrics. Budgets are kept in API memory.
- The release adds the mandatory `web_e2e` gate and the security control LIVE-15; the
  Streamlit `browser_e2e` gate stays mandatory until the Streamlit UI is retired.
- No backend change was required for the authenticated contract.
