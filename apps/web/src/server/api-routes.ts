/**
 * Allowlist of FastAPI operations the browser may reach through the BFF. Anything not
 * listed is answered with 404 by the BFF and never forwarded. FastAPI remains the
 * authority for tenant, role and resource checks; this list only narrows the surface.
 * Shared catalog mutations (platform administration) are intentionally absent.
 */
export type Method = "GET" | "POST" | "PATCH";
export type BodyKind = "none" | "json" | "multipart";

export interface RouteRule {
  method: Method;
  pattern: RegExp;
  body: BodyKind;
  query: readonly string[];
  /** Upstream answers text/plain instead of JSON. */
  text?: boolean;
  /** Request may carry an Idempotency-Key header. */
  idempotent?: boolean;
}

const ID = "[0-9a-fA-F-]{36}";
const rule = (method: Method, path: string, body: BodyKind = "none", query: readonly string[] = [], extra: Partial<RouteRule> = {}): RouteRule => ({
  method,
  pattern: new RegExp(`^${path}$`),
  body,
  query,
  ...extra,
});

export const ROUTES: readonly RouteRule[] = [
  rule("GET", "/health"),
  rule("GET", "/ready"),
  rule("GET", "/metrics", "none", [], { text: true }),
  rule("POST", "/v1/query", "json", [], { idempotent: true }),
  rule("GET", "/v1/workspaces", "none", ["limit", "offset"]),
  rule("GET", "/v1/collections", "none", ["workspace_id", "limit", "offset"]),
  rule("POST", "/v1/documents/upload", "multipart"),
  rule("GET", `/v1/documents/${ID}/versions`),
  rule("GET", `/v1/documents/${ID}/versions/${ID}/pages`),
  rule("GET", `/v1/documents/${ID}/versions/${ID}/chunks/\\d{1,7}/provenance`),
  rule("POST", `/v1/documents/${ID}/reindex`),
  rule("GET", "/v1/ingestion/jobs"),
  rule("GET", `/v1/ingestion/jobs/${ID}`),
  rule("POST", `/v1/ingestion/jobs/${ID}/cancel`),
  rule("POST", `/v1/ingestion/jobs/${ID}/retry`),
  rule("GET", "/v1/intelligence/documents", "none", ["classification", "person_id", "review_required"]),
  rule("GET", `/v1/intelligence/documents/${ID}`),
  rule("GET", "/v1/intelligence/creditors"),
  rule("GET", "/v1/intelligence/claims", "none", ["person_id"]),
  rule("GET", "/v1/intelligence/deadlines", "none", ["person_id"]),
  rule("GET", "/v1/intelligence/findings"),
  rule("GET", `/v1/intelligence/facts/${ID}`),
  rule("GET", "/v1/intelligence/persons"),
  rule("POST", "/v1/intelligence/persons", "json"),
  rule("POST", `/v1/intelligence/persons/${ID}/aliases`, "json"),
  rule("POST", "/v1/intelligence/reviews", "json"),
  rule("POST", `/v1/intelligence/documents/${ID}/reprocess`),
  rule("GET", "/v1/evaluations"),
  rule("GET", "/v1/audit-events"),
  rule("GET", "/v1/costs/summary"),
  rule("GET", "/v1/admin/providers"),
  rule("GET", "/v1/admin/models"),
  rule("GET", "/v1/admin/model-policies"),
  rule("GET", "/v1/admin/budgets"),
  rule("POST", "/v1/admin/budgets", "json"),
  rule("GET", "/v1/admin/quotas"),
  rule("POST", "/v1/admin/finops/policy/simulate", "json"),
  rule("POST", "/v1/admin/model-router/simulate", "json"),
];

export function matchRoute(method: string, path: string): RouteRule | null {
  if (path.length > 512 || path.includes("..") || path.includes("//")) return null;
  return ROUTES.find((item) => item.method === method && item.pattern.test(path)) ?? null;
}

export const IDEMPOTENCY_KEY = /^[A-Za-z0-9._~:+/=-]{1,128}$/;
