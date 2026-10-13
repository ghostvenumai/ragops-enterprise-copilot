"use client";

import { ShieldX } from "lucide-react";
import { useTranslations } from "next-intl";
import { useSession } from "@/components/providers/session-context";
import { EmptyState } from "@/components/ui/feedback";

/** UI guard for administration pages. The API still rejects every unauthorized request. */
export function RequireRole({ role, children }: { role: "admin"; children: React.ReactNode }) {
  const t = useTranslations("errors.forbidden");
  const { roles } = useSession();
  if (!roles.includes(role)) {
    return (
      <div data-testid="forbidden-page">
        <EmptyState icon={ShieldX} title={t("title")} description={t("pageBody")} />
      </div>
    );
  }
  return <>{children}</>;
}
