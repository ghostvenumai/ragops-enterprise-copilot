"use client";

import { useQuery } from "@tanstack/react-query";
import { Gauge, LineChart as LineIcon, Target } from "lucide-react";
import { useLocale, useTranslations } from "next-intl";
import * as React from "react";
import { Line, LineChart, ResponsiveContainer, Tooltip as ChartTooltip, XAxis, YAxis, CartesianGrid } from "recharts";
import { ApiErrorAlert } from "@/components/common/api-error";
import { PageHeader } from "@/components/common/page-header";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Alert, LoadingRows } from "@/components/ui/feedback";
import { api } from "@/lib/api/client";
import { useApiQuery } from "@/lib/api/hooks";
import { evaluationSchema } from "@/lib/api/schemas";
import { formatNumber, formatPercent } from "@/lib/format";
import { parseMetrics } from "@/lib/metrics";
import { ReadinessCard } from "./readiness-card";

const SHOWN = [
  "ragops_request_count",
  "ragops_success_count",
  "ragops_failure_count",
  "ragops_avg_latency_ms",
  "ragops_avg_retrieval_latency_ms",
  "ragops_total_tokens",
  "ragops_ingestion_jobs_total",
  "ragops_ingestion_queue_depth",
  "ragops_ingestion_queue_up",
];

const EVALUATION_RATES = ["retrieval_hit_rate", "precision_at_5", "recall_at_5", "citation_coverage", "abstention_rate", "security_test_pass_rate"];

function Metrics() {
  const t = useTranslations("monitoring.metrics");
  const locale = useLocale();
  const metrics = useQuery({
    queryKey: ["api", "/metrics"],
    queryFn: ({ signal }) => api.text("/metrics", { signal }).then(parseMetrics),
    refetchInterval: 15_000,
  });
  // A short in-browser history of samples taken while this page is open; nothing is invented.
  const [history, setHistory] = React.useState<{ at: string; requests: number; queue: number }[]>([]);
  React.useEffect(() => {
    if (!metrics.data) return;
    const sample = {
      at: new Intl.DateTimeFormat(locale, { hour: "2-digit", minute: "2-digit", second: "2-digit" }).format(new Date()),
      requests: metrics.data.ragops_request_count ?? 0,
      queue: Math.max(0, metrics.data.ragops_ingestion_queue_depth ?? 0),
    };
    setHistory((all) => [...all.slice(-29), sample]);
  }, [metrics.data, locale]);
  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <Gauge aria-hidden className="size-4 text-primary" />
          {t("title")}
        </CardTitle>
        <CardDescription>{t("hint")}</CardDescription>
      </CardHeader>
      <CardContent className="space-y-4">
        {metrics.isPending ? (
          <LoadingRows label={t("loading")} />
        ) : metrics.isError ? (
          <ApiErrorAlert error={metrics.error} />
        ) : (
          <>
            <dl className="grid grid-cols-2 gap-3 sm:grid-cols-3" data-testid="metrics">
              {SHOWN.filter((name) => name in metrics.data).map((name) => (
                <div key={name} className="rounded-md border px-3 py-2">
                  <dt className="text-xs text-muted-foreground">{t.has(`names.${name}` as never) ? t(`names.${name}` as never) : name}</dt>
                  <dd className="tabular text-lg font-semibold">
                    {name === "ragops_ingestion_queue_up"
                      ? metrics.data[name] === 1
                        ? t("up")
                        : t("down")
                      : name === "ragops_ingestion_queue_depth" && (metrics.data[name] ?? 0) < 0
                        ? t("unknown")
                        : formatNumber(metrics.data[name] ?? 0, locale, 1)}
                  </dd>
                </div>
              ))}
            </dl>
            <figure aria-label={t("chartLabel")} className="h-48">
              <figcaption className="mb-1 flex items-center gap-1.5 text-xs text-muted-foreground">
                <LineIcon aria-hidden className="size-3.5" />
                {t("chartLabel")}
              </figcaption>
              <ResponsiveContainer width="100%" height="100%">
                <LineChart data={history} margin={{ top: 4, right: 8, left: -16, bottom: 0 }}>
                  <CartesianGrid strokeDasharray="3 3" stroke="hsl(var(--border))" />
                  <XAxis dataKey="at" tick={{ fontSize: 11 }} minTickGap={24} />
                  <YAxis allowDecimals={false} tick={{ fontSize: 11 }} />
                  <ChartTooltip />
                  <Line type="monotone" dataKey="requests" name={t("names.ragops_request_count")} stroke="hsl(var(--brand-600))" dot={false} isAnimationActive={false} />
                  <Line type="monotone" dataKey="queue" name={t("names.ragops_ingestion_queue_depth")} stroke="hsl(var(--info-foreground))" dot={false} isAnimationActive={false} />
                </LineChart>
              </ResponsiveContainer>
            </figure>
          </>
        )}
      </CardContent>
    </Card>
  );
}

function Evaluation() {
  const t = useTranslations("monitoring.evaluation");
  const locale = useLocale();
  const evaluation = useApiQuery("/v1/evaluations", evaluationSchema);
  const data = evaluation.data as Record<string, unknown> | undefined;
  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <Target aria-hidden className="size-4 text-primary" />
          {t("title")}
        </CardTitle>
        <CardDescription>{t("hint")}</CardDescription>
      </CardHeader>
      <CardContent>
        {evaluation.isPending ? (
          <LoadingRows label={t("loading")} />
        ) : evaluation.isError ? (
          <ApiErrorAlert error={evaluation.error} />
        ) : data?.status === "not_run" ? (
          <Alert tone="info">{t("notRun")}</Alert>
        ) : (
          <dl className="grid grid-cols-2 gap-3 sm:grid-cols-3" data-testid="evaluation">
            {EVALUATION_RATES.filter((key) => typeof data?.[key] === "number").map((key) => (
              <div key={key} className="rounded-md border px-3 py-2">
                <dt className="text-xs text-muted-foreground">{t(`fields.${key}` as never)}</dt>
                <dd className="tabular text-lg font-semibold">{formatPercent(data?.[key] as number, locale)}</dd>
              </div>
            ))}
            {typeof data?.case_count === "number" ? (
              <div className="rounded-md border px-3 py-2">
                <dt className="text-xs text-muted-foreground">{t("fields.case_count")}</dt>
                <dd className="tabular text-lg font-semibold">{formatNumber(data.case_count, locale)}</dd>
              </div>
            ) : null}
            {typeof data?.tenant_leakage_count === "number" ? (
              <div className="rounded-md border px-3 py-2">
                <dt className="text-xs text-muted-foreground">{t("fields.tenant_leakage_count")}</dt>
                <dd className="tabular text-lg font-semibold">{formatNumber(data.tenant_leakage_count, locale)}</dd>
              </div>
            ) : null}
          </dl>
        )}
      </CardContent>
    </Card>
  );
}

export function Monitoring() {
  const t = useTranslations("monitoring");
  return (
    <>
      <PageHeader title={t("title")} description={t("description")} />
      <div className="grid grid-cols-1 items-start gap-4 xl:grid-cols-2">
        <Metrics />
        <ReadinessCard />
        <Evaluation />
      </div>
    </>
  );
}
