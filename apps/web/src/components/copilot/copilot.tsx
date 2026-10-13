"use client";

import { Bot, Loader2, RotateCcw, Send, UserRound } from "lucide-react";
import { useTranslations } from "next-intl";
import * as React from "react";
import { ApiErrorAlert } from "@/components/common/api-error";
import { PageHeader } from "@/components/common/page-header";
import { useSession } from "@/components/providers/session-context";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Alert, EmptyState } from "@/components/ui/feedback";
import { Textarea } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { NativeSelect } from "@/components/ui/native-select";
import { ApiError, api, newIdempotencyKey } from "@/lib/api/client";
import { queryRequestSchema, queryResponseSchema, type QueryResponse } from "@/lib/api/schemas";
import { AnswerView, RefusalView } from "./answer";

interface Turn {
  id: string;
  question: string;
  topK: number;
  /** One key per logical question: a retry of the same turn reuses it (same answer, no double billing). */
  idempotencyKey: string;
  state: "pending" | "done" | "error";
  result?: QueryResponse;
  replayed?: boolean;
  error?: unknown;
}

export type RefusalCode =
  | "forbidden"
  | "rate_limited"
  | "budget_exceeded"
  | "key_conflict"
  | "in_progress"
  | "reconciliation"
  | "no_model"
  | "not_configured";

/** Controlled refusals the API states explicitly; anything else is shown as a generic error. */
export function refusal(error: unknown): RefusalCode | null {
  if (!(error instanceof ApiError)) return null;
  const detail = error.detail ?? "";
  if (error.status === 403) return "forbidden";
  if (error.status === 429) return detail.includes("budget") ? "budget_exceeded" : "rate_limited";
  if (error.status === 409) {
    if (detail.includes("in progress")) return "in_progress";
    if (detail.includes("different request")) return "key_conflict";
    if (detail.includes("reconciliation")) return "reconciliation";
    if (detail.includes("no eligible model")) return "no_model";
  }
  if (error.status === 503 && detail.includes("accounting not configured")) return "not_configured";
  return null;
}

/** Resending the same request (same key) only helps for transient states. */
const RETRYABLE: ReadonlySet<RefusalCode | null> = new Set<RefusalCode | null>(["rate_limited", "in_progress", null]);

