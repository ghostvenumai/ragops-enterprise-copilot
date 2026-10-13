import { NextResponse, type NextRequest } from "next/server";

const SESSION_COOKIE = "__Host-ragops-session";
const PUBLIC_PATHS = ["/signin", "/auth/", "/api/", "/favicon.ico"];

function contentSecurityPolicy(nonce: string): string {
  const development = process.env.NODE_ENV === "development";
  return [
    "default-src 'self'",
    `script-src 'self' 'nonce-${nonce}' 'strict-dynamic'${development ? " 'unsafe-eval'" : ""}`,
    // Tailwind output is a static stylesheet; Recharts and Radix set inline style attributes.
    "style-src 'self' 'unsafe-inline'",
    "img-src 'self' data: blob:",
    "font-src 'self'",
    `connect-src 'self'${development ? " ws:" : ""}`,
    "frame-ancestors 'none'",
    "form-action 'self'",
    "base-uri 'none'",
    "object-src 'none'",
  ].join("; ");
}

export function middleware(request: NextRequest) {
  const { pathname, search } = request.nextUrl;
  const isPublic = PUBLIC_PATHS.some((prefix) => pathname === prefix || pathname.startsWith(prefix));
  // Cheap presence check only; the server layout verifies the session itself.
  if (!isPublic && !request.cookies.get(SESSION_COOKIE)) {
    const target = new URL("/signin", request.url);
    target.searchParams.set("returnTo", `${pathname}${search}`);
    return NextResponse.redirect(target, 303);
  }
  const nonce = btoa(crypto.randomUUID());
  const csp = contentSecurityPolicy(nonce);
  const requestHeaders = new Headers(request.headers);
  requestHeaders.set("x-nonce", nonce);
  requestHeaders.set("content-security-policy", csp);
  const response = NextResponse.next({ request: { headers: requestHeaders } });
  response.headers.set("content-security-policy", csp);
  response.headers.set("x-content-type-options", "nosniff");
  response.headers.set("x-frame-options", "DENY");
  response.headers.set("referrer-policy", "no-referrer");
  response.headers.set("cross-origin-opener-policy", "same-origin");
  response.headers.set("cross-origin-resource-policy", "same-origin");
  response.headers.set("permissions-policy", "camera=(), microphone=(), geolocation=(), payment=(), usb=()");
  if (request.headers.get("x-forwarded-proto") === "https" || request.nextUrl.protocol === "https:") {
    response.headers.set("strict-transport-security", "max-age=63072000; includeSubDomains");
  }
  // Pages contain tenant data: never store them in shared or browser caches.
  response.headers.set("cache-control", "no-store, private");
  return response;
}

export const config = {
  matcher: [{ source: "/((?!_next/static|_next/image|favicon.ico).*)" }],
};
