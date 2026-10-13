"use client";

import { FileText } from "lucide-react";
import { useLocale, useTranslations } from "next-intl";
import { z } from "zod";
import { ApiErrorAlert } from "@/components/common/api-error";
import { StatusBadge } from "@/components/common/status-badge";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Alert, LoadingRows } from "@/components/ui/feedback";
import { TBody, TD, TH, THead, TR, Table } from "@/components/ui/table";
import { useApiQuery } from "@/lib/api/hooks";
import { pageReportSchema, versionSchema, type Job } from "@/lib/api/schemas";
import { formatNumber, formatPercent } from "@/lib/format";
import { useLabel } from "@/components/intelligence/use-label";

export function DocumentDetails({ job, onClose }: { job: Job | null; onClose: () => void }) {
  const t = useTranslations("knowledge.details");
  const locale = useLocale();
  const method = useLabel("intelligence.extractionMethods");
  const versions = useApiQuery(job ? `/v1/documents/${job.document_id}/versions` : null, z.array(versionSchema));
  const pages = useApiQuery(job ? `/v1/documents/${job.document_id}/versions/${job.version_id}/pages` : null, pageReportSchema, { retry: false });
  return (
    <Dialog open={job !== null} onOpenChange={(open) => (open ? undefined : onClose())}>
      <DialogContent closeLabel={t("close")} className="max-w-3xl" data-testid="document-details">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <FileText aria-hidden className="size-5 text-primary" />
            {job?.title ?? job?.filename ?? t("document")}
          </DialogTitle>
          <DialogDescription>{t("description")}</DialogDescription>
        </DialogHeader>
        <section className="space-y-2">
          <h3 className="text-sm font-semibold">{t("versions")}</h3>
          {versions.isPending ? (
            <LoadingRows label={t("loading")} rows={2} />
          ) : versions.isError ? (
            <ApiErrorAlert error={versions.error} />
          ) : (
            <Table caption={t("versions")}>
              <THead>
                <TR>
                  <TH>{t("version")}</TH>
                  <TH>{t("filename")}</TH>
                  <TH>{t("size")}</TH>
                  <TH>{t("status")}</TH>
                </TR>
              </THead>
              <TBody>
                {versions.data.map((version) => (
                  <TR key={version.id}>
                    <TD className="tabular">v{version.version}</TD>
                    <TD className="max-w-[16rem] truncate">{version.filename}</TD>
                    <TD className="tabular">{formatNumber(Math.round(version.size / 1024), locale)} KB</TD>
                    <TD>
                      <StatusBadge value={version.ingestion_status} />
                    </TD>
                  </TR>
                ))}
              </TBody>
            </Table>
          )}
        </section>
        <section className="space-y-2">
          <h3 className="text-sm font-semibold">{t("pages")}</h3>
          {pages.isPending ? (
            <LoadingRows label={t("loading")} rows={2} />
          ) : pages.isError ? (
            <Alert tone="info">{t("noPageReport")}</Alert>
          ) : (
            <>
              {pages.data.review_required ? (
                <Alert tone="warning" title={t("reviewRequiredTitle")}>
                  {t("reviewRequiredBody")}
                </Alert>
              ) : null}
              <Table caption={t("pages")}>
                <THead>
                  <TR>
                    <TH>{t("page")}</TH>
                    <TH>{t("method")}</TH>
                    <TH>{t("confidence")}</TH>
                    <TH>{t("words")}</TH>
                    <TH>{t("quality")}</TH>
                  </TR>
                </THead>
                <TBody>
                  {pages.data.pages.map((page) => (
                    <TR key={page.page_number} data-testid="page-row">
                      <TD className="tabular">{page.page_number}</TD>
                      <TD>{method(page.extraction_method)}</TD>
                      <TD className="tabular">{page.mean_confidence == null ? "—" : formatPercent(page.mean_confidence / 100, locale)}</TD>
                      <TD className="tabular">{page.word_count == null ? "—" : formatNumber(page.word_count, locale)}</TD>
                      <TD>
                        <StatusBadge value={page.quality_state} />
                        {page.flags.length ? <span className="ml-2 text-xs text-muted-foreground">{page.flags.join(", ")}</span> : null}
                      </TD>
                    </TR>
                  ))}
                </TBody>
              </Table>
            </>
          )}
        </section>
      </DialogContent>
    </Dialog>
  );
}
