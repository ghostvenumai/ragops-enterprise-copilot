"use client";

import { Activity, AlertTriangle, ClipboardCheck, Coins, FileStack, Gauge, HeartPulse, ListChecks } from "lucide-react";
import { useLocale, useTranslations } from "next-intl";
import Link from "next/link";
import { Bar, BarChart, CartesianGrid, ResponsiveContainer, Tooltip as ChartTooltip, XAxis, YAxis } from "recharts";
import { z } from "zod";
import { ApiErrorAlert } from "@/components/common/api-error";
import { PageHeader } from "@/components/common/page-header";
import { StatusBadge } from "@/components/common/status-badge";
import { useSession } from "@/components/providers/session-context";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Alert, EmptyState, LoadingRows, Skeleton } from "@/components/ui/feedback";
import { useApiQuery } from "@/lib/api/hooks";
import {
  auditEventSchema,
  costSummarySchema,
  findingSchema,
  healthSchema,
  intelligenceDocumentSchema,
  jobSchema,
  readinessSchema,
} from "@/lib/api/schemas";
import { formatDateTime, formatEuroFloat, formatNumber } from "@/lib/format";
import { isAdmin } from "@/lib/roles";

function Metric({ icon: Icon, label, value, hint, testId }: { icon: React.ElementType; label: string; value: React.ReactNode; hint?: string; testId?: string }) {
  return (
    <Card>
      <CardContent className="flex items-start gap-4 p-5">
        <div className="rounded-lg bg-accent p-2.5 text-accent-foreground">
          <Icon aria-hidden className="size-5" />
        </div>
        <div className="min-w-0">
          <p className="text-sm text-muted-foreground">{label}</p>
          <p className="tabular text-2xl font-semibold tracking-tight" data-testid={testId}>
            {value}
          </p>
          {hint ? <p className="mt-0.5 text-xs text-muted-foreground">{hint}</p> : null}
        </div>
      </CardContent>
    </Card>
  );
}

function SystemStatus() {
  const t = useTranslations("cockpit");
  const health = useApiQuery("/health", healthSchema, { refetchInterval: 30_000 });
  const ready = useApiQuery("/ready", readinessSchema, { refetchInterval: 30_000, allowStatus: 503 });
  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <HeartPulse aria-hidden className="size-4 text-primary" />
          {t("systemStatus")}
        </CardTitle>
        <CardDescription>{t("systemStatusHint")}</CardDescription>
      </CardHeader>
      <CardContent className="space-y-3">
        {ready.isPending || health.isPending ? (
          <LoadingRows label={t("loading")} rows={3} />
        ) : ready.isError ? (
          <ApiErrorAlert error={ready.error} />
        ) : (
          <>
            <div className="flex flex-wrap items-center gap-2 text-sm">
              <span className="text-muted-foreground">{t("liveness")}</span>
              <StatusBadge value={health.data?.status === "ok" ? "up" : "down"} testId="health-status" />
              <span className="ml-3 text-muted-foreground">{t("readiness")}</span>
              <StatusBadge value={ready.data.status} testId="readiness-status" />
            </div>
            <ul className="grid gap-2 sm:grid-cols-2">
              {Object.entries(ready.data.components).map(([name, component]) => (
                <li key={name} className="flex items-center justify-between rounded-md border px-3 py-2 text-sm">
                  <span className="font-medium">{t.has(`components.${name}` as never) ? t(`components.${name}` as never) : name}</span>
                  <span className="flex items-center gap-2">
                    {component.critical ? <span className="text-xs text-muted-foreground">{t("critical")}</span> : null}
                    <StatusBadge value={component.status} />
                  </span>
                </li>
              ))}
            </ul>
          </>
        )}
      </CardContent>
    </Card>
  );
}

