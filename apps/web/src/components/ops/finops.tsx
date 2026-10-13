"use client";

import { useQueryClient } from "@tanstack/react-query";
import { Calculator, Coins, Plus, Route, Wallet } from "lucide-react";
import { useLocale, useTranslations } from "next-intl";
import * as React from "react";
import { toast } from "sonner";
import { z } from "zod";
import { ApiErrorAlert } from "@/components/common/api-error";
import { PageHeader } from "@/components/common/page-header";
import { useSession } from "@/components/providers/session-context";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Alert, EmptyState, LoadingRows } from "@/components/ui/feedback";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { NativeSelect } from "@/components/ui/native-select";
import { TBody, TD, TH, THead, TR, Table } from "@/components/ui/table";
import { api } from "@/lib/api/client";
import { useApiQuery } from "@/lib/api/hooks";
import { budgetCreateSchema, budgetSchema, costSummarySchema, policySimulationSchema, routeSimulationSchema } from "@/lib/api/schemas";
import { formatDecimal, formatEuroFloat, formatNumber } from "@/lib/format";

function ProcessCounters() {
  const t = useTranslations("finops.counters");
  const locale = useLocale();
  const costs = useApiQuery("/v1/costs/summary", costSummarySchema, { refetchInterval: 30_000 });
  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <Coins aria-hidden className="size-4 text-primary" />
          {t("title")}
        </CardTitle>
        <CardDescription>{t("scope")}</CardDescription>
      </CardHeader>
      <CardContent>
        {costs.isPending ? (
          <LoadingRows label={t("loading")} rows={2} />
        ) : costs.isError ? (
          <ApiErrorAlert error={costs.error} />
        ) : (
          <dl className="grid grid-cols-3 gap-3">
            <div>
              <dt className="text-xs text-muted-foreground">{t("requests")}</dt>
              <dd className="tabular text-xl font-semibold">{formatNumber(costs.data.request_count, locale)}</dd>
            </div>
            <div>
              <dt className="text-xs text-muted-foreground">{t("tokens")}</dt>
              <dd className="tabular text-xl font-semibold">{formatNumber(costs.data.total_tokens, locale)}</dd>
            </div>
            <div>
              <dt className="text-xs text-muted-foreground">{t("cost")}</dt>
              <dd className="tabular text-xl font-semibold" data-testid="process-cost">{formatEuroFloat(costs.data.total_cost_eur, locale)}</dd>
            </div>
          </dl>
        )}
      </CardContent>
    </Card>
  );
}

