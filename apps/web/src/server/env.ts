import "server-only";
import { z } from "zod";

/**
 * Server-only configuration. Nothing here is ever sent to the browser: the client
 * secret, session secret and upstream URLs stay inside the Backend-for-Frontend.
 */
const schema = z.object({
  RAGOPS_WEB_API_URL: z.url(),
  RAGOPS_WEB_PUBLIC_URL: z.url(),
  RAGOPS_WEB_OIDC_ISSUER: z.url(),
  RAGOPS_WEB_OIDC_CLIENT_ID: z.string().min(1).max(255),
  RAGOPS_WEB_OIDC_CLIENT_SECRET: z.string().min(16).max(512),
  RAGOPS_WEB_OIDC_AUDIENCE: z.string().min(1).default("ragops-api"),
  RAGOPS_WEB_OIDC_ROLES_CLAIM: z.string().min(1).default("resource_access.ragops-api.roles"),
  RAGOPS_WEB_OIDC_TENANT_CLAIM: z.string().min(1).default("tenant_id"),
  RAGOPS_WEB_OIDC_SCOPE: z.string().min(1).default("openid profile email"),
  RAGOPS_WEB_SESSION_SECRET: z.string().min(32),
  RAGOPS_WEB_SESSION_IDLE_SECONDS: z.coerce.number().int().min(60).max(86_400).default(1800),
  RAGOPS_WEB_SESSION_MAX_SECONDS: z.coerce.number().int().min(300).max(86_400).default(28_800),
  RAGOPS_WEB_MAX_UPLOAD_BYTES: z.coerce.number().int().min(1024).max(50_000_000).default(12_000_000),
});

export type WebEnv = z.infer<typeof schema>;

let cached: WebEnv | undefined;

export class ConfigurationError extends Error {}

export function webEnv(): WebEnv {
  if (cached) return cached;
  const parsed = schema.safeParse(process.env);
  if (!parsed.success) {
    // Only variable names are reported, never values.
    const names = [...new Set(parsed.error.issues.map((issue) => issue.path.join(".")))];
    throw new ConfigurationError(`invalid web configuration: ${names.join(", ")}`);
  }
  const env = parsed.data;
  const publicUrl = new URL(env.RAGOPS_WEB_PUBLIC_URL);
  const local = publicUrl.hostname === "localhost" || publicUrl.hostname === "127.0.0.1";
  if (publicUrl.protocol !== "https:" && !local) {
    throw new ConfigurationError("RAGOPS_WEB_PUBLIC_URL must use HTTPS outside localhost");
  }
  cached = env;
  return env;
}

export function resetWebEnvForTests(): void {
  cached = undefined;
}

export function publicOrigin(): string {
  return new URL(webEnv().RAGOPS_WEB_PUBLIC_URL).origin;
}
