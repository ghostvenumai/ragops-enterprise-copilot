import { cookies } from "next/headers";
import { NextResponse, type NextRequest } from "next/server";
import { sessionCookieOptions } from "@/server/auth";
import { pkceChallenge, randomToken } from "@/server/crypto";
import { publicOrigin } from "@/server/env";
import { LOCALE_COOKIE, resolveLocale } from "@/i18n/config";
import { TRANSACTION_MAX_AGE, safeReturnTo, sealTransaction } from "@/server/login-transaction";
import { authorizationUrl, discovery } from "@/server/oidc";
import { LOGIN_COOKIE } from "@/server/session";

export const dynamic = "force-dynamic";
export const runtime = "nodejs";

/** Starts Authorization Code + PKCE; the browser only carries the sealed transaction. */
export async function GET(request: NextRequest): Promise<Response> {
  const origin = publicOrigin();
  const returnTo = safeReturnTo(request.nextUrl.searchParams.get("returnTo"));
  let endpoint: string;
  try {
    endpoint = (await discovery()).discovery.authorization_endpoint;
  } catch {
    return NextResponse.redirect(new URL("/signin?error=idp_unavailable", origin), 303);
  }
  const verifier = randomToken(48);
  const tx = { state: randomToken(24), nonce: randomToken(24), verifier, returnTo };
  const jar = await cookies();
  const locale = resolveLocale(jar.get(LOCALE_COOKIE)?.value);
  const response = NextResponse.redirect(
    authorizationUrl(endpoint, { state: tx.state, nonce: tx.nonce, challenge: pkceChallenge(verifier), locale }),
    303,
  );
  response.cookies.set(LOGIN_COOKIE, await sealTransaction(tx), { ...sessionCookieOptions, maxAge: TRANSACTION_MAX_AGE });
  response.headers.set("cache-control", "no-store");
  return response;
}
