import { resetWebEnvForTests } from "@/server/env";

export const PUBLIC_URL = "http://localhost:3999";

export function setTestEnv(overrides: Record<string, string> = {}): void {
  Object.assign(process.env, {
    RAGOPS_WEB_API_URL: "http://127.0.0.1:1",
    RAGOPS_WEB_PUBLIC_URL: PUBLIC_URL,
    RAGOPS_WEB_OIDC_ISSUER: "http://127.0.0.1:2/realms/test",
    RAGOPS_WEB_OIDC_CLIENT_ID: "ragops-web-test",
    RAGOPS_WEB_OIDC_CLIENT_SECRET: "test-client-secret-value-0123456789",
    RAGOPS_WEB_SESSION_SECRET: "test-session-secret-0123456789-0123456789",
    ...overrides,
  });
  resetWebEnvForTests();
}
