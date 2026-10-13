"use client";

import { Scale } from "lucide-react";
import { useLocale, useTranslations } from "next-intl";
import Link from "next/link";
import * as React from "react";
import { ApiErrorAlert } from "@/components/common/api-error";
import { StatusBadge } from "@/components/common/status-badge";
import { useSession } from "@/components/providers/session-context";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Alert, EmptyState, LoadingRows } from "@/components/ui/feedback";
import { TBody, TD, TH, THead, TR, Table } from "@/components/ui/table";
import { useApiQuery } from "@/lib/api/hooks";
import { claimsSchema, type Claim } from "@/lib/api/schemas";
import { formatDate, formatMoney } from "@/lib/format";
import { canReview } from "@/lib/roles";
import { LegalNotice } from "./legal-notice";
import { ReviewDialog, type ReviewTarget } from "./review-dialog";
import { useLabel } from "./use-label";

function ClaimCard({ claim, onReview }: { claim: Claim; onReview?: (target: ReviewTarget) => void }) {
  const t = useTranslations("intelligence.claims");
  const locale = useLocale();
  const roles = useLabel("intelligence.amountRoles");
  const factTypes = useLabel("intelligence.factTypes");
  const classes = useLabel("intelligence.classes");
  const status = useLabel("intelligence.claimStatus");
  return (
    <Card data-testid="claim-card">
      <CardHeader className="gap-2 sm:flex-row sm:items-start sm:justify-between">
        <div className="min-w-0">
          <CardTitle className="truncate">{claim.creditor}</CardTitle>
          <CardDescription>
            {t("identifier")}: <span className="font-mono">{claim.identifier}</span> ({claim.identifier_type})
          </CardDescription>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <Badge tone={claim.resolved ? "success" : "warning"}>{claim.resolved ? t("resolved") : t("unresolved")}</Badge>
          <Badge tone="neutral">{status(claim.status)}</Badge>
          {claim.settled ? <Badge tone="info">{t("settledStated")}</Badge> : null}
          {onReview ? (
            <Button
              variant="outline"
              size="sm"
              onClick={() => onReview({ entityType: "claim_group", entityId: claim.claim_group_id, expectedVersion: claim.review_version, label: claim.creditor })}
            >
              {t("reviewGroup")}
            </Button>
          ) : null}
        </div>
      </CardHeader>
      <CardContent className="space-y-3">
        {claim.potentially_current ? (
          <div className="rounded-md border bg-muted/40 p-3" data-testid="latest-stated">
            <p className="text-xs font-medium text-muted-foreground">
              {claim.potentially_current.basis === "latest_stated_balance" ? t("latestStatedBalance") : t("latestPrincipalOnly")}
            </p>
            <p className="tabular text-xl font-semibold">{formatMoney(claim.potentially_current.amount, claim.potentially_current.currency, locale)}</p>
            <p className="text-xs text-muted-foreground">{t("statedAsOf", { date: formatDate(claim.potentially_current.as_of, locale) })}</p>
          </div>
        ) : (
          <Alert tone="warning">{t("noLatest")}</Alert>
        )}
        {claim.unresolved_reasons.length ? (
          <p className="text-xs text-warning-foreground">
            {t("openPoints")}: {claim.unresolved_reasons.join(", ")}
          </p>
        ) : null}
        <Table caption={t("history")}>
          <THead>
            <TR>
              <TH>{t("document")}</TH>
              <TH>{t("amountType")}</TH>
              <TH className="text-right">{t("statedAmount")}</TH>
              <TH>{t("role")}</TH>
              <TH>{t("membership")}</TH>
            </TR>
          </THead>
          <TBody>
            {claim.documents.flatMap((member) =>
              (member.amounts.length ? member.amounts : [null]).map((amount, index) => (
                <TR key={`${member.member_id}-${amount?.fact_id ?? index}`}>
                  <TD className="max-w-[14rem]">
                    {index === 0 ? (
                      <>
                        <Link href={`/intelligence/documents/${member.document_id}`} className="font-medium text-primary hover:underline">
                          {member.title}
                        </Link>
                        <p className="text-xs text-muted-foreground">
                          {classes(member.document_class)} · {formatDate(member.issue_date, locale)}
                        </p>
                      </>
                    ) : null}
                  </TD>
                  <TD>{amount ? factTypes(amount.fact_type) : "—"}</TD>
                  <TD className="tabular text-right">{amount ? formatMoney(amount.amount, amount.currency, locale) : "—"}</TD>
                  <TD>{amount ? <Badge tone={amount.role === "potentially_current" ? "brand" : "neutral"}>{roles(amount.role)}</Badge> : null}</TD>
                  <TD>
                    {index === 0 ? (
                      <span className="flex items-center gap-1">
                        <StatusBadge value={member.membership} />
                        {onReview ? (
                          <Button
                            variant="ghost"
                            size="sm"
                            onClick={() => onReview({ entityType: "claim_member", entityId: member.member_id, expectedVersion: member.review_version, label: member.title })}
                          >
                            {t("review")}
                          </Button>
                        ) : null}
                      </span>
                    ) : null}
                  </TD>
                </TR>
              )),
            )}
          </TBody>
        </Table>
      </CardContent>
    </Card>
  );
}

