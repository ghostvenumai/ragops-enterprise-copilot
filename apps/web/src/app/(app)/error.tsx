"use client";

import { useTranslations } from "next-intl";
import { Button } from "@/components/ui/button";
import { Alert } from "@/components/ui/feedback";

/** Rendering failures show a generic message; the error itself is never displayed. */
export default function AppError({ reset }: { error: Error; reset: () => void }) {
  const t = useTranslations("errors.unknown");
  return (
    <div className="space-y-3">
      <Alert tone="danger" title={t("title")} live>
        {t("body")}
      </Alert>
      <Button variant="outline" onClick={reset}>
        {t("retry")}
      </Button>
    </div>
  );
}