function Processing() {
  const t = useTranslations("cockpit");
  const tStates = useTranslations("states");
  const locale = useLocale();
  const jobs = useApiQuery("/v1/ingestion/jobs", z.array(jobSchema), { refetchInterval: 20_000 });
  if (jobs.isPending) return <Skeleton className="h-72 w-full" />;
  if (jobs.isError) return <ApiErrorAlert error={jobs.error} />;
  const counts = new Map<string, number>();
  for (const job of jobs.data) counts.set(job.status, (counts.get(job.status) ?? 0) + 1);
  const chart = [...counts.entries()].map(([status, count]) => ({
    status: tStates.has(status as never) ? tStates(status as never) : status,
    count,
  }));
  const review = jobs.data.filter((job) => job.review_required).length;
  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <FileStack aria-hidden className="size-4 text-primary" />
          {t("processing")}
        </CardTitle>
        <CardDescription>{t("processingHint", { count: jobs.data.length })}</CardDescription>
      </CardHeader>
      <CardContent>
        {jobs.data.length === 0 ? (
          <EmptyState icon={FileStack} title={t("noJobs")} description={t("noJobsHint")} />
        ) : (
          <div className="grid grid-cols-1 gap-4 md:grid-cols-[1fr_200px]">
            <figure aria-label={t("jobsChartLabel")} className="h-56">
              <ResponsiveContainer width="100%" height="100%">
                <BarChart data={chart} margin={{ top: 8, right: 8, left: -16, bottom: 0 }}>
                  <CartesianGrid strokeDasharray="3 3" vertical={false} stroke="hsl(var(--border))" />
                  <XAxis dataKey="status" tick={{ fontSize: 12 }} />
                  <YAxis allowDecimals={false} tick={{ fontSize: 12 }} />
                  <ChartTooltip cursor={{ fill: "hsl(var(--muted))" }} />
                  <Bar dataKey="count" name={t("jobs")} fill="hsl(var(--brand-500))" radius={[4, 4, 0, 0]} />
                </BarChart>
              </ResponsiveContainer>
            </figure>
            <dl className="grid grid-cols-2 content-start gap-3 md:grid-cols-1">
              {chart.map((row) => (
                <div key={row.status} className="rounded-md border px-3 py-2">
                  <dt className="text-xs text-muted-foreground">{row.status}</dt>
                  <dd className="tabular text-lg font-semibold">{formatNumber(row.count, locale)}</dd>
                </div>
              ))}
              <div className="rounded-md border border-warning-foreground/20 bg-warning px-3 py-2 text-warning-foreground">
                <dt className="text-xs">{t("ocrReview")}</dt>
                <dd className="tabular text-lg font-semibold">{formatNumber(review, locale)}</dd>
              </div>
            </dl>
          </div>
        )}
      </CardContent>
    </Card>
  );
}

function ReviewQueue() {
  const t = useTranslations("cockpit");
  const locale = useLocale();
  const findings = useApiQuery("/v1/intelligence/findings", z.array(findingSchema), { refetchInterval: 60_000 });
  const documents = useApiQuery("/v1/intelligence/documents?review_required=true", z.array(intelligenceDocumentSchema));
  const value = findings.isPending ? "…" : findings.isError ? "—" : formatNumber(findings.data.length, locale);
  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <ClipboardCheck aria-hidden className="size-4 text-primary" />
          {t("reviewQueue")}
        </CardTitle>
        <CardDescription>{t("reviewQueueHint")}</CardDescription>
      </CardHeader>
      <CardContent className="space-y-3">
        {findings.isError ? <ApiErrorAlert error={findings.error} /> : null}
        <div className="flex items-baseline gap-2">
          <span className="tabular text-3xl font-semibold" data-testid="open-findings">
            {value}
          </span>
          <span className="text-sm text-muted-foreground">{t("openFindings")}</span>
        </div>
        <p className="text-sm text-muted-foreground">
          {documents.isSuccess ? t("documentsNeedingReview", { count: documents.data.length }) : " "}
        </p>
        <Link href="/intelligence?tab=findings" className="text-sm font-medium text-primary underline-offset-4 hover:underline">
          {t("openReviewQueue")}
        </Link>
      </CardContent>
    </Card>
  );
}

function UsageAndCost() {
  const t = useTranslations("cockpit");
  const locale = useLocale();
  const { roles } = useSession();
  const costs = useApiQuery(isAdmin(roles) ? "/v1/costs/summary" : null, costSummarySchema);
  if (!isAdmin(roles)) {
    return (
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <Coins aria-hidden className="size-4 text-primary" />
            {t("usage")}
          </CardTitle>
        </CardHeader>
        <CardContent>
          <Alert tone="info">{t("adminOnly")}</Alert>
        </CardContent>
      </Card>
    );
  }
  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <Coins aria-hidden className="size-4 text-primary" />
          {t("usage")}
        </CardTitle>
        <CardDescription>{t("usageScope")}</CardDescription>
      </CardHeader>
      <CardContent>
        {costs.isPending ? (
          <LoadingRows label={t("loading")} rows={2} />
        ) : costs.isError ? (
          <ApiErrorAlert error={costs.error} />
        ) : (
          <dl className="grid grid-cols-3 gap-3 text-sm">
            <div>
              <dt className="text-muted-foreground">{t("requests")}</dt>
              <dd className="tabular text-lg font-semibold" data-testid="cost-requests">{formatNumber(costs.data.request_count, locale)}</dd>
            </div>
            <div>
              <dt className="text-muted-foreground">{t("tokens")}</dt>
              <dd className="tabular text-lg font-semibold">{formatNumber(costs.data.total_tokens, locale)}</dd>
            </div>
            <div>
              <dt className="text-muted-foreground">{t("estimatedCost")}</dt>
              <dd className="tabular text-lg font-semibold">{formatEuroFloat(costs.data.total_cost_eur, locale)}</dd>
            </div>
          </dl>
        )}
      </CardContent>
    </Card>
  );
}

