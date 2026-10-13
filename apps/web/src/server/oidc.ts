import "server-only";
import { createRemoteJWKSet, decodeProtectedHeader, jwtVerify, type JWTPayload } from "jose";
import { z } from "zod";
import { webEnv, type WebEnv } from "./env";
import type { SessionUser } from "./session";

const ALGORITHMS = ["RS256", "RS384", "RS512", "PS256", "ES256"];
const FETCH_TIMEOUT_MS = 5000;

export class IdentityProviderUnavailable extends Error {}
export class TokenRejected extends Error {}

const discoverySchema = z.object({
  issuer: z.string(),
  authorization_endpoint: z.url(),
  token_endpoint: z.url(),
  jwks_uri: z.url(),
  end_session_endpoint: z.url().optional(),
});
export type Discovery = z.infer<typeof discoverySchema>;

const tokenResponseSchema = z.object({
  access_token: z.string().min(1),
  expires_in: z.number().int().positive(),
  refresh_token: z.string().optional(),
  refresh_expires_in: z.number().int().nonnegative().optional(),
  id_token: z.string().optional(),
  token_type: z.string(),
});
export type TokenResponse = z.infer<typeof tokenResponseSchema>;

interface Cached {
  discovery: Discovery;
  jwks: ReturnType<typeof createRemoteJWKSet>;
  fetchedAt: number;
}
const CACHE_KEY = Symbol.for("ragops.web.oidcDiscovery");
const DISCOVERY_TTL_MS = 5 * 60 * 1000;

async function fetchWithTimeout(url: string, init?: RequestInit): Promise<Response> {
  try {
    return await fetch(url, { ...init, cache: "no-store", signal: AbortSignal.timeout(FETCH_TIMEOUT_MS) });
  } catch {
    throw new IdentityProviderUnavailable("identity provider unreachable");
  }
}

export async function discovery(env: WebEnv = webEnv()): Promise<Cached> {
  const holder = globalThis as unknown as Record<symbol, Cached | undefined>;
  const cached = holder[CACHE_KEY];
  if (cached && Date.now() - cached.fetchedAt < DISCOVERY_TTL_MS) return cached;
  const issuer = env.RAGOPS_WEB_OIDC_ISSUER.replace(/\/$/, "");
  const response = await fetchWithTimeout(`${issuer}/.well-known/openid-configuration`);
  if (!response.ok) throw new IdentityProviderUnavailable("discovery failed");
  const parsed = discoverySchema.safeParse(await response.json().catch(() => null));
  if (!parsed.success || parsed.data.issuer !== issuer) {
    throw new IdentityProviderUnavailable("discovery document is invalid");
  }
  const fresh: Cached = {
    discovery: parsed.data,
    jwks: createRemoteJWKSet(new URL(parsed.data.jwks_uri), { timeoutDuration: FETCH_TIMEOUT_MS }),
    fetchedAt: Date.now(),
  };
  holder[CACHE_KEY] = fresh;
  return fresh;
}

export function redirectUri(env: WebEnv = webEnv()): string {
  return new URL("/auth/callback", env.RAGOPS_WEB_PUBLIC_URL).toString();
}

export function authorizationUrl(
  endpoint: string,
  params: { state: string; nonce: string; challenge: string; locale: string },
  env: WebEnv = webEnv(),
): string {
  const url = new URL(endpoint);
  url.search = new URLSearchParams({
    response_type: "code",
    client_id: env.RAGOPS_WEB_OIDC_CLIENT_ID,
    redirect_uri: redirectUri(env),
    scope: env.RAGOPS_WEB_OIDC_SCOPE,
    state: params.state,
    nonce: params.nonce,
    code_challenge: params.challenge,
    code_challenge_method: "S256",
    ui_locales: params.locale === "en-US" ? "en" : "de",
  }).toString();
  return url.toString();
}

async function tokenRequest(form: Record<string, string>, env: WebEnv): Promise<TokenResponse> {
  const { discovery: doc } = await discovery(env);
  const basic = Buffer.from(
    `${encodeURIComponent(env.RAGOPS_WEB_OIDC_CLIENT_ID)}:${encodeURIComponent(env.RAGOPS_WEB_OIDC_CLIENT_SECRET)}`,
  ).toString("base64");
  const response = await fetchWithTimeout(doc.token_endpoint, {
    method: "POST",
    headers: { "content-type": "application/x-www-form-urlencoded", authorization: `Basic ${basic}` },
    body: new URLSearchParams(form).toString(),
  });
  if (response.status >= 500) throw new IdentityProviderUnavailable("token endpoint failed");
  const parsed = tokenResponseSchema.safeParse(await response.json().catch(() => null));
  if (!response.ok || !parsed.success) throw new TokenRejected("token request rejected");
  return parsed.data;
}

