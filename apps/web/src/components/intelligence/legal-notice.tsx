"use client";

import { Scale } from "lucide-react";
import { useTranslations } from "next-intl";

/** Shown wherever amounts appear: extracted statements are not a legal assessment of debt. */
export function LegalNotice() {
  const t = useTranslations("intelligence");
  return (
    <p className="flex items-start gap-2 rounded-md border border-warning-foreground/20 bg-warning px-3 py-2 text-xs text-warning-foreground" data-testid="legal-notice">
      <Scale aria-hidden className="mt-0.5 size-4 shrink-0" />
      {t("legalNotice")}
    </p>
  );
}
