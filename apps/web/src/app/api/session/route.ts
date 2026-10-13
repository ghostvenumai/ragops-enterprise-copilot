import { cookies } from "next/headers";
import { resolveSession, sessionCookieOptions } from "@/server/auth";
import { SESSION_COOKIE } from "@/server/session";

export const dynamic = "force-dynamic";
export const runtime = "nodejs";

/** Who is signed in, and the CSRF token for this session. Never any token material. */
export async function GET(): Promise<Response> {
  const jar = await cookies();
  const state = await resolveSession(jar.get(SESSION_COOKIE)?.value);
  const headers = { "cache-control": "no-store, private" };
  if (state.status === "missing" || state.status === "expired") {
    if (state.status === "expired") jar.set(SESSION_COOKIE, "", { ...sessionCookieOptions, maxAge: 0 });
    return Response.json({ authenticated: false, reason: state.status }, { status: 401, headers });
  }
  const { user, csrfToken, accessExpiresAt } = state.session;
  return Response.json(
    {
      authenticated: true,
      user: { name: user.name, email: user.email, tenantId: user.tenantId, roles: user.roles },
      csrfToken,
      accessExpiresAt,
      identityProvider: state.status === "idp_unavailable" ? "unavailable" : "available",
    },
    { headers },
  );
}
