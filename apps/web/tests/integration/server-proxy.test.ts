// @vitest-environment node
import { createServer, type IncomingMessage, type Server } from "node:http";
import type { AddressInfo } from "node:net";
import { proxyRequest } from "@/server/proxy";
import { PUBLIC_URL, setTestEnv } from "../support/env";

/**
 * API integration tests of the BFF proxy against a real HTTP server that behaves like FastAPI.
 * They prove what reaches the upstream (and what never does) and what the browser receives.
 */
interface Seen {
  method: string;
  url: string;
  headers: IncomingMessage["headers"];
  body: string;
}

let server: Server;
let apiUrl: string;
const seen: Seen[] = [];
let respond: (seen: Seen) => { status: number; body: string; headers?: Record<string, string> } = () => ({ status: 200, body: "{}" });

beforeAll(async () => {
  setTestEnv();
  server = createServer((request, response) => {
    const chunks: Buffer[] = [];
    request.on("data", (chunk: Buffer) => chunks.push(chunk));
    request.on("end", () => {
      const entry = { method: request.method ?? "", url: request.url ?? "", headers: request.headers, body: Buffer.concat(chunks).toString() };
      seen.push(entry);
      const reply = respond(entry);
      response.writeHead(reply.status, { "content-type": "application/json", ...reply.headers });
      response.end(reply.body);
    });
  });
  await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
  apiUrl = `http://127.0.0.1:${(server.address() as AddressInfo).port}`;
});

afterAll(() => new Promise<void>((resolve) => server.close(() => resolve())));

beforeEach(() => {
  seen.length = 0;
  respond = () => ({ status: 200, body: "{}" });
});

const context = () => ({ apiUrl, accessToken: "access-token-secret", csrfToken: "csrf-123", maxUploadBytes: 1000 });
const segments = (path: string) => path.split("/").filter(Boolean);
const mutation = (path: string, body: string, headers: Record<string, string> = {}) =>
  new Request(`${PUBLIC_URL}/api/bff${path}`, {
    method: "POST",
    body,
    headers: { origin: PUBLIC_URL, "sec-fetch-site": "same-origin", "x-csrf-token": "csrf-123", "content-type": "application/json", ...headers },
  });

