import "server-only";
import { EncryptJWT, jwtDecrypt } from "jose";
import { derivedKey } from "./crypto";
import { webEnv } from "./env";

export interface LoginTransaction {
  state: string;
  nonce: string;
  verifier: string;
  returnTo: string;
}

const TTL_SECONDS = 600;

/** Short-lived, encrypted and integrity-protected state of one sign-in attempt. */
export async function sealTransaction(tx: LoginTransaction): Promise<string> {
  return new EncryptJWT({ ...tx })
    .setProtectedHeader({ alg: "dir", enc: "A256GCM" })
    .setIssuedAt()
    .setExpirationTime(`${TTL_SECONDS}s`)
    .encrypt(derivedKey(webEnv().RAGOPS_WEB_SESSION_SECRET, "login-transaction"));
}

export async function openTransaction(value: string | undefined): Promise<LoginTransaction | null> {
  if (!value || value.length > 4096) return null;
  try {
    const { payload } = await jwtDecrypt(value, derivedKey(webEnv().RAGOPS_WEB_SESSION_SECRET, "login-transaction"));
    const { state, nonce, verifier, returnTo } = payload;
    if ([state, nonce, verifier, returnTo].every((item) => typeof item === "string")) {
      return { state, nonce, verifier, returnTo } as LoginTransaction;
    }
  } catch {
    return null;
  }
  return null;
}

export const TRANSACTION_MAX_AGE = TTL_SECONDS;

/** Only same-application paths; never protocol-relative or absolute URLs (open redirect). */
export function safeReturnTo(value: string | null | undefined): string {
  if (!value || !value.startsWith("/") || value.startsWith("//") || value.startsWith("/\\")) return "/";
  if (value.startsWith("/auth/") || value.startsWith("/api/") || value.length > 512) return "/";
  if (/[\u0000-\u001f]/.test(value)) return "/";
  return value;
}
