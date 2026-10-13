import { cookies } from "next/headers";
import { resolveSession, sessionCookieOptions } from "@/server/auth";
import { mutationAllowed } from "@/server/csrf";
import { endSessionUrl } from "@/server/oidc";
import { SESSION_COOKIE, sessionStore } from "@/server/session";

export const dynamic = "force-dynamic";
export const runtime = "nodejs";

/** Ends the local session first, then hands the browser the provider sign-out URL. */
export async function POST(request: Request): Promise<Response> {
  const jar = await cookies();
  const id = jar.get(SESSION_COOKIE)?.value;
  const state = await resolveSession(id);
  const headers = { "cache-control": "no-store" };
  if (state.status === "active" || state.status === "idp_unavailable") {
    if (!mutationAllowed(request, state.session.csrfToken)) {
      return Response.json({ error: "csrf_rejected" }, { status: 403, headers });
    }
  }
  const ended = sessionStore().delete(id);
  jar.set(SESSION_COOKIE, "", { ...sessionCookieOptions, maxAge: 0 });
  const redirect = (await endSessionUrl(ended?.idToken ?? null)) ?? "/signin?signedOut=1";
  return Response.json({ redirect }, { headers });
}