function Budgets() {
  const t = useTranslations("finops.budgets");
  const modes = useTranslations("finops.modes");
  const locale = useLocale();
  const { csrfToken } = useSession();
  const queryClient = useQueryClient();
  const budgets = useApiQuery("/v1/admin/budgets", z.array(budgetSchema));
  const [amount, setAmount] = React.useState("");
  const [mode, setMode] = React.useState<"monitor" | "optimize" | "hard_limit">("optimize");
  const [invalid, setInvalid] = React.useState(false);
  const [error, setError] = React.useState<unknown>(null);

  async function create(event: React.FormEvent) {
    event.preventDefault();
    const parsed = budgetCreateSchema.safeParse({ budget_amount: Number(amount.replace(",", ".")), currency: "EUR", enforcement_mode: mode });
    setInvalid(!parsed.success);
    if (!parsed.success) return;
    setError(null);
    try {
      await api.post("/v1/admin/budgets", parsed.data, budgetSchema, { csrfToken });
      toast.success(t("created"));
      setAmount("");
      await queryClient.invalidateQueries({ queryKey: ["api", "/v1/admin/budgets"] });
    } catch (failure) {
      setError(failure);
    }
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <Wallet aria-hidden className="size-4 text-primary" />
          {t("title")}
        </CardTitle>
        <CardDescription>{t("hint")}</CardDescription>
      </CardHeader>
      <CardContent className="space-y-4">
        {budgets.isPending ? (
          <LoadingRows label={t("loading")} />
        ) : budgets.isError ? (
          <ApiErrorAlert error={budgets.error} />
        ) : budgets.data.length === 0 ? (
          <EmptyState icon={Wallet} title={t("empty")} />
        ) : (
          <Table caption={t("title")}>
            <THead>
              <TR>
                <TH>{t("id")}</TH>
                <TH className="text-right">{t("amount")}</TH>
                <TH>{t("mode")}</TH>
              </TR>
            </THead>
            <TBody>
              {budgets.data.map((budget) => (
                <TR key={budget.id}>
                  <TD className="font-mono text-xs">{budget.id}</TD>
                  <TD className="tabular text-right">{formatEuroFloat(budget.budget_amount, locale)}</TD>
                  <TD>
                    <Badge tone="neutral">{modes.has(budget.enforcement_mode as never) ? modes(budget.enforcement_mode as never) : budget.enforcement_mode}</Badge>
                  </TD>
                </TR>
              ))}
            </TBody>
          </Table>
        )}
        <form onSubmit={create} className="grid gap-3 border-t pt-4 sm:grid-cols-[1fr_1fr_auto] sm:items-end" noValidate>
          <div className="space-y-1.5">
            <Label htmlFor="budget-amount">{t("amountEur")}</Label>
            <Input id="budget-amount" inputMode="decimal" value={amount} onChange={(event) => setAmount(event.target.value)} aria-invalid={invalid || undefined} />
          </div>
          <div className="space-y-1.5">
            <Label htmlFor="budget-mode">{t("mode")}</Label>
            <NativeSelect id="budget-mode" value={mode} onChange={(event) => setMode(event.target.value as typeof mode)}>
              {(["monitor", "optimize", "hard_limit"] as const).map((value) => (
                <option key={value} value={value}>
                  {modes(value)}
                </option>
              ))}
            </NativeSelect>
          </div>
          <Button type="submit">
            <Plus aria-hidden />
            {t("create")}
          </Button>
          {invalid ? <p className="text-sm text-destructive sm:col-span-3">{t("invalid")}</p> : null}
          {error ? <ApiErrorAlert error={error} className="sm:col-span-3" /> : null}
        </form>
        <Alert tone="info">{t("volatileNotice")}</Alert>
      </CardContent>
    </Card>
  );
}

function PolicySimulation() {
  const t = useTranslations("finops.simulation");
  const locale = useLocale();
  const { csrfToken } = useSession();
  const [estimated, setEstimated] = React.useState("0.50");
  const [budget, setBudget] = React.useState("10.00");
  const [result, setResult] = React.useState<z.infer<typeof policySimulationSchema> | null>(null);
  const [error, setError] = React.useState<unknown>(null);
  async function run(event: React.FormEvent) {
    event.preventDefault();
    setError(null);
    try {
      const { data } = await api.post(
        "/v1/admin/finops/policy/simulate",
        { estimated_cost: estimated.replace(",", "."), budget_amount: budget.replace(",", ".") },
        policySimulationSchema,
        { csrfToken },
      );
      setResult(data);
    } catch (failure) {
      setResult(null);
      setError(failure);
    }
  }
  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <Calculator aria-hidden className="size-4 text-primary" />
          {t("title")}
        </CardTitle>
        <CardDescription>{t("hint")}</CardDescription>
      </CardHeader>
      <CardContent className="space-y-3">
        <form onSubmit={run} className="grid gap-3 sm:grid-cols-[1fr_1fr_auto] sm:items-end" noValidate>
          <div className="space-y-1.5">
            <Label htmlFor="sim-estimated">{t("estimated")}</Label>
            <Input id="sim-estimated" inputMode="decimal" value={estimated} onChange={(event) => setEstimated(event.target.value)} />
          </div>
          <div className="space-y-1.5">
            <Label htmlFor="sim-budget">{t("budget")}</Label>
            <Input id="sim-budget" inputMode="decimal" value={budget} onChange={(event) => setBudget(event.target.value)} />
          </div>
          <Button type="submit" variant="secondary">
            {t("run")}
          </Button>
        </form>
        {error ? <ApiErrorAlert error={error} /> : null}
        {result ? (
          <div className="rounded-md border bg-muted/40 p-3 text-sm" aria-live="polite" data-testid="policy-result">
            <Badge tone="info">{t("simulationOnly")}</Badge>
            <p className="mt-2">
              {t("decision")}: <strong>{result.decision}</strong> · {t("state")}: <strong>{result.state}</strong>
            </p>
            <p>
              {t("projected")}: <span className="tabular">{formatDecimal(result.estimated_post_request_spend, locale)} EUR</span>
            </p>
            <p className="font-mono text-xs text-muted-foreground">{result.reason_codes.join(", ")}</p>
          </div>
        ) : null}
      </CardContent>
    </Card>
  );
}