describe("BFF proxy", () => {
  it("adds the bearer token upstream and never returns it to the browser", async () => {
    respond = () => ({ status: 200, body: JSON.stringify([{ id: "w" }]), headers: { "set-cookie": "upstream=1", "ratelimit-remaining": "39" } });
    const response = await proxyRequest(new Request(`${PUBLIC_URL}/api/bff/v1/workspaces?limit=5`), segments("v1/workspaces"), context());
    expect(response.status).toBe(200);
    expect(seen[0]?.headers.authorization).toBe("Bearer access-token-secret");
    expect(seen[0]?.url).toBe("/v1/workspaces?limit=5");
    expect(response.headers.get("set-cookie")).toBeNull();
    expect(response.headers.get("cache-control")).toContain("no-store");
    expect(response.headers.get("ratelimit-remaining")).toBe("39");
    expect(await response.text()).not.toContain("access-token-secret");
  });

  it("does not forward browser cookies or client-supplied identity headers", async () => {
    const request = new Request(`${PUBLIC_URL}/api/bff/v1/workspaces`, {
      headers: { cookie: "__Host-ragops-session=abc", authorization: "Bearer forged", "x-tenant-id": "tenant-other" },
    });
    await proxyRequest(request, segments("v1/workspaces"), context());
    expect(seen[0]?.headers.cookie).toBeUndefined();
    expect(seen[0]?.headers["x-tenant-id"]).toBeUndefined();
    expect(seen[0]?.headers.authorization).toBe("Bearer access-token-secret");
  });

  it("answers 404 for operations outside the allowlist without contacting the API", async () => {
    for (const [method, path] of [
      ["GET", "/v1/admin/finops/usage/../../x"],
      ["DELETE", "/v1/documents/00000000-0000-0000-0000-000000000000"],
      ["POST", "/v1/admin/providers"],
      ["POST", "/v1/documents/ingest"],
      ["GET", "/docs"],
    ] as const) {
      const request = new Request(`${PUBLIC_URL}/api/bff${path}`, {
        method,
        headers: { origin: PUBLIC_URL, "x-csrf-token": "csrf-123" },
        body: method === "GET" ? undefined : "{}",
      });
      const response = await proxyRequest(request, segments(path), context());
      expect(response.status, `${method} ${path}`).toBe(404);
    }
    expect(seen).toEqual([]);
  });

  it("rejects state-changing requests without the CSRF token or from another origin", async () => {
    const body = JSON.stringify({ question: "q", top_k: 5 });
    expect((await proxyRequest(mutation("/v1/query", body, { "x-csrf-token": "wrong" }), segments("v1/query"), context())).status).toBe(403);
    expect((await proxyRequest(mutation("/v1/query", body, { origin: "https://evil.example" }), segments("v1/query"), context())).status).toBe(403);
    expect((await proxyRequest(mutation("/v1/query", body, { "sec-fetch-site": "cross-site" }), segments("v1/query"), context())).status).toBe(403);
    expect(seen).toEqual([]);
  });

  it("forwards a valid idempotency key only for /v1/query and returns the replay marker", async () => {
    respond = () => ({ status: 200, body: "{}", headers: { "idempotency-replayed": "true" } });
    const ok = await proxyRequest(mutation("/v1/query", "{}", { "idempotency-key": "ui-abc" }), segments("v1/query"), context());
    expect(ok.headers.get("idempotency-replayed")).toBe("true");
    expect(seen[0]?.headers["idempotency-key"]).toBe("ui-abc");
    const bad = await proxyRequest(mutation("/v1/query", "{}", { "idempotency-key": "bad keyä" }), segments("v1/query"), context());
    expect(bad.status).toBe(400);
    const elsewhere = await proxyRequest(mutation("/v1/intelligence/reviews", "{}", { "idempotency-key": "ui-abc" }), segments("v1/intelligence/reviews"), context());
    expect(elsewhere.status).toBe(400);
    expect(seen).toHaveLength(1);
  });

  it("rejects unknown query parameters (no tenant or role smuggling)", async () => {
    const response = await proxyRequest(new Request(`${PUBLIC_URL}/api/bff/v1/workspaces?tenant_id=tenant-beta`), segments("v1/workspaces"), context());
    expect(response.status).toBe(400);
    expect(seen).toEqual([]);
  });

  it("sanitizes upstream errors: validation details that echo input are dropped", async () => {
    respond = () => ({ status: 422, body: JSON.stringify({ detail: [{ loc: ["body", "question"], input: "SECRET DOCUMENT TEXT" }] }) });
    const response = await proxyRequest(mutation("/v1/query", "{}"), segments("v1/query"), context());
    expect(response.status).toBe(422);
    const text = await response.text();
    expect(text).not.toContain("SECRET DOCUMENT TEXT");
    expect(JSON.parse(text)).toEqual({ error: "validation_error" });
  });

  it("keeps short generic upstream details and Retry-After", async () => {
    respond = () => ({ status: 409, body: JSON.stringify({ detail: "conflict" }), headers: { "retry-after": "1" } });
    const response = await proxyRequest(mutation("/v1/intelligence/reviews", "{}"), segments("v1/intelligence/reviews"), context());
    expect(response.status).toBe(409);
    expect(response.headers.get("retry-after")).toBe("1");
    expect(await response.json()).toEqual({ error: "conflict", detail: "conflict" });
  });

  it("passes the readiness report through on 503", async () => {
    respond = () => ({ status: 503, body: JSON.stringify({ status: "unready", components: { redis: { critical: true, status: "down", reason: "timeout" } } }) });
    const response = await proxyRequest(new Request(`${PUBLIC_URL}/api/bff/ready`), ["ready"], context());
    expect(response.status).toBe(503);
    expect((await response.json()).status).toBe("unready");
  });

  it("rejects oversized and malformed bodies before the API sees them", async () => {
    const big = await proxyRequest(mutation("/v1/query", "x".repeat(70_000)), segments("v1/query"), context());
    expect(big.status).toBe(413);
    const malformed = await proxyRequest(mutation("/v1/query", "{not json"), segments("v1/query"), context());
    expect(malformed.status).toBe(400);
    const upload = new Request(`${PUBLIC_URL}/api/bff/v1/documents/upload`, {
      method: "POST",
      body: "x".repeat(70_000),
      headers: { origin: PUBLIC_URL, "x-csrf-token": "csrf-123", "content-type": "multipart/form-data; boundary=x" },
    });
    expect((await proxyRequest(upload, segments("v1/documents/upload"), context())).status).toBe(413);
    expect(seen).toEqual([]);
  });

  it("reports an unreachable API without internal details", async () => {
    const response = await proxyRequest(new Request(`${PUBLIC_URL}/api/bff/v1/workspaces`), segments("v1/workspaces"), { ...context(), apiUrl: "http://127.0.0.1:1" });
    expect(response.status).toBe(502);
    expect(await response.json()).toEqual({ error: "api_unreachable" });
  });
});
