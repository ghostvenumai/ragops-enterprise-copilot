"use client";

import { Boxes, Cpu, ScrollText, ShieldCheck } from "lucide-react";
import { useLocale, useTranslations } from "next-intl";
import * as React from "react";
import { z } from "zod";
import { ApiErrorAlert } from "@/components/common/api-error";
import { PageHeader } from "@/components/common/page-header";
import { StatusBadge } from "@/components/common/status-badge";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { EmptyState, LoadingRows } from "@/components/ui/feedback";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { TBody, TD, TH, THead, TR, Table } from "@/components/ui/table";
import { useApiQuery } from "@/lib/api/hooks";
import { auditEventSchema, modelPolicySchema, modelSchema, providerSchema } from "@/lib/api/schemas";
import { formatDateTime, shortId } from "@/lib/format";
import { ReadinessCard } from "./readiness-card";

function AuditLog() {
  const t = useTranslations("governance.audit");
  const locale = useLocale();
  const events = useApiQuery("/v1/audit-events", z.array(auditEventSchema));
  const [filter, setFilter] = React.useState("");
  const rows = (events.data ?? []).filter((event) => !filter || event.event_type.includes(filter.trim())).slice().reverse();
  return (
    <Card className="xl:col-span-2">
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <ScrollText aria-hidden className="size-4 text-primary" />
          {t("title")}
        </CardTitle>
        <CardDescription>{t("hint")}</CardDescription>
      </CardHeader>
      <CardContent className="space-y-3">
        <div className="max-w-sm space-y-1.5">
          <Label htmlFor="audit-filter">{t("filter")}</Label>
          <Input id="audit-filter" value={filter} onChange={(event) => setFilter(event.target.value)} placeholder={t("filterPlaceholder")} />
        </div>
        {events.isPending ? (
          <LoadingRows label={t("loading")} rows={5} />
        ) : events.isError ? (
          <ApiErrorAlert error={events.error} />
        ) : rows.length === 0 ? (
          <EmptyState icon={ScrollText} title={t("empty")} />
        ) : (
          <Table caption={t("title")} data-testid="audit-table">
            <THead>
              <TR>
                <TH>{t("time")}</TH>
                <TH>{t("event")}</TH>
                <TH>{t("outcome")}</TH>
                <TH className="hidden md:table-cell">{t("user")}</TH>
                <TH className="hidden lg:table-cell">{t("correlation")}</TH>
              </TR>
            </THead>
            <TBody>
              {rows.map((event) => (
                <TR key={event.event_id}>
                  <TD className="whitespace-nowrap text-xs">{formatDateTime(event.timestamp, locale)}</TD>
                  <TD className="font-mono text-xs">{event.event_type}</TD>
                  <TD>
                    <StatusBadge value={event.outcome === "success" ? "completed" : event.outcome === "failed" ? "failed" : event.outcome} />
                  </TD>
                  <TD className="hidden font-mono text-xs md:table-cell">{shortId(event.user_id)}</TD>
                  <TD className="hidden font-mono text-xs lg:table-cell">{shortId(event.correlation_id)}</TD>
                </TR>
              ))}
            </TBody>
          </Table>
        )}
      </CardContent>
    </Card>
  );
}

function ModelCatalog() {
  const t = useTranslations("governance.models");
  const providers = useApiQuery("/v1/admin/providers", z.array(providerSchema));
  const models = useApiQuery("/v1/admin/models", z.array(modelSchema));
  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <Cpu aria-hidden className="size-4 text-primary" />
          {t("title")}
        </CardTitle>
        <CardDescription>{t("hint")}</CardDescription>
      </CardHeader>
      <CardContent className="space-y-4">
        {providers.isError ? <ApiErrorAlert error={providers.error} /> : null}
        <div className="flex flex-wrap gap-2" data-testid="providers">
          {(providers.data ?? []).map((provider) => (
            <Badge key={provider.provider_id} tone={provider.health === "healthy" ? "success" : "neutral"}>
              {provider.provider_id} · {t.has(`health.${provider.health}` as never) ? t(`health.${provider.health}` as never) : provider.health}
            </Badge>
          ))}
        </div>
        {models.isPending ? (
          <LoadingRows label={t("loading")} />
        ) : models.isError ? (
          <ApiErrorAlert error={models.error} />
        ) : (
          <Table caption={t("title")}>
            <THead>
              <TR>
                <TH>{t("model")}</TH>
                <TH>{t("tier")}</TH>
                <TH>{t("enabled")}</TH>
                <TH className="hidden md:table-cell">{t("highRisk")}</TH>
              </TR>
            </THead>
            <TBody>
              {models.data.map((model) => (
                <TR key={`${model.provider_id}/${model.model_id}`}>
                  <TD>
                    <p className="font-medium">{model.display_name}</p>
                    <p className="font-mono text-xs text-muted-foreground">
                      {model.provider_id}/{model.model_id}
                    </p>
                  </TD>
                  <TD>{model.routing_tier}</TD>
                  <TD>
                    <Badge tone={model.enabled ? "success" : "neutral"}>{model.enabled ? t("yes") : t("no")}</Badge>
                  </TD>
                  <TD className="hidden md:table-cell">{model.high_risk_allowed ? t("yes") : t("no")}</TD>
                </TR>
              ))}
            </TBody>
          </Table>
        )}
        <p className="text-xs text-muted-foreground">{t("readOnly")}</p>
      </CardContent>
    </Card>
  );
}

function Policies() {
  const t = useTranslations("governance.policies");
  const policies = useApiQuery("/v1/admin/model-policies", z.array(modelPolicySchema));
  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <Boxes aria-hidden className="size-4 text-primary" />
          {t("title")}
        </CardTitle>
        <CardDescription>{t("hint")}</CardDescription>
      </CardHeader>
      <CardContent>
        {policies.isPending ? (
          <LoadingRows label={t("loading")} />
        ) : policies.isError ? (
          <ApiErrorAlert error={policies.error} />
        ) : policies.data.length === 0 ? (
          <EmptyState icon={ShieldCheck} title={t("empty")} description={t("emptyHint")} />
        ) : (
          policies.data.map((policy, index) => (
            <dl key={index} className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1 text-sm">
              {Object.entries(policy).map(([key, value]) => (
                <React.Fragment key={key}>
                  <dt className="text-muted-foreground">{t.has(`fields.${key}` as never) ? t(`fields.${key}` as never) : key}</dt>
                  <dd className="font-mono text-xs">{Array.isArray(value) ? value.join(", ") || "—" : String(value)}</dd>
                </React.Fragment>
              ))}
            </dl>
          ))
        )}
      </CardContent>
    </Card>
  );
}

export function Governance() {
  const t = useTranslations("governance");
  return (
    <>
      <PageHeader title={t("title")} description={t("description")} />
      <div className="grid grid-cols-1 items-start gap-4 xl:grid-cols-2">
        <AuditLog />
        <ModelCatalog />
        <Policies />
        <ReadinessCard />
      </div>
    </>
  );
}
