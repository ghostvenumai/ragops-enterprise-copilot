# RAGOps web frontend

Next.js (App Router) enterprise interface for RAGOps with a Backend-for-Frontend boundary:
the browser never receives provider, service or identity tokens.

- Architecture and security: [`docs/adr/0020-web-frontend-bff.md`](../../docs/adr/0020-web-frontend-bff.md)
- Configuration, development, i18n and tests: [`docs/WEB_FRONTEND.md`](../../docs/WEB_FRONTEND.md)

```bash
npm ci && npm run typecheck && npm run lint && npm test && npm run build
```

Layout:

| Path | Content |
| --- | --- |
| `src/app` | Routes: `(app)/*` pages behind the server-side session check, `signin`, `auth/{login,callback,logout}`, `api/{session,bff}` |
| `src/server` | Server-only modules: configuration, OIDC (PKCE, token verification), session store, CSRF, BFF allowlist and proxy |
| `src/components` | Design system (`ui`), shell and navigation (`layout`), feature modules |
| `src/lib` | API client, Zod contracts, exact formatters, role hints |
| `messages` | `de-DE` (default) and `en-US` catalogs |
| `tests` | Vitest unit, component and integration tests |
