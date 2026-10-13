import { cookies } from "next/headers";
import { resolveSession, sessionCookieOptions } from "@/server/auth";
import { webEnv } from "@/server/env";
import { errorResponse, proxyRequest } from "@/server/proxy";
import { SESSION_COOKIE } from "@/server/session";

export const dynamic = "force-dynamic";
export const runtime = "nodejs";

type Context = { params: Promise<{ path: string[] }> };

async function handle(request: Request, context: Context): Promise<Response> {
  const jar = await cookies();
  const state = await resolveSession(jar.get(SESSION_COOKIE)?.value);
  if (state.status === "missing") return errorResponse(401, "unauthenticated");
  if (state.status === "expired") {
    jar.set(SESSION_COOKIE, "", { ...sessionCookieOptions, maxAge: 0 });
    return errorResponse(401, "session_expired");
  }
  const env = webEnv();
  const { path } = await context.params;
  return proxyRequest(request, path, {
    apiUrl: env.RAGOPS_WEB_API_URL,
    accessToken: state.session.accessToken,
    csrfToken: state.session.csrfToken,
    maxUploadBytes: env.RAGOPS_WEB_MAX_UPLOAD_BYTES,
  });
}

export const GET = handle;
export const POST = handle;
export const PATCH = handle;
export const PUT = handle;
export const DELETE = handle;
