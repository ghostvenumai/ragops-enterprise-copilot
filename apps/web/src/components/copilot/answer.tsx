"use client";

import { AlertTriangle, FileText, Info, Repeat2, ShieldAlert } from "lucide-react";
import { useLocale, useTranslations } from "next-intl";
import { Badge } from "@/components/ui/badge";
import { Alert } from "@/components/ui/feedback";
import type { QueryResponse } from "@/lib/api/schemas";
import { formatEuroFloat, formatMs, formatNumber, formatPercent } from "@/lib/format";
import { AnswerText } from "./answer-text";

const LOW_EVIDENCE = 0.5;

/** The answer is rendered as text only. Model output can never become markup or a link. */
export function AnswerView({ result, replayed }: { result: QueryResponse; replayed: boolean }) {
  const t = useTranslations("copilot");
  const locale = useLocale();
  const uncertain = !result.abstained && result.evidence_score < LOW_EVIDENCE;
  return (
    <div className="space-y-3" data-testid="answer">
      {result.abstained ? (
        <Alert tone="warning" title={t("abstainedTitle")} live>
          {t("abstainedBody")}
        </Alert>
      ) : null}
      {uncertain ? (
        <Alert tone="warning" title={t("lowEvidenceTitle")}>
          {t("lowEvidenceBody")}
        </Alert>
      ) : null}
      {result.metrics.prompt_injection_detected ? (
        <Alert tone="danger" title={t("injectionTitle")}>
          {t("injectionBody")}
        </Alert>
      ) : null}
      <div className="space-y-2 break-words text-[15px] leading-relaxed" data-testid="answer-text" lang="">
        <AnswerText text={result.answer} />
      </div>
      <div className="flex flex-wrap gap-2">
        <Badge tone={result.abstained ? "warning" : uncertain ? "warning" : "success"} data-testid="evidence-score">
          {t("evidence", { value: formatPercent(result.evidence_score, locale) })}
        </Badge>
        {replayed ? (
          <Badge tone="info" data-testid="replayed">
            <Repeat2 aria-hidden className="size-3" />
            {t("replayed")}
          </Badge>
        ) : null}
      </div>
      <section aria-label={t("sources")} className="rounded-md border bg-muted/40 p-3">
        <h3 className="mb-2 flex items-center gap-1.5 text-sm font-semibold">
          <FileText aria-hidden className="size-4 text-primary" />
          {t("sources")} ({result.citations.length})
        </h3>
        {result.citations.length === 0 ? (
          <p className="text-sm text-muted-foreground">{t("noSources")}</p>
        ) : (
          <ol className="space-y-1.5 text-sm" data-testid="citations">
            {result.citations.map((citation, index) => (
              <li key={`${citation.source_id}-${index}`} className="flex flex-wrap items-baseline gap-x-2">
                <span className="font-mono text-xs text-muted-foreground">[{index + 1}]</span>
                <span className="font-medium">{citation.title}</span>
                {citation.page_number != null ? (
                  <Badge tone="neutral" data-testid="citation-page">{t("page", { page: citation.page_number })}</Badge>
                ) : null}
                <span className="text-xs text-muted-foreground">
                  {t("relevance", { value: formatNumber(citation.score, locale, 3) })} · <span className="font-mono">{citation.source_id}</span>
                </span>
              </li>
            ))}
          </ol>
        )}
      </section>
      <details className="text-xs text-muted-foreground">
        <summary className="cursor-pointer select-none font-medium text-foreground/80">
          <Info aria-hidden className="mr-1 inline size-3.5" />
          {t("metrics")}
        </summary>
        <dl className="mt-2 grid grid-cols-2 gap-x-4 gap-y-1 sm:grid-cols-3" data-testid="query-metrics">
          <dt>{t("tokens")}</dt>
          <dd className="tabular">{formatNumber(result.metrics.tokens, locale)}</dd>
          <dt>{t("cost")}</dt>
          <dd className="tabular">{formatEuroFloat(result.metrics.estimated_cost_eur, locale)}</dd>
          <dt>{t("retrievalLatency")}</dt>
          <dd className="tabular">{formatMs(result.metrics.retrieval_latency_ms, locale)}</dd>
          <dt>{t("llmLatency")}</dt>
          <dd className="tabular">{formatMs(result.metrics.llm_latency_ms, locale)}</dd>
          <dt>{t("correlation")}</dt>
          <dd className="font-mono">{result.correlation_id}</dd>
        </dl>
      </details>
    </div>
  );
}

export function RefusalView({ code }: { code: "forbidden" | "rate_limited" | "budget_exceeded" | "key_conflict" | "in_progress" | "reconciliation" | "no_model" | "not_configured" }) {
  const t = useTranslations("copilot.refusals");
  const Icon = code === "forbidden" ? ShieldAlert : AlertTriangle;
  // A refusal is an outcome, not a crash: it is announced politely with its reason.
  return (
    <div className="flex gap-2 text-sm" data-testid={`refusal-${code}`}>
      <Icon aria-hidden className="mt-0.5 size-4 shrink-0 text-warning-foreground" />
      <span>{t(code)}</span>
    </div>
  );
}
