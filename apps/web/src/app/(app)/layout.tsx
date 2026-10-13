import { redirect } from "next/navigation";
import { AppShell } from "@/components/layout/app-shell";
import { AppProviders } from "@/components/providers/app-providers";
import { currentSession } from "@/server/auth";

export const dynamic = "force-dynamic";

/** Every application page is rendered for a verified, server-side session only. */
export default async function AuthenticatedLayout({ children }: { children: React.ReactNode }) {
  const state = await currentSession();
  if (state.status === "missing") redirect("/signin");
  if (state.status === "expired") redirect("/signin?error=session_expired");
  const { user, csrfToken } = state.session;
  return (
    <AppProviders session={{ name: user.name, email: user.email, tenantId: user.tenantId, roles: user.roles, csrfToken }}>
      <AppShell>{children}</AppShell>
    </AppProviders>
  );
}