export function Copilot() {
  const t = useTranslations("copilot");
  const { csrfToken } = useSession();
  const [turns, setTurns] = React.useState<Turn[]>([]);
  const [question, setQuestion] = React.useState("");
  const [topK, setTopK] = React.useState(3);
  const [validation, setValidation] = React.useState<string | null>(null);
  const endRef = React.useRef<HTMLDivElement>(null);
  const busy = turns.some((turn) => turn.state === "pending");

  React.useEffect(() => {
    // Block body on purpose: newer browsers return a Promise from scrollIntoView, and an
    // effect must never return anything but a cleanup function.
    endRef.current?.scrollIntoView({ block: "end", behavior: "smooth" });
  }, [turns]);

  async function run(turn: Turn) {
    setTurns((all) => all.map((item) => (item.id === turn.id ? { ...turn, state: "pending", error: undefined } : item)));
    try {
      const { data, replayed } = await api.post("/v1/query", { question: turn.question, top_k: turn.topK }, queryResponseSchema, {
        csrfToken,
        idempotencyKey: turn.idempotencyKey,
      });
      setTurns((all) => all.map((item) => (item.id === turn.id ? { ...item, state: "done", result: data, replayed } : item)));
    } catch (error) {
      setTurns((all) => all.map((item) => (item.id === turn.id ? { ...item, state: "error", error } : item)));
    }
  }

  function submit(event: React.FormEvent) {
    event.preventDefault();
    const parsed = queryRequestSchema.safeParse({ question, top_k: topK });
    if (!parsed.success) {
      setValidation(question.trim() ? t("validation.tooLong") : t("validation.required"));
      return;
    }
    setValidation(null);
    const turn: Turn = { id: crypto.randomUUID(), question: parsed.data.question, topK, idempotencyKey: newIdempotencyKey(), state: "pending" };
    setTurns((all) => [...all, turn]);
    setQuestion("");
    void run(turn);
  }

  return (
    <>
      <PageHeader title={t("title")} description={t("description")} />
      <Alert tone="info" className="mb-4">
        {t("languageNotice")}
      </Alert>
      <Card className="flex min-h-[60vh] flex-col">
        <CardContent className="flex-1 space-y-5 overflow-y-auto p-4 sm:p-6" aria-live="polite" aria-busy={busy} data-testid="conversation">
          {turns.length === 0 ? (
            <EmptyState icon={Bot} title={t("emptyTitle")} description={t("emptyBody")} />
          ) : (
            turns.map((turn) => (
              <article key={turn.id} className="space-y-3" data-testid="turn">
                <div className="flex justify-end">
                  <div className="flex max-w-[85%] items-start gap-2 rounded-lg bg-primary px-3 py-2 text-sm text-primary-foreground">
                    <UserRound aria-hidden className="mt-0.5 size-4 shrink-0" />
                    <p className="whitespace-pre-wrap break-words">
                      <span className="sr-only">{t("you")}: </span>
                      {turn.question}
                    </p>
                  </div>
                </div>
                <div className="flex items-start gap-2">
                  <div className="rounded-full bg-accent p-1.5 text-accent-foreground">
                    <Bot aria-hidden className="size-4" />
                  </div>
                  <div className="min-w-0 flex-1 rounded-lg border bg-card p-3 sm:p-4">
                    <span className="sr-only">{t("assistant")}: </span>
                    {turn.state === "pending" ? (
                      <p className="flex items-center gap-2 text-sm text-muted-foreground" role="status" data-testid="answer-pending">
                        <Loader2 aria-hidden className="size-4 animate-spin" />
                        {t("thinking")}
                      </p>
                    ) : turn.state === "done" && turn.result ? (
                      <AnswerView result={turn.result} replayed={Boolean(turn.replayed)} />
                    ) : (
                      <div className="space-y-2" data-testid={refusal(turn.error) ? undefined : "answer-error"}>
                        {refusal(turn.error) ? <RefusalView code={refusal(turn.error)!} /> : <ApiErrorAlert error={turn.error} />}
                        {RETRYABLE.has(refusal(turn.error)) ? (
                          <Button variant="outline" size="sm" onClick={() => void run(turn)} data-testid="retry-query">
                            <RotateCcw aria-hidden />
                            {t("retrySame")}
                          </Button>
                        ) : null}
                      </div>
                    )}
                  </div>
                </div>
              </article>
            ))
          )}
          <div ref={endRef} />
        </CardContent>
        <form onSubmit={submit} className="space-y-3 border-t p-4 sm:p-5" noValidate>
          <div className="space-y-1.5">
            <Label htmlFor="question">{t("questionLabel")}</Label>
            <Textarea
              id="question"
              value={question}
              onChange={(event) => setQuestion(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === "Enter" && !event.shiftKey) {
                  event.preventDefault();
                  event.currentTarget.form?.requestSubmit();
                }
              }}
              maxLength={4000}
              placeholder={t("questionPlaceholder")}
              aria-invalid={validation ? true : undefined}
              aria-describedby={validation ? "question-error" : "question-hint"}
              data-testid="question"
            />
            {validation ? (
              <p id="question-error" className="text-sm text-destructive" role="alert">
                {validation}
              </p>
            ) : (
              <p id="question-hint" className="text-xs text-muted-foreground">
                {t("questionHint")}
              </p>
            )}
          </div>
          <div className="flex flex-wrap items-end justify-between gap-3">
            <div className="w-40 space-y-1.5">
              <Label htmlFor="top-k">{t("topK")}</Label>
              <NativeSelect id="top-k" data-testid="top-k" value={topK} onChange={(event) => setTopK(Number(event.target.value))}>
                {[1, 3, 5, 8].map((value) => (
                  <option key={value} value={value}>
                    {value}
                  </option>
                ))}
              </NativeSelect>
            </div>
            <Button type="submit" disabled={busy} data-testid="ask">
              {busy ? <Loader2 aria-hidden className="animate-spin" /> : <Send aria-hidden />}
              {t("ask")}
            </Button>
          </div>
        </form>
      </Card>
    </>
  );
}
