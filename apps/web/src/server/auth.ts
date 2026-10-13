import "server-only";
import { cookies } from "next/headers";
import { webEnv } from "./env";
import { IdentityProviderUnavailable, refreshTokens, verifyAccessToken } from "./oidc";
import { SESSION_COOKIE, sessionStore, type SessionRecord } from "./session";

const REFRESH_MARGIN_MS = 30_000;

export function sessionLimits() {
  const env = webEnv();
  return { idleMs: env.RAGOPS_WEB_SESSION_IDLE_SECONDS * 1000, absoluteMs: env.RAGOPS_WEB_SESSION_MAX_SECONDS * 1000 };
}

export type SessionState =
  | { status: "active"; id: string; session: SessionRecord }
  | { status: "missing" }
  | { status: "expired" }
  | { status: "idp_unavailable"; id: string; session: SessionRecord };

/**
 * Resolve the session of the current request and refresh its access token when it is about
 * to expire. A failed refresh ends the session (fail closed); an unreachable identity
 * provider keeps the still-valid token until it actually expires.
 */
export async function resolveSession(id: string | undefined): Promise<SessionState> {
  const store = sessionStore();
  const session = store.get(id, sessionLimits());
  if (!id || !session) return id ? { status: "expired" } : { status: "missing" };
  if (session.accessExpiresAt - Date.now() > REFRESH_MARGIN_MS) return { status: "active", id, session };
  if (!session.refreshToken || (session.refreshExpiresAt !== null && session.refreshExpiresAt <= Date.now())) {
    store.delete(id);
    return { status: "expired" };
  }
  try {
    const refreshed = await store.refreshOnce(id, async () => {
      const tokens = await refreshTokens(session.refreshToken as string);
      const verified = await verifyAccessToken(tokens.access_token);
      // A refresh must never switch the principal or tenant of an existing session.
      if (verified.user.subject !== session.user.subject || verified.user.tenantId !== session.user.tenantId) {
        return null;
      }
      const changes: Partial<SessionRecord> = {
        user: verified.user,
        accessToken: tokens.access_token,
        accessExpiresAt: verified.expiresAt,
        refreshToken: tokens.refresh_token ?? session.refreshToken,
        refreshExpiresAt: tokens.refresh_expires_in ? Date.now() + tokens.refresh_expires_in * 1000 : session.refreshExpiresAt,
        idToken: tokens.id_token ?? session.idToken,
      };
      store.update(id, changes);
      return { ...session, ...changes };
    });
    if (!refreshed) {
      store.delete(id);
      return { status: "expired" };
    }
    return { status: "active", id, session: refreshed };
  } catch (error) {
    if (error instanceof IdentityProviderUnavailable && session.accessExpiresAt > Date.now()) {
      return { status: "idp_unavailable", id, session };
    }
    store.delete(id);
    return { status: "expired" };
  }
}

export async function currentSession(): Promise<SessionState> {
  const jar = await cookies();
  return resolveSession(jar.get(SESSION_COOKIE)?.value);
}

export const sessionCookieOptions = {
  httpOnly: true,
  secure: true,
  sameSite: "lax" as const,
  path: "/",
};
