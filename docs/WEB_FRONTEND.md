# Web frontend (ENT-12-UI)

`apps/web` is the Next.js enterprise interface for RAGOps. It runs beside the Streamlit
dashboard (`apps/dashboard`), which stays the supported UI until a cut-over is decided.
Architecture and security decisions: [ADR 0020](adr/0020-web-frontend-bff.md).

## Modules

| Area | Route | API (through the BFF) | Roles |
| --- | --- | --- | --- |
| Enterprise Cockpit | `/` | `/health`, `/ready`, `/v1/ingestion/jobs`, `/v1/intelligence/findings`, `/v1/intelligence/documents?review_required=true`, `/v1/costs/summary` and `/v1/audit-events` (admin) | all; cost and audit cards for admins |
| AI Copilot | `/copilot` | `POST /v1/query` with one `Idempotency-Key` per question (reused when the same request is resent) | all |
| Knowledge Base | `/knowledge` | `/v1/workspaces`, `/v1/collections`, `POST /v1/documents/upload`, `/v1/ingestion/jobs`, retry/cancel, `/v1/documents/{id}/versions`, `/v1/documents/{id}/versions/{v}/pages` | all; retry/cancel for admins |
| Document Intelligence | `/intelligence`, `/intelligence/documents/{id}` | all `/v1/intelligence/*` routes: documents, facts, creditors, claims, deadlines, findings, persons, aliases, reviews, reprocess | read: all; reviews, persons, reprocess: admin, compliance |
| Monitoring | `/monitoring` | `/metrics`, `/ready`, `/v1/evaluations` | admin |
| FinOps | `/finops` | `/v1/costs/summary`, `/v1/admin/budgets`, `/v1/admin/finops/policy/simulate`, `/v1/admin/model-router/simulate` | admin |
| Governance & Operations | `/governance` | `/v1/audit-events`, `/v1/admin/providers`, `/v1/admin/models`, `/v1/admin/model-policies`, `/ready` | admin |

The UI hides controls by role; FastAPI decides every request.

### Known limitations (honest gaps, not hidden)

- The API does not serve original files, so document intelligence shows page, line, source
  excerpt and extraction method but no page image or highlight.
- `/v1/admin/finops/summary|forecast|usage|alerts` return static placeholders; the UI does not
  show them as metrics. `/v1/costs/summary` and `/metrics` are process-wide counters since the
  API start and are labelled as such. Budgets are held in API memory.
- Sessions live in the web server process (single instance; a restart signs users out).
- Shared model-catalog changes (platform administration) are intentionally not offered.
- The copilot answers only what the API's model router allows. The local deployment's catalog
  has a single SIMPLE-tier deterministic model, so questions that retrieve four or more chunks
  or two or more documents are refused with "no eligible model" (shown as a controlled
  refusal). Production catalogs with STANDARD/COMPLEX models lift this; the UI defaults to
  three sources.

## Configuration (server-side environment only)

| Variable | Required | Default | Meaning |
| --- | --- | --- | --- |
| `RAGOPS_WEB_API_URL` | yes | – | Internal FastAPI base URL (never sent to the browser) |
| `RAGOPS_WEB_PUBLIC_URL` | yes | – | Public origin of the web app; HTTPS outside localhost; used for redirect URIs and the CSRF origin check |
| `RAGOPS_WEB_OIDC_ISSUER` | yes | – | Issuer URL (discovery at `/.well-known/openid-configuration`) |
| `RAGOPS_WEB_OIDC_CLIENT_ID` | yes | – | Confidential client; must be in the API's `RAGOPS_OIDC_ALLOWED_CLIENTS` |
| `RAGOPS_WEB_OIDC_CLIENT_SECRET` | yes | – | Client secret (secret store / mounted file, never committed) |
| `RAGOPS_WEB_OIDC_AUDIENCE` | no | `ragops-api` | Audience the access token must carry (same as the API) |
| `RAGOPS_WEB_OIDC_ROLES_CLAIM` | no | `resource_access.ragops-api.roles` | Dotted roles claim (same as the API) |
| `RAGOPS_WEB_OIDC_TENANT_CLAIM` | no | `tenant_id` | Tenant claim (same as the API) |
| `RAGOPS_WEB_OIDC_SCOPE` | no | `openid profile email` | Requested scopes |
| `RAGOPS_WEB_SESSION_SECRET` | yes | – | ≥ 32 random characters; encrypts the sign-in transaction cookie |
| `RAGOPS_WEB_SESSION_IDLE_SECONDS` | no | `1800` | Idle session lifetime |
| `RAGOPS_WEB_SESSION_MAX_SECONDS` | no | `28800` | Absolute session lifetime |
| `RAGOPS_WEB_MAX_UPLOAD_BYTES` | no | `12000000` | Upload body limit enforced by the BFF (the API enforces its own limits) |

The API behind the BFF must run the vector query path with durable query idempotency
(`RAGOPS_IDEMPOTENCY_HMAC_KEY` or `RAGOPS_IDEMPOTENCY_HMAC_KEY_FILE`): the copilot sends one
`Idempotency-Key` per question, and an API without the key fails closed with 503, which the UI
shows as "service temporarily unavailable" instead of retrying without the key.

