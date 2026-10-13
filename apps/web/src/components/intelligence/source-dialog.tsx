"use client";

import { Quote } from "lucide-react";
import { useLocale, useTranslations } from "next-intl";
import { ApiErrorAlert } from "@/components/common/api-error";
import { StatusBadge } from "@/components/common/status-badge";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Alert, LoadingRows } from "@/components/ui/feedback";
import { useApiQuery } from "@/lib/api/hooks";
import { factDetailSchema } from "@/lib/api/schemas";
import { formatDate, formatMoney } from "@/lib/format";
import { useLabel } from "./use-label";

/**
 * Provenance of one extracted fact. The raw source text is shown exactly as extracted
 * (no translation, no markup). The original file itself is not served by the API, so the
 * page cannot be rendered or highlighted here; this is stated instead of approximated.
 */
export function SourceDialog({ factId, onClose }: { factId: string | null; onClose: () => void }) {
  const t = useTranslations("intelligence.source");
  const locale = useLocale();
  const factLabel = useLabel("intelligence.factTypes");
  const assertion = useLabel("intelligence.assertions");
  const method = useLabel("intelligence.extractionMethods");
  const fact = useApiQuery(factId ? `/v1/intelligence/facts/${factId}` : null, factDetailSchema);
  return (
    <Dialog open={factId !== null} onOpenChange={(open) => (open ? undefined : onClose())}>
      <DialogContent closeLabel={t("close")} className="max-w-2xl" data-testid="source-dialog">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <Quote aria-hidden className="size-5 text-primary" />
            {t("title")}
          </DialogTitle>
          <DialogDescription>{t("description")}</DialogDescription>
        </DialogHeader>
        {fact.isPending ? (
          <LoadingRows label={t("loading")} rows={4} />
        ) : fact.isError ? (
          <ApiErrorAlert error={fact.error} />
        ) : (
          <div className="space-y-4 text-sm">
            <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-2">
              <dt className="text-muted-foreground">{t("document")}</dt>
              <dd className="font-medium">{fact.data.source.title}</dd>
              <dt className="text-muted-foreground">{t("factType")}</dt>
              <dd>{factLabel(fact.data.fact_type)}</dd>
              <dt className="text-muted-foreground">{t("assertion")}</dt>
              <dd>{assertion(fact.data.assertion)}</dd>
              <dt className="text-muted-foreground">{t("page")}</dt>
              <dd className="tabular" data-testid="source-page">{fact.data.source.page_number ?? "—"}</dd>
              <dt className="text-muted-foreground">{t("line")}</dt>
              <dd className="tabular">{fact.data.source.line ?? "—"}</dd>
              <dt className="text-muted-foreground">{t("method")}</dt>
              <dd>{method(fact.data.source.extraction_method)}</dd>
              <dt className="text-muted-foreground">{t("quality")}</dt>
              <dd>
                <StatusBadge value={fact.data.quality_state} />
              </dd>
              <dt className="text-muted-foreground">{t("reviewState")}</dt>
              <dd>
                <StatusBadge value={fact.data.review_state} />
              </dd>
              {fact.data.amount ? (
                <>
                  <dt className="text-muted-foreground">{t("statedAmount")}</dt>
                  <dd className="tabular">{formatMoney(fact.data.amount, fact.data.currency, locale)}</dd>
                </>
              ) : null}
              {fact.data.date ? (
                <>
                  <dt className="text-muted-foreground">{t("statedDate")}</dt>
                  <dd>{formatDate(fact.data.date, locale)}</dd>
                </>
              ) : null}
            </dl>
            <figure className="rounded-md border bg-muted/50 p-3">
              <figcaption className="mb-1 text-xs font-semibold uppercase tracking-wide text-muted-foreground">{t("rawValue")}</figcaption>
              <blockquote className="whitespace-pre-wrap break-words font-mono text-sm" lang="" data-testid="source-raw">
                {fact.data.source.raw_value ?? "—"}
              </blockquote>
              <p className="mt-2 text-xs text-muted-foreground">{t("untranslated")}</p>
            </figure>
            <Alert tone="info" title={t("highlightLimitTitle")}>
              {t("highlightLimitBody", { regions: fact.data.source.regions.length })}
            </Alert>
          </div>
        )}
      </DialogContent>
    </Dialog>
  );
}