function RecentActivity() {
  const t = useTranslations("cockpit");
  const locale = useLocale();
  const { roles } = useSession();
  const admin = isAdmin(roles);
  const audit = useApiQuery(admin ? "/v1/audit-events" : null, z.array(auditEventSchema));
  const jobs = useApiQuery("/v1/ingestion/jobs", z.array(jobSchema));
  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <Activity aria-hidden className="size-4 text-primary" />
          {t("recentActivity")}
        </CardTitle>
        <CardDescription>{admin ? t("recentActivityAudit") : t("recentActivityJobs")}</CardDescription>
      </CardHeader>
      <CardContent>
        {admin ? (
          audit.isPending ? (
            <LoadingRows label={t("loading")} />
          ) : audit.isError ? (
            <ApiErrorAlert error={audit.error} />
          ) : audit.data.length === 0 ? (
            <EmptyState icon={ListChecks} title={t("noActivity")} />
          ) : (
            <ul className="divide-y text-sm" data-testid="recent-activity">
              {audit.data.slice(-6).reverse().map((event) => (
                <li key={event.event_id} className="flex flex-wrap items-center justify-between gap-2 py-2">
                  <span className="font-mono text-xs">{event.event_type}</span>
                  <span className="flex items-center gap-2">
                    <StatusBadge value={event.outcome === "success" ? "completed" : event.outcome === "failed" ? "failed" : event.outcome} />
                    <time className="text-xs text-muted-foreground" dateTime={event.timestamp}>{formatDateTime(event.timestamp, locale)}</time>
                  </span>
                </li>
              ))}
            </ul>
          )
        ) : jobs.isPending ? (
          <LoadingRows label={t("loading")} />
        ) : jobs.isError ? (
          <ApiErrorAlert error={jobs.error} />
        ) : jobs.data.length === 0 ? (
          <EmptyState icon={ListChecks} title={t("noActivity")} />
        ) : (
          <ul className="divide-y text-sm" data-testid="recent-activity">
            {jobs.data.slice(0, 6).map((job) => (
              <li key={job.job_id} className="flex items-center justify-between gap-2 py-2">
                <span className="truncate">{job.title ?? job.filename ?? job.job_id}</span>
                <StatusBadge value={job.status} />
              </li>
            ))}
          </ul>
        )}
      </CardContent>
    </Card>
  );
}

export function Cockpit() {
  const t = useTranslations("cockpit");
  const session = useSession();
  const locale = useLocale();
  const jobs = useApiQuery("/v1/ingestion/jobs", z.array(jobSchema), { refetchInterval: 20_000 });
  const findings = useApiQuery("/v1/intelligence/findings", z.array(findingSchema));
  const failed = jobs.data?.filter((job) => job.status === "failed").length;
  const active = jobs.data?.filter((job) => ["queued", "running", "processing", "retrying"].includes(job.status)).length;
  return (
    <>
      <PageHeader title={t("title")} description={t("description", { tenant: session.tenantId })} />
      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 xl:grid-cols-4">
        <Metric icon={FileStack} label={t("jobsTotal")} value={jobs.data ? formatNumber(jobs.data.length, locale) : "…"} testId="metric-jobs" />
        <Metric icon={Gauge} label={t("jobsActive")} value={active === undefined ? "…" : formatNumber(active, locale)} />
        <Metric icon={AlertTriangle} label={t("jobsFailed")} value={failed === undefined ? "…" : formatNumber(failed, locale)} testId="metric-failed" />
        <Metric icon={ClipboardCheck} label={t("findings")} value={findings.data ? formatNumber(findings.data.length, locale) : findings.isError ? "—" : "…"} />
      </div>
      <div className="mt-4 grid grid-cols-1 gap-4 xl:grid-cols-3">
        <div className="space-y-4 xl:col-span-2">
          <Processing />
          <SystemStatus />
        </div>
        <div className="space-y-4">
          <ReviewQueue />
          <UsageAndCost />
          <RecentActivity />
        </div>
      </div>
    </>
  );
}