export function exchangeCode(code: string, verifier: string, env: WebEnv = webEnv()) {
  return tokenRequest(
    { grant_type: "authorization_code", code, code_verifier: verifier, redirect_uri: redirectUri(env) },
    env,
  );
}

export function refreshTokens(refreshToken: string, env: WebEnv = webEnv()) {
  return tokenRequest({ grant_type: "refresh_token", refresh_token: refreshToken }, env);
}

export function claimValue(claims: JWTPayload, path: string): unknown {
  let value: unknown = claims;
  for (const part of path.split(".")) {
    if (typeof value !== "object" || value === null || !(part in value)) return undefined;
    value = (value as Record<string, unknown>)[part];
  }
  return value;
}

/** The same checks the API applies, so the BFF never holds a token the API would reject. */
export async function verifyAccessToken(token: string, env: WebEnv = webEnv()): Promise<{ user: SessionUser; expiresAt: number }> {
  const { jwks, discovery: doc } = await discovery(env);
  const header = decodeProtectedHeader(token);
  if (header.typ && !["jwt", "at+jwt", "application/at+jwt"].includes(header.typ.toLowerCase())) {
    throw new TokenRejected("not an access token");
  }
  let payload: JWTPayload;
  try {
    ({ payload } = await jwtVerify(token, jwks, {
      issuer: doc.issuer,
      audience: env.RAGOPS_WEB_OIDC_AUDIENCE,
      algorithms: ALGORITHMS,
      requiredClaims: ["exp", "sub"],
    }));
  } catch {
    throw new TokenRejected("access token rejected");
  }
  const typ = payload.typ;
  if (typeof typ === "string" && !["bearer", "at+jwt"].includes(typ.toLowerCase())) {
    throw new TokenRejected("not an access token");
  }
  const party = payload.azp ?? payload.client_id;
  if (party !== env.RAGOPS_WEB_OIDC_CLIENT_ID) throw new TokenRejected("token issued to another client");
  const tenant = claimValue(payload, env.RAGOPS_WEB_OIDC_TENANT_CLAIM);
  if (typeof tenant !== "string" || !tenant || typeof payload.sub !== "string") {
    throw new TokenRejected("tenant or subject missing");
  }
  const rawRoles = claimValue(payload, env.RAGOPS_WEB_OIDC_ROLES_CLAIM);
  const roles = Array.isArray(rawRoles) ? rawRoles.filter((r): r is string => typeof r === "string") : [];
  const text = (key: string) => (typeof payload[key] === "string" && payload[key] ? (payload[key] as string) : null);
  return {
    user: {
      subject: payload.sub,
      name: text("name") ?? text("preferred_username"),
      email: text("email"),
      tenantId: tenant,
      roles,
    },
    expiresAt: (payload.exp ?? 0) * 1000,
  };
}

export async function verifyIdToken(token: string, nonce: string, env: WebEnv = webEnv()): Promise<void> {
  const { jwks, discovery: doc } = await discovery(env);
  try {
    const { payload } = await jwtVerify(token, jwks, {
      issuer: doc.issuer,
      audience: env.RAGOPS_WEB_OIDC_CLIENT_ID,
      algorithms: ALGORITHMS,
    });
    if (payload.nonce !== nonce) throw new TokenRejected("nonce mismatch");
  } catch (error) {
    if (error instanceof TokenRejected) throw error;
    throw new TokenRejected("id token rejected");
  }
}

export async function endSessionUrl(idToken: string | null, env: WebEnv = webEnv()): Promise<string | null> {
  try {
    const { discovery: doc } = await discovery(env);
    if (!doc.end_session_endpoint) return null;
    const url = new URL(doc.end_session_endpoint);
    const params = new URLSearchParams({
      client_id: env.RAGOPS_WEB_OIDC_CLIENT_ID,
      post_logout_redirect_uri: new URL("/signin?signedOut=1", env.RAGOPS_WEB_PUBLIC_URL).toString(),
    });
    if (idToken) params.set("id_token_hint", idToken);
    url.search = params.toString();
    return url.toString();
  } catch {
    return null;
  }
}