The identity provider client needs the redirect URI `<public URL>/auth/callback`, the
post-logout redirect URI `<public URL>/signin?signedOut=1`, the standard flow, PKCE method
`S256`, an audience mapper for `ragops-api` and the tenant claim mapper. Creating such a client
in the live realm requires operator approval; the `web_e2e` gate only uses a temporary client.

## Development

Requires Node.js ≥ 18.18 and npm.

```bash
cd apps/web
npm ci                      # exact versions from package-lock.json
npm run typecheck           # tsc --noEmit (strict)
npm run lint                # eslint, zero warnings
npm test                    # vitest: unit, component and BFF integration tests
npm run build               # production build
RAGOPS_WEB_API_URL=http://127.0.0.1:8000 RAGOPS_WEB_PUBLIC_URL=http://localhost:3000 \
RAGOPS_WEB_OIDC_ISSUER=http://localhost:8081/realms/<realm> RAGOPS_WEB_OIDC_CLIENT_ID=<client> \
RAGOPS_WEB_OIDC_CLIENT_SECRET=<from secret store> RAGOPS_WEB_SESSION_SECRET=<random> \
npm start                   # next start on 127.0.0.1:3000
```

Open `http://localhost:3000` (Chromium accepts `Secure` cookies on `localhost`). Use synthetic
documents only.

## Internationalisation

- Locales: `de-DE` (default) and `en-US`; switcher in the header and on the sign-in page.
- Preference cookie `ragops-locale` (not HttpOnly, SameSite=Lax, one year, no personal data).
  Switching refreshes the server components in place; URL, session, tenant, permissions and
  loaded data stay unchanged.
- Catalogs: `apps/web/messages/de-DE.json` and `en-US.json`. Keys are typed from the German
  catalog (`global.d.ts`), so an unknown key is a compile error. `tests/unit/i18n-parity.test.ts`
  enforces identical keys and placeholders, non-empty messages and no German text in the English
  catalog, and parses every component to reject literal UI text.
- Dates, numbers and currencies use `Intl` with the active locale; document amounts are exact
  decimal strings formatted without floating point.
- Document text, extracted evidence, citation identifiers, audit records and model answers are
  shown as provided and never translated.

## Tests and evidence

| Layer | Where | What |
| --- | --- | --- |
| Type safety | `npm run typecheck` | strict TypeScript, typed translation keys |
| Lint | `npm run lint` | Next.js rules, no `any`, no `dangerouslySetInnerHTML` |
| Unit / component | `apps/web/tests/{unit,components}` | formatting, schemas, i18n parity, hardcoded-text guard, session store, login transaction, CSRF, copilot idempotency and refusals, review dialog incl. 409, jobs notice, language switcher |
| API integration | `apps/web/tests/integration` | BFF proxy against a real HTTP upstream; OIDC token verification against a local JWKS |
| Browser E2E | `scripts/web_e2e_gate.py` (release gate `web_e2e`) | real Chromium, Keycloak, API, worker, PostgreSQL, Redis, Qdrant; both languages; axe WCAG 2.2 AA rule set; responsive screenshots |
| Python self-tests | `tests/product/test_web_e2e_gate.py` | gate registry, verdict and LIVE-15 evidence contract |
| Dependencies | `npm audit --omit=dev` | production dependency advisories |

Dependency audit status (ENT-12-UI): `npm audit --omit=dev` reports no vulnerabilities. The
full audit still lists advisories in development-only tooling (Vitest/tinypool, the Tailwind 3
toolchain via postcss-selector-parser/chokidar, and eslint-config-next via fast-glob/braces).
Their fixes require Node.js 20 (Vitest 4, Tailwind 4, Next 16) or have no fixed release yet;
these packages run only on the developer and CI machine, never in the served application.
They are accepted for this milestone and must be revisited with the Node.js 20 upgrade.

Committed evidence of the ENT-12-UI run is in `evidence/product-v1/web-ui/`: the 55
screenshots (both languages; desktop, tablet and mobile) and `web-e2e-summary.json` of the
final pre-commit run. A commit cannot contain its own SHA, so the summary carries
`source_tree_sha256`: SHA-256 over `(path, SHA-256(content))` of every committed file in
`apps/web` (without `node_modules`, `.next`, `next-env.d.ts`, `*.tsbuildinfo`) and of
`scripts/web_e2e_gate.py`, `scripts/browser_e2e_gate.py`, `scripts/rc_live_gate.py`,
`scripts/security_gate.py` and `scripts/rag_query_gate.py`, sorted by path. The release
evidence itself is the `web_e2e` gate run on the commit (`evidence/product-v1/rc-live/`,
host-local like every live gate).

The committed summary (`cc329dc0…`, 114 files) matches the tree of the ENT-12-UI commit
before the closure fixes. The later fail-closed aggregation change in `scripts/rc_live_gate.py`
(release bookkeeping only; no browser journey, page or assertion changed) is part of the digest,
so the summary no longer matches the final commit. It is kept as the record of that run, not
regenerated without a run; the operator closure re-runs `web_e2e` on the final commit.
