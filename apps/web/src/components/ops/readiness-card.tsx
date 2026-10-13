"use client";

import { HeartPulse } from "lucide-react";
import { useTranslations } from "next-intl";
import { ApiErrorAlert } from "@/components/common/api-error";
import { StatusBadge } from "@/components/common/status-badge";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { LoadingRows } from "@/components/ui/feedback";
import { TBody, TD, TH, THead, TR, Table } from "@/components/ui/table";
import { useApiQuery } from "@/lib/api/hooks";
import { readinessSchema } from "@/lib/api/schemas";

export function ReadinessCard() {
  const t = useTranslations("monitoring.readiness");
  const tc = useTranslations("cockpit");
  const ready = useApiQuery("/ready", readinessSchema, { refetchInterval: 15_000, allowStatus: 503 });
  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <HeartPulse aria-hidden className="size-4 text-primary" />
          {t("title")}
        </CardTitle>
        <CardDescription>{t("hint")}</CardDescription>
      </CardHeader>
      <CardContent>
        {ready.isPending ? (
          <LoadingRows label={t("loading")} />
        ) : ready.isError ? (
          <ApiErrorAlert error={ready.error} />
        ) : (
          <>
            <div className="mb-3 flex items-center gap-2 text-sm">
              {t("overall")}: <StatusBadge value={ready.data.status} />
            </div>
            <Table caption={t("title")}>
              <THead>
                <TR>
                  <TH>{t("component")}</TH>
                  <TH>{t("critical")}</TH>
                  <TH>{t("status")}</TH>
                  <TH>{t("reason")}</TH>
                </TR>
              </THead>
              <TBody>
                {Object.entries(ready.data.components).map(([name, component]) => (
                  <TR key={name}>
                    <TD className="font-medium">{tc.has(`components.${name}` as never) ? tc(`components.${name}` as never) : name}</TD>
                    <TD>{component.critical ? t("yes") : t("no")}</TD>
                    <TD>
                      <StatusBadge value={component.status} />
                    </TD>
                    <TD className="font-mono text-xs">{component.reason}</TD>
                  </TR>
                ))}
              </TBody>
            </Table>
          </>
        )}
      </CardContent>
    </Card>
  );
}
