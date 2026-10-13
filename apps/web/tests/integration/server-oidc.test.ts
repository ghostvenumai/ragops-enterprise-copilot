// @vitest-environment node
import { createServer, type Server } from "node:http";
import type { AddressInfo } from "node:net";
import { SignJWT, exportJWK, generateKeyPair, type JWK } from "jose";
import { resetWebEnvForTests } from "@/server/env";
import { authorizationUrl, verifyAccessToken, verifyIdToken } from "@/server/oidc";
import { setTestEnv } from "../support/env";

/** Token verification against a local OIDC discovery + JWKS server (no real IdP needed). */
let server: Server;
let issuer: string;
let privateKey: CryptoKey;
let otherKey: CryptoKey;
const kid = "test-key";

beforeAll(async () => {
  const pair = await generateKeyPair("RS256", { extractable: true });
  privateKey = pair.privateKey as CryptoKey;
  otherKey = (await generateKeyPair("RS256")).privateKey as CryptoKey;
  const jwk: JWK = { ...(await exportJWK(pair.publicKey)), kid, alg: "RS256", use: "sig" };
  server = createServer((request, response) => {
    response.setHeader("content-type", "application/json");
    if (request.url?.endsWith("/.well-known/openid-configuration")) {
      response.end(
        JSON.stringify({
          issuer,
          authorization_endpoint: `${issuer}/protocol/openid-connect/auth`,
          token_endpoint: `${issuer}/protocol/openid-connect/token`,
          jwks_uri: `${issuer}/protocol/openid-connect/certs`,
          end_session_endpoint: `${issuer}/protocol/openid-connect/logout`,
        }),
      );
    } else if (request.url?.endsWith("/certs")) {
      response.end(JSON.stringify({ keys: [jwk] }));
    } else {
      response.statusCode = 404;
      response.end("{}");
    }
  });
  await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
  issuer = `http://127.0.0.1:${(server.address() as AddressInfo).port}/realms/test`;
  setTestEnv({ RAGOPS_WEB_OIDC_ISSUER: issuer });
  resetWebEnvForTests();
});

afterAll(() => new Promise<void>((resolve) => server.close(() => resolve())));

async function token(claims: Record<string, unknown>, options: { key?: CryptoKey; typ?: string; exp?: string } = {}) {
  return new SignJWT({
    typ: "Bearer",
    azp: "ragops-web-test",
    tenant_id: "tenant-alpha",
    resource_access: { "ragops-api": { roles: ["admin"] } },
    name: "RC Admin",
    ...claims,
  })
    .setProtectedHeader({ alg: "RS256", kid, typ: options.typ ?? "JWT" })
    .setIssuer(issuer)
    .setAudience("ragops-api")
    .setSubject("user-1")
    .setIssuedAt()
    .setExpirationTime(options.exp ?? "5m")
    .sign(options.key ?? privateKey);
}

describe("access token verification", () => {
  it("accepts a valid token and derives tenant and roles only from verified claims", async () => {
    const { user } = await verifyAccessToken(await token({}));
    expect(user).toEqual({ subject: "user-1", name: "RC Admin", email: null, tenantId: "tenant-alpha", roles: ["admin"] });
  });

  it.each([
    ["wrong signature", { key: "other" }],
    ["expired", { exp: "-1m" }],
    ["another client", { claims: { azp: "rc-other-client" } }],
    ["ID token type", { claims: { typ: "ID" } }],
    ["refresh token header", { typ: "Refresh" }],
    ["missing tenant", { claims: { tenant_id: undefined } }],
  ])("rejects a token with %s", async (_label, variant) => {
    const v = variant as { key?: string; exp?: string; typ?: string; claims?: Record<string, unknown> };
    const jwt = await token(v.claims ?? {}, { key: v.key === "other" ? otherKey : undefined, exp: v.exp, typ: v.typ });
    await expect(verifyAccessToken(jwt)).rejects.toThrow();
  });

  it("rejects a token for another audience", async () => {
    const jwt = await new SignJWT({ typ: "Bearer", azp: "ragops-web-test", tenant_id: "tenant-alpha" })
      .setProtectedHeader({ alg: "RS256", kid })
      .setIssuer(issuer)
      .setAudience("another-api")
      .setSubject("user-1")
      .setExpirationTime("5m")
      .sign(privateKey);
    await expect(verifyAccessToken(jwt)).rejects.toThrow();
  });

  it("checks the ID token nonce", async () => {
    const id = await new SignJWT({ nonce: "n-1" })
      .setProtectedHeader({ alg: "RS256", kid })
      .setIssuer(issuer)
      .setAudience("ragops-web-test")
      .setSubject("user-1")
      .setExpirationTime("5m")
      .sign(privateKey);
    await expect(verifyIdToken(id, "n-1")).resolves.toBeUndefined();
    await expect(verifyIdToken(id, "n-2")).rejects.toThrow();
  });

  it("builds an authorization request with PKCE S256 and no secret", () => {
    const url = new URL(authorizationUrl(`${issuer}/auth`, { state: "s", nonce: "n", challenge: "c", locale: "en-US" }));
    expect(url.searchParams.get("code_challenge_method")).toBe("S256");
    expect(url.searchParams.get("response_type")).toBe("code");
    expect(url.searchParams.get("ui_locales")).toBe("en");
    expect(url.toString()).not.toContain("test-client-secret");
  });
});