function RouterSimulation() {
  const t = useTranslations("finops.router");
  const locale = useLocale();
  const { csrfToken } = useSession();
  const [intent, setIntent] = React.useState("factual");
  const [chunks, setChunks] = React.useState("5");
  const [highRisk, setHighRisk] = React.useState(false);
  const [result, setResult] = React.useState<z.infer<typeof routeSimulationSchema> | null>(null);
  const [error, setError] = React.useState<unknown>(null);
  async function run(event: React.FormEvent) {
    event.preventDefault();
    setError(null);
    try {
      const { data } = await api.post(
        "/v1/admin/model-router/simulate",
        { intent, retrieved_chunks: Number(chunks) || 0, source_count: Number(chunks) || 0, high_risk_flag: highRisk },
        routeSimulationSchema,
        { csrfToken },
      );
      setResult(data);
    } catch (failure) {
      setResult(null);
      setError(failure);
    }
  }
  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <Route aria-hidden className="size-4 text-primary" />
          {t("title")}
        </CardTitle>
        <CardDescription>{t("hint")}</CardDescription>
      </CardHeader>
      <CardContent className="space-y-3">
        <form onSubmit={run} className="grid gap-3 sm:grid-cols-2" noValidate>
          <div className="space-y-1.5">
            <Label htmlFor="route-intent">{t("intent")}</Label>
            <NativeSelect id="route-intent" value={intent} onChange={(event) => setIntent(event.target.value)}>
              {["factual", "compliance", "legal", "security", "critical_operations"].map((value) => (
                <option key={value} value={value}>
                  {t(`intents.${value}` as never)}
                </option>
              ))}
            </NativeSelect>
          </div>
          <div className="space-y-1.5">
            <Label htmlFor="route-chunks">{t("chunks")}</Label>
            <Input id="route-chunks" type="number" min={0} max={50} value={chunks} onChange={(event) => setChunks(event.target.value)} />
          </div>
          <label className="flex items-center gap-2 text-sm">
            <input type="checkbox" checked={highRisk} onChange={(event) => setHighRisk(event.target.checked)} className="size-4 accent-[hsl(var(--primary))]" />
            {t("highRisk")}
          </label>
          <div className="sm:text-right">
            <Button type="submit" variant="secondary">
              {t("run")}
            </Button>
          </div>
        </form>
        {error ? <ApiErrorAlert error={error} /> : null}
        {result ? (
          <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1 rounded-md border bg-muted/40 p-3 text-sm" aria-live="polite" data-testid="route-result">
            <dt className="text-muted-foreground">{t("class")}</dt>
            <dd>{result.routing_class}</dd>
            <dt className="text-muted-foreground">{t("model")}</dt>
            <dd className="font-mono text-xs">
              {result.provider_id} / {result.model_id}
            </dd>
            <dt className="text-muted-foreground">{t("estimate")}</dt>
            <dd className="tabular">{formatEuroFloat(result.estimated_cost, locale)}</dd>
            <dt className="text-muted-foreground">{t("reasons")}</dt>
            <dd className="font-mono text-xs">{result.reason_codes.join(", ")}</dd>
          </dl>
        ) : null}
      </CardContent>
    </Card>
  );
}

export function FinOps() {
  const t = useTranslations("finops");
  return (
    <>
      <PageHeader title={t("title")} description={t("description")} />
      <Alert tone="warning" className="mb-4" title={t("tenantUsageGapTitle")}>
        {t("tenantUsageGapBody")}
      </Alert>
      <div className="grid grid-cols-1 items-start gap-4 xl:grid-cols-2">
        <ProcessCounters />
        <Budgets />
        <PolicySimulation />
        <RouterSimulation />
      </div>
    </>
  );
}
