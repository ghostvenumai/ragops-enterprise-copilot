// @vitest-environment node
import { MemorySessionStore, type SessionRecord } from "@/server/session";
import { safeReturnTo, openTransaction, sealTransaction } from "@/server/login-transaction";
import { mutationAllowed } from "@/server/csrf";
import { PUBLIC_URL, setTestEnv } from "../support/env";

const record = (): Omit<SessionRecord, "csrfToken" | "createdAt" | "lastSeenAt"> => ({
  user: { subject: "s", name: null, email: null, tenantId: "tenant-alpha", roles: ["viewer"] },
  accessToken: "a",
  accessExpiresAt: Date.now() + 60_000,
  refreshToken: "r",
  refreshExpiresAt: null,
  idToken: null,
});

describe("server-side session store", () => {
  const limits = { idleMs: 1000, absoluteMs: 5000 };

  it("issues an unguessable identifier and a CSRF token, and stores no identifier in clear", () => {
    const store = new MemorySessionStore();
    const { id, session } = store.create(record());
    expect(id).toMatch(/^[A-Za-z0-9_-]{43}$/);
    expect(session.csrfToken).toMatch(/^[A-Za-z0-9_-]{43}$/);
    expect(store.get(id, limits)?.user.tenantId).toBe("tenant-alpha");
    expect(store.get(`${id}x`, limits)).toBeNull();
  });

  it("expires idle and absolute sessions", () => {
    let now = 0;
    const store = new MemorySessionStore(() => now);
    const { id } = store.create(record());
    now = 900;
    expect(store.get(id, limits)).not.toBeNull();
    now = 1800;
    expect(store.get(id, limits)).not.toBeNull();
    now = 3100;
    expect(store.get(id, limits)).toBeNull();
    const second = store.create(record()).id;
    for (now = 3100; now <= 8200; now += 900) store.get(second, limits);
    expect(store.get(second, limits)).toBeNull();
  });

  it("deletes sessions and shares one refresh between concurrent callers", async () => {
    const store = new MemorySessionStore();
    const { id, session } = store.create(record());
    let calls = 0;
    const run = async () => {
      calls += 1;
      await new Promise((resolve) => setTimeout(resolve, 10));
      return session;
    };
    await Promise.all([store.refreshOnce(id, run), store.refreshOnce(id, run), store.refreshOnce(id, run)]);
    expect(calls).toBe(1);
    expect(store.delete(id)).not.toBeNull();
    expect(store.get(id, limits)).toBeNull();
  });
});

describe("login transaction", () => {
  beforeAll(() => setTestEnv());

  it("round-trips sealed state and rejects tampering", async () => {
    const sealed = await sealTransaction({ state: "s", nonce: "n", verifier: "v", returnTo: "/knowledge" });
    expect(sealed).not.toContain("verifier");
    expect(await openTransaction(sealed)).toEqual({ state: "s", nonce: "n", verifier: "v", returnTo: "/knowledge" });
    const tampered = `${sealed.slice(0, -4)}AAAA`;
    expect(await openTransaction(tampered)).toBeNull();
    expect(await openTransaction(undefined)).toBeNull();
  });

  it("only allows same-application return paths (no open redirect)", () => {
    expect(safeReturnTo("/intelligence?tab=claims")).toBe("/intelligence?tab=claims");
    for (const value of ["https://evil.example", "//evil.example", "/\\evil.example", "/auth/logout", "/api/bff/v1/query", "javascript:alert(1)", "/a\nb", null]) {
      expect(safeReturnTo(value)).toBe("/");
    }
  });
});

describe("CSRF check", () => {
  beforeAll(() => setTestEnv());
  const request = (headers: Record<string, string>) => new Request(`${PUBLIC_URL}/x`, { method: "POST", headers });

  it("requires the same origin and the synchronizer token", () => {
    expect(mutationAllowed(request({ origin: PUBLIC_URL, "x-csrf-token": "t" }), "t")).toBe(true);
    expect(mutationAllowed(request({ origin: PUBLIC_URL, "x-csrf-token": "x" }), "t")).toBe(false);
    expect(mutationAllowed(request({ "x-csrf-token": "t" }), "t")).toBe(false);
    expect(mutationAllowed(request({ origin: "http://localhost:4000", "x-csrf-token": "t" }), "t")).toBe(false);
    expect(mutationAllowed(request({ origin: PUBLIC_URL, "x-csrf-token": "t", "sec-fetch-site": "same-site" }), "t")).toBe(false);
  });
});
