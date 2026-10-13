import "server-only";
import { safeEqual } from "./crypto";
import { publicOrigin } from "./env";

export const CSRF_HEADER = "x-csrf-token";

/**
 * State-changing requests need both a same-origin Origin header and the session's
 * synchronizer token in a custom header (which a cross-site form cannot set).
 */
export function mutationAllowed(request: Request, csrfToken: string): boolean {
  const origin = request.headers.get("origin");
  if (!origin || origin !== publicOrigin()) return false;
  const site = request.headers.get("sec-fetch-site");
  if (site && site !== "same-origin") return false;
  return safeEqual(request.headers.get(CSRF_HEADER), csrfToken);
}
