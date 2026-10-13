"use client";

import { useQueryClient } from "@tanstack/react-query";
import { Eye, ListChecks, RotateCcw, XCircle } from "lucide-react";
import { useLocale, useTranslations } from "next-intl";
import * as React from "react";
import { z } from "zod";
import { ApiErrorAlert } from "@/components/common/api-error";
import { StatusBadge } from "@/components/common/status-badge";
import { useSession } from "@/components/providers/session-context";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Alert, EmptyState, LoadingRows } from "@/components/ui/feedback";
import { TBody, TD, TH, THead, TR, Table } from "@/components/ui/table";
import { api } from "@/lib/api/client";
import { useApiQuery } from "@/lib/api/hooks";
import { jobActionSchema, jobSchema, type Job } from "@/lib/api/schemas";
import { formatNumber, formatPercent } from "@/lib/format";
import { canManageIngestion } from "@/lib/roles";
import { DocumentDetails } from "./document-details";

const ACTIVE = new Set(["queued", "running", "processing", "retrying"]);
const RETRYABLE = new Set(["failed", "retrying"]);

type Notice = { tone: "success" | "danger"; key: "retried" | "cancelled" | "actionFailed"; title: string; error?: unknown };

export function JobsTable() {
  const t = useTranslations("knowledge.jobs");
  const locale = useLocale();
  const session = useSession();
  const queryClient = useQueryClient();
  const jobs = useApiQuery("/v1/ingestion/jobs", z.array(jobSchema), {
    // Poll only while something is still moving.
    refetchInterval: (query) => ((query.state.data as Job[] | undefined)?.some((job) => ACTIVE.has(job.status)) ? 3000 : 20_000),
  });
  const [details, setDetails] = React.useState<Job | null>(null);
  const [pending, setPending] = React.useState<string | null>(null);
  // The outcome stays visible across polling refreshes until the next action.
  const [notice, setNotice] = React.useState<Notice | null>(null);
  const manage = canManageIngestion(session.roles);

  async function act(job: Job, action: "retry" | "cancel") {
    setPending(job.job_id);
    const title = job.title ?? job.filename ?? job.job_id;
    try {
      await api.post(`/v1/ingestion/jobs/${job.job_id}/${action}`, undefined, jobActionSchema, { csrfToken: session.csrfToken });
      setNotice({ tone: "success", key: action === "retry" ? "retried" : "cancelled", title });
      await queryClient.invalidateQueries({ queryKey: ["api", "/v1/ingestion/jobs"] });
    } catch (error) {
      setNotice({ tone: "danger", key: "actionFailed", title, error });
    } finally {
      setPending(null);
    }
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <ListChecks aria-hidden className="size-4 text-primary" />
          {t("title")}
        </CardTitle>
        <CardDescription>{t("description")}</CardDescription>
      </CardHeader>
      <CardContent className="space-y-3">
        <div aria-live="polite" data-testid="jobs-notice">
          {notice ? (
            notice.tone === "success" ? (
              <Alert tone="success" title={t(`notices.${notice.key}`)}>
                {notice.title}
              </Alert>
            ) : (
              <ApiErrorAlert error={notice.error} />
            )
          ) : null}
        </div>
        {jobs.isPending ? (
          <LoadingRows label={t("loading")} rows={4} />
        ) : jobs.isError ? (
          <ApiErrorAlert error={jobs.error} />
        ) : jobs.data.length === 0 ? (
          <EmptyState icon={ListChecks} title={t("empty")} description={t("emptyHint")} />
        ) : (
          <Table caption={t("title")} data-testid="jobs-table">
            <THead>
              <TR>
                <TH>{t("document")}</TH>
                <TH>{t("status")}</TH>
                <TH className="hidden md:table-cell">{t("stage")}</TH>
                <TH className="hidden md:table-cell">{t("pages")}</TH>
                <TH className="text-right">{t("actions")}</TH>
              </TR>
            </THead>
            <TBody>
              {jobs.data.map((job) => (
                <TR key={job.job_id} data-testid="job-row" data-title={job.title ?? ""} data-status={job.status}>
                  <TD className="max-w-[18rem]">
                    <p className="truncate font-medium">{job.title ?? job.filename ?? job.job_id}</p>
                    <p className="truncate text-xs text-muted-foreground">{job.filename}</p>
                  </TD>
                  <TD>
                    <div className="flex flex-wrap items-center gap-1.5">
                      <StatusBadge value={job.status} testId="job-status" />
                      {job.review_required ? <StatusBadge value="review_required" testId="job-review" /> : null}
                    </div>
                    {job.error_code ? <p className="mt-1 font-mono text-xs text-destructive">{job.error_code}</p> : null}
                  </TD>
                  <TD className="hidden md:table-cell">
                    <span className="text-sm">{job.stage ?? "—"}</span>
                    {job.progress != null && ACTIVE.has(job.status) ? (
                      <span className="ml-2 text-xs text-muted-foreground">{formatPercent(job.progress / 100, locale)}</span>
                    ) : null}
                  </TD>
                  <TD className="tabular hidden md:table-cell">
                    {job.pages_total ? `${formatNumber(job.pages_total, locale)}${job.pages_review ? ` · ${t("pagesReview", { count: job.pages_review })}` : ""}` : "—"}
                  </TD>
                  <TD>
                    <div className="flex justify-end gap-1">
                      <Button variant="ghost" size="sm" onClick={() => setDetails(job)} aria-label={t("detailsFor", { title: job.title ?? job.job_id })}>
                        <Eye aria-hidden />
                        <span className="hidden lg:inline">{t("details")}</span>
                      </Button>
                      {manage && RETRYABLE.has(job.status) ? (
                        <Button variant="outline" size="sm" disabled={pending === job.job_id} onClick={() => void act(job, "retry")} data-testid="job-retry">
                          <RotateCcw aria-hidden />
                          {t("retry")}
                        </Button>
                      ) : null}
                      {manage && job.status === "queued" ? (
                        <Button variant="ghost" size="sm" disabled={pending === job.job_id} onClick={() => void act(job, "cancel")}>
                          <XCircle aria-hidden />
                          <span className="hidden lg:inline">{t("cancel")}</span>
                        </Button>
                      ) : null}
                    </div>
                  </TD>
                </TR>
              ))}
            </TBody>
          </Table>
        )}
      </CardContent>
      <DocumentDetails job={details} onClose={() => setDetails(null)} />
    </Card>
  );
}
