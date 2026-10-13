import { cookies } from "next/headers";
import { NextResponse, type NextRequest } from "next/server";
import { sessionCookieOptions } from "@/server/auth";
import { safeEqual } from "@/server/crypto";
import { publicOrigin, webEnv } from "@/server/env";
import { openTransaction } from "@/server/login-transaction";
import { IdentityProviderUnavailable, exchangeCode, verifyAccessToken, verifyIdToken } from "@/server/oidc";
import { LOGIN_COOKIE, SESSION_COOKIE, sessionStore } from "@/server/session";

export const dynamic = "force-dynamic";
export const runtime = "nodejs";

function fail(reason: string): Response {
  const response = NextResponse.redirect(new URL(`/signin?error=${reason}`, publicOrigin()), 303);
  response.cookies.set(LOGIN_COOKIE, "", { ...sessionCookieOptions, maxAge: 0 });
  response.headers.set("cache-control", "no-store");
  return response;
}

export async function GET(request: NextRequest): Promise<Response> {
  const params = request.nextUrl.searchParams;
  const jar = await cookies();
  const tx = await openTransaction(jar.get(LOGIN_COOKIE)?.value);
  if (!tx || !safeEqual(params.get("state"), tx.state)) return fail("invalid_state");
  if (params.get("error")) return fail("denied");
  const code = params.get("code");
  if (!code || code.length > 4096) return fail("invalid_state");
  try {
    const tokens = await exchangeCode(code, tx.verifier);
    if (!tokens.id_token) return fail("token_rejected");
    await verifyIdToken(tokens.id_token, tx.nonce);
    const verified = await verifyAccessToken(tokens.access_token);
    const store = sessionStore();
    // Never reuse an identifier issued before authentication (session fixation).
    store.delete(jar.get(SESSION_COOKIE)?.value);
    const { id } = store.create({
      user: verified.user,
      accessToken: tokens.access_token,
      accessExpiresAt: verified.expiresAt,
      refreshToken: tokens.refresh_token ?? null,
      refreshExpiresAt: tokens.refresh_expires_in ? Date.now() + tokens.refresh_expires_in * 1000 : null,
      idToken: tokens.id_token,
    });
    const response = NextResponse.redirect(new URL(tx.returnTo, publicOrigin()), 303);
    response.cookies.set(SESSION_COOKIE, id, { ...sessionCookieOptions, maxAge: webEnv().RAGOPS_WEB_SESSION_MAX_SECONDS });
    response.cookies.set(LOGIN_COOKIE, "", { ...sessionCookieOptions, maxAge: 0 });
    response.headers.set("cache-control", "no-store");
    return response;
  } catch (error) {
    return fail(error instanceof IdentityProviderUnavailable ? "idp_unavailable" : "token_rejected");
  }
}