export function ClaimsView() {
  const t = useTranslations("intelligence.claims");
  const locale = useLocale();
  const session = useSession();
  const claims = useApiQuery("/v1/intelligence/claims", claimsSchema);
  const reasons = useLabel("intelligence.findingReasons");
  const [target, setTarget] = React.useState<ReviewTarget | null>(null);
  if (claims.isPending) return <LoadingRows label={t("loading")} rows={4} />;
  if (claims.isError) return <ApiErrorAlert error={claims.error} />;
  const data = claims.data;
  const reviewer = canReview(session.roles);
  return (
    <div className="space-y-4">
      <LegalNotice />
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <Scale aria-hidden className="size-4 text-primary" />
            {t("summary")}
          </CardTitle>
          <CardDescription>{t("summaryHint")}</CardDescription>
        </CardHeader>
        <CardContent className="space-y-3">
          {data.complete && data.consolidated_total ? (
            <div data-testid="consolidated-total">
              <p className="text-sm text-muted-foreground">{t("consolidated")}</p>
              {Object.entries(data.consolidated_total).map(([currency, value]) => (
                <p key={currency} className="tabular text-2xl font-semibold">
                  {formatMoney(value, currency, locale)}
                </p>
              ))}
            </div>
          ) : (
            <Alert tone="warning" title={t("noTotalTitle")} live>
              {t("noTotalBody", { count: data.unresolved.length })}
            </Alert>
          )}
          {Object.keys(data.resolved_subtotal).length && !data.complete ? (
            <p className="text-sm text-muted-foreground">
              {t("resolvedSubtotal")}:{" "}
              {Object.entries(data.resolved_subtotal)
                .map(([currency, value]) => formatMoney(value, currency, locale))
                .join(" · ")}
            </p>
          ) : null}
          {data.unresolved.length ? (
            <ul className="space-y-1 text-sm" data-testid="unresolved">
              {data.unresolved.map((item, index) => (
                <li key={`${item.document_id ?? item.claim_group_id}-${index}`} className="flex flex-wrap gap-2">
                  {item.document_id ? (
                    <Link href={`/intelligence/documents/${item.document_id}`} className="text-primary hover:underline">
                      {item.title ?? item.document_id}
                    </Link>
                  ) : (
                    <span className="font-mono text-xs">{item.claim_group_id}</span>
                  )}
                  <span className="text-muted-foreground">— {item.reason.split(",").map(reasons).join(", ")}</span>
                </li>
              ))}
            </ul>
          ) : null}
          <p className="text-xs text-muted-foreground">{t("rule", { rule: data.aggregation_rule })}</p>
        </CardContent>
      </Card>
      {data.claims.length === 0 ? (
        <EmptyState icon={Scale} title={t("empty")} />
      ) : (
        data.claims.map((claim) => <ClaimCard key={claim.claim_group_id} claim={claim} onReview={reviewer ? setTarget : undefined} />)
      )}
      <ReviewDialog target={target} onClose={() => setTarget(null)} />
    </div>
  );
}
