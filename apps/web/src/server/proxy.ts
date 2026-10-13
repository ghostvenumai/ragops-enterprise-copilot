import "server-only";
import { IDEMPOTENCY_KEY, matchRoute } from "./api-routes";
import { mutationAllowed } from "./csrf";

const PASS_RESPONSE_HEADERS = [
  "retry-after",
  "ratelimit-limit",
  "ratelimit-remaining",
  "ratelimit-reset",
  "idempotency-replayed",
];
const TIMEOUT_MS = 30_000;
const UPLOAD_TIMEOUT_MS = 90_000;
const MAX_JSON_BYTES = 64_000;

export interface ProxyContext {
  apiUrl: string;
  accessToken: string;
  csrfToken: string;
  maxUploadBytes: number;
}

/** Errors the browser sees: a stable code and, at most, a short generic upstream detail. */
export function errorResponse(status: number, code: string, detail?: string, headers?: HeadersInit): Response {
  return Response.json(
    { error: code, ...(detail ? { detail } : {}) },
    { status, headers: { "cache-control": "no-store", ...Object.fromEntries(new Headers(headers)) } },
  );
}

const SAFE_DETAIL = /^[\w .,:;'()\/-]{1,160}$/;

/** FastAPI details are short generic strings; validation arrays may echo input and are dropped. */
export function sanitizeDetail(payload: unknown): string | undefined {
  if (!payload || typeof payload !== "object" || !("detail" in payload)) return undefined;
  const detail = (payload as { detail: unknown }).detail;
  if (typeof detail === "string" && SAFE_DETAIL.test(detail)) return detail;
  return undefined;
}

function errorCode(status: number): string {
  if (status === 400) return "bad_request";
  if (status === 401) return "unauthenticated";
  if (status === 403) return "forbidden";
  if (status === 404) return "not_found";
  if (status === 409) return "conflict";
  if (status === 413) return "too_large";
  if (status === 422) return "validation_error";
  if (status === 429) return "rate_limited";
  if (status === 501) return "not_implemented";
  if (status === 503) return "unavailable";
  return "upstream_error";
}

export async function proxyRequest(request: Request, segments: string[], context: ProxyContext): Promise<Response> {
  let path: string;
  try {
    path = `/${segments.map((part) => encodeURIComponent(decodeURIComponent(part))).join("/")}`;
  } catch {
    return errorResponse(404, "not_found");
  }
  const rule = matchRoute(request.method, path);
  if (!rule) return errorResponse(404, "not_found");
  if (rule.method !== "GET" && !mutationAllowed(request, context.csrfToken)) {
    return errorResponse(403, "csrf_rejected");
  }

  const incoming = new URL(request.url);
  const query = new URLSearchParams();
  for (const [key, value] of incoming.searchParams) {
    if (!rule.query.includes(key) || value.length > 128) return errorResponse(400, "bad_request");
    query.append(key, value);
  }

  const headers = new Headers({ authorization: `Bearer ${context.accessToken}`, accept: rule.text ? "text/plain" : "application/json" });
  const key = request.headers.get("idempotency-key");
  if (key !== null) {
    if (!rule.idempotent || !IDEMPOTENCY_KEY.test(key)) return errorResponse(400, "bad_request");
    headers.set("idempotency-key", key);
  }

  let body: BodyInit | undefined;
  const declared = Number(request.headers.get("content-length") ?? "0");
  if (rule.body === "json") {
    if (declared > MAX_JSON_BYTES) return errorResponse(413, "too_large");
    const text = await request.text();
    if (text.length > MAX_JSON_BYTES) return errorResponse(413, "too_large");
    try {
      JSON.parse(text);
    } catch {
      return errorResponse(400, "bad_request");
    }
    headers.set("content-type", "application/json");
    body = text;
  } else if (rule.body === "multipart") {
    const type = request.headers.get("content-type") ?? "";
    if (!type.startsWith("multipart/form-data")) return errorResponse(400, "bad_request");
    const limit = context.maxUploadBytes + 64_000;
    if (declared > limit) return errorResponse(413, "too_large");
    const buffer = await request.arrayBuffer();
    if (buffer.byteLength > limit) return errorResponse(413, "too_large");
    headers.set("content-type", type);
    body = buffer;
  }

  let upstream: Response;
  try {
    upstream = await fetch(`${context.apiUrl.replace(/\/$/, "")}${path}${query.size ? `?${query}` : ""}`, {
      method: rule.method,
      headers,
      body,
      cache: "no-store",
      redirect: "manual",
      signal: AbortSignal.timeout(rule.body === "multipart" ? UPLOAD_TIMEOUT_MS : TIMEOUT_MS),
    });
  } catch {
    return errorResponse(502, "api_unreachable");
  }

  const passed = new Headers({ "cache-control": "no-store, private", vary: "cookie" });
  for (const name of PASS_RESPONSE_HEADERS) {
    const value = upstream.headers.get(name);
    if (value !== null) passed.set(name, value);
  }

  if (rule.text) {
    if (!upstream.ok) return errorResponse(upstream.status, errorCode(upstream.status), undefined, passed);
    passed.set("content-type", "text/plain; charset=utf-8");
    return new Response(await upstream.text(), { status: upstream.status, headers: passed });
  }

  const payload: unknown = await upstream.json().catch(() => null);
  // /ready answers 503 with a full component report; that body is the useful information.
  const readiness = path === "/ready" && upstream.status === 503 && payload !== null;
  if (!upstream.ok && !readiness) {
    return errorResponse(upstream.status, errorCode(upstream.status), sanitizeDetail(payload), passed);
  }
  return Response.json(payload, { status: upstream.status, headers: passed });
}
