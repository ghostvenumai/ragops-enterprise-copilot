"use client";

import { useQueryClient } from "@tanstack/react-query";
import { ArrowLeft, Flag, Quote, RefreshCw, ShieldAlert } from "lucide-react";
import { useLocale, useTranslations } from "next-intl";
import Link from "next/link";
import * as React from "react";
import { toast } from "sonner";
import { z } from "zod";
import { ApiErrorAlert } from "@/components/common/api-error";
import { PageHeader } from "@/components/common/page-header";
import { StatusBadge } from "@/components/common/status-badge";
import { useSession } from "@/components/providers/session-context";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Alert, EmptyState, LoadingRows } from "@/components/ui/feedback";
import { TBody, TD, TH, THead, TR, Table } from "@/components/ui/table";
import { api } from "@/lib/api/client";
import { useApiQuery } from "@/lib/api/hooks";
import { intelligenceDocumentDetailSchema, personSchema, type Fact } from "@/lib/api/schemas";
import { formatDate, formatMoney, shortId } from "@/lib/format";
import { canReview } from "@/lib/roles";
import { LegalNotice } from "./legal-notice";
import { ReviewDialog, type ReviewTarget } from "./review-dialog";
import { SourceDialog } from "./source-dialog";
import { useLabel } from "./use-label";

function FactValue({ fact }: { fact: Fact }) {
  const locale = useLocale();
  const t = useTranslations("intelligence.document");
  const effective = fact.effective;
  if (effective.origin === "rejected") return <span className="text-muted-foreground line-through">{t("rejectedValue")}</span>;
  const shown = effective.amount
    ? formatMoney(effective.amount, effective.currency ?? null, locale)
    : effective.date
      ? formatDate(effective.date, locale)
      : effective.text ?? "—";
  return (
    <span className="flex flex-wrap items-center gap-1.5">
      <span className="tabular font-medium" lang={effective.amount || effective.date ? undefined : ""}>
        {shown}
      </span>
      {effective.origin === "user" ? <Badge tone="brand">{t("correctedByReviewer")}</Badge> : null}
    </span>
  );
}

export function DocumentDetail({ documentId }: { documentId: string }) {
  const t = useTranslations("intelligence.document");
  const classes = useLabel("intelligence.classes");
  const factTypes = useLabel("intelligence.factTypes");
  const categories = useLabel("intelligence.categories");
  const debtor = useLabel("intelligence.debtorStatus");
  const classification = useLabel("intelligence.classificationStatus");
  const claimStatus = useLabel("intelligence.claimStatus");
  const strength = useLabel("intelligence.evidenceStrength");
  const session = useSession();
  const reviewer = canReview(session.roles);
  const queryClient = useQueryClient();
  const detail = useApiQuery(`/v1/intelligence/documents/${documentId}`, intelligenceDocumentDetailSchema);
  const persons = useApiQuery(reviewer ? "/v1/intelligence/persons" : null, z.array(personSchema));
  const [target, setTarget] = React.useState<ReviewTarget | null>(null);
  const [source, setSource] = React.useState<string | null>(null);
  const [reprocessing, setReprocessing] = React.useState(false);
  const [reprocessError, setReprocessError] = React.useState<unknown>(null);

  async function reprocess() {
    setReprocessing(true);
    setReprocessError(null);
    try {
      await api.post(`/v1/intelligence/documents/${documentId}/reprocess`, undefined, z.object({}).passthrough(), { csrfToken: session.csrfToken });
      toast.success(t("reprocessQueued"));
      await queryClient.invalidateQueries({ queryKey: ["api"] });
    } catch (error) {
      setReprocessError(error);
    } finally {
      setReprocessing(false);
    }
  }

  if (detail.isPending) return <LoadingRows label={t("loading")} rows={6} />;
  if (detail.isError) {
    return (
      <div className="space-y-4">
        <Link href="/intelligence" className="inline-flex items-center gap-1 text-sm text-primary hover:underline">
          <ArrowLeft aria-hidden className="size-4" />
          {t("back")}
        </Link>
        <ApiErrorAlert error={detail.error} />
      </div>
    );
  }
  const doc = detail.data;
  const personOptions = (persons.data ?? []).map((person) => ({ id: person.id, label: person.label }));
  const personName = (id: string) => persons.data?.find((person) => person.id === id)?.label ?? shortId(id);

  return (
    <>
      <Link href="/intelligence" className="mb-3 inline-flex items-center gap-1 text-sm text-primary hover:underline">
        <ArrowLeft aria-hidden className="size-4" />
        {t("back")}
      </Link>
      <PageHeader
        title={doc.title}
        description={t("description")}
        actions={
          reviewer ? (
            <>
              <Button
                variant="outline"
                onClick={() =>
                  doc.review_version !== null &&
                  setTarget({ entityType: "document", entityId: doc.document_id, expectedVersion: doc.review_version, label: doc.title, persons: personOptions })
                }
                disabled={doc.review_version === null}
                data-testid="review-document"
              >
                <Flag aria-hidden />
                {t("reviewDocument")}
              </Button>
              <Button variant="outline" onClick={() => void reprocess()} disabled={reprocessing}>
                <RefreshCw aria-hidden className={reprocessing ? "animate-spin" : undefined} />
                {t("reprocess")}
              </Button>
            </>
          ) : null
        }
      />
      {!reviewer ? (
        <Alert tone="info" className="mb-4" title={t("readOnlyTitle")}>
          {t("readOnlyBody")}
        </Alert>
      ) : null}
      {reprocessError ? <ApiErrorAlert error={reprocessError} className="mb-4" /> : null}
      {doc.ignored_instruction_lines > 0 ? (
        <Alert tone="warning" className="mb-4" title={t("injectionTitle")}>
          <ShieldAlert aria-hidden className="mr-1 inline size-4" />
          {t("injectionBody", { count: doc.ignored_instruction_lines })}
        </Alert>
      ) : null}
      <div className="mb-4 grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
        <Card>
          <CardContent className="p-4">
            <p className="text-xs text-muted-foreground">{t("classification")}</p>
            <p className="mt-1 font-semibold" data-testid="doc-classification">{classes(doc.classification)}</p>
            <p className="text-xs text-muted-foreground">{classification(doc.classification_status)}</p>
          </CardContent>
        </Card>
        <Card>
          <CardContent className="p-4">
            <p className="text-xs text-muted-foreground">{t("debtor")}</p>
            <p className="mt-1 font-semibold">{debtor(doc.debtor_status)}</p>
          </CardContent>
        </Card>
        <Card>
          <CardContent className="p-4">
            <p className="text-xs text-muted-foreground">{t("analysis")}</p>
            <div className="mt-1">
              <StatusBadge value={doc.analysis_status} />
            </div>
            <p className="mt-1 text-xs text-muted-foreground">{doc.extractor_version ?? "—"}</p>
          </CardContent>
        </Card>
        <Card>
          <CardContent className="p-4">
            <p className="text-xs text-muted-foreground">{t("reviewNeeded")}</p>
            <div className="mt-1">
              <StatusBadge value={doc.review_required ? "review_required" : "confirmed"} testId="doc-review-required" />
            </div>
          </CardContent>
        </Card>
      </div>

      <div className="space-y-4">
        <Card>
          <CardHeader>
            <CardTitle>{t("classifications")}</CardTitle>
            <CardDescription>{t("classificationsHint")}</CardDescription>
          </CardHeader>
          <CardContent>
            <Table caption={t("classifications")}>
              <THead>
                <TR>
                  <TH>{t("class")}</TH>
                  <TH>{t("strength")}</TH>
                  <TH>{t("state")}</TH>
                  {reviewer ? <TH className="text-right">{t("actions")}</TH> : null}
                </TR>
              </THead>
              <TBody>
                {doc.classifications.map((item) => (
                  <TR key={item.id}>
                    <TD>
                      <span className="font-medium">{classes(item.document_class)}</span>
                      {item.is_primary ? <Badge tone="brand" className="ml-2">{t("primary")}</Badge> : null}
                    </TD>
                    <TD>{strength(item.evidence_strength)}</TD>
                    <TD>
                      <StatusBadge value={item.review_state} />
                    </TD>
                    {reviewer ? (
                      <TD className="text-right">
                        <Button
                          variant="ghost"
                          size="sm"
                          onClick={() => setTarget({ entityType: "classification", entityId: item.id, expectedVersion: item.review_version, label: classes(item.document_class) })}
                        >
                          {t("review")}
                        </Button>
                      </TD>
                    ) : null}
                  </TR>
                ))}
              </TBody>
            </Table>
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle>{t("facts")}</CardTitle>
            <CardDescription>{t("factsHint")}</CardDescription>
          </CardHeader>
          <CardContent className="space-y-3">
            <LegalNotice />
            {doc.facts.length === 0 ? (
              <EmptyState icon={Quote} title={t("noFacts")} />
            ) : (
              <Table caption={t("facts")} data-testid="facts-table">
                <THead>
                  <TR>
                    <TH>{t("factType")}</TH>
                    <TH>{t("value")}</TH>
                    <TH className="hidden md:table-cell">{t("page")}</TH>
                    <TH>{t("state")}</TH>
                    <TH className="text-right">{t("actions")}</TH>
                  </TR>
                </THead>
                <TBody>
                  {doc.facts.map((fact) => (
                    <TR key={fact.id} data-testid="fact-row" data-fact-type={fact.fact_type}>
                      <TD>
                        <p className="font-medium">{factTypes(fact.fact_type)}</p>
                        <p className="text-xs text-muted-foreground">{categories(fact.category)}</p>
                      </TD>
                      <TD className="max-w-[18rem]">
                        <FactValue fact={fact} />
                      </TD>
                      <TD className="tabular hidden md:table-cell">{fact.page_number ?? "—"}</TD>
                      <TD>
                        <div className="flex flex-wrap gap-1">
                          <StatusBadge value={fact.review_state} testId="fact-review-state" />
                          {fact.quality_state !== "accepted" ? <StatusBadge value={fact.quality_state} /> : null}
                        </div>
                      </TD>
                      <TD>
                        <div className="flex justify-end gap-1">
                          <Button variant="ghost" size="sm" onClick={() => setSource(fact.id)} aria-label={t("sourceFor", { type: factTypes(fact.fact_type) })} data-testid="fact-source">
                            <Quote aria-hidden />
                            <span className="hidden lg:inline">{t("source")}</span>
                          </Button>
                          {reviewer ? (
                            <Button
                              variant="outline"
                              size="sm"
                              data-testid="fact-review"
                              onClick={() =>
                                setTarget({
                                  entityType: "fact",
                                  entityId: fact.id,
                                  expectedVersion: fact.review_version,
                                  label: factTypes(fact.fact_type),
                                  category: fact.category,
                                  currency: fact.currency,
                                })
                              }
                            >
                              {t("review")}
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
        </Card>

        <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
          <Card>
            <CardHeader>
              <CardTitle>{t("persons")}</CardTitle>
            </CardHeader>
            <CardContent>
              {doc.persons.length === 0 ? (
                <p className="text-sm text-muted-foreground">{t("noPersons")}</p>
              ) : (
                <ul className="divide-y text-sm">
                  {doc.persons.map((link) => (
                    <li key={link.id} className="flex flex-wrap items-center justify-between gap-2 py-2">
                      <span>
                        <span className="font-medium">{reviewer ? personName(link.person_id) : shortId(link.person_id)}</span>
                        <span className="ml-2 text-muted-foreground">{t(`personRoles.${link.role === "addressee" ? "addressee" : "debtor"}`)}</span>
                      </span>
                      <span className="flex items-center gap-2">
                        <StatusBadge value={link.state} />
                        {reviewer ? (
                          <Button variant="ghost" size="sm" onClick={() => setTarget({ entityType: "person_link", entityId: link.id, expectedVersion: link.review_version, label: personName(link.person_id) })}>
                            {t("review")}
                          </Button>
                        ) : null}
                      </span>
                    </li>
                  ))}
                </ul>
              )}
            </CardContent>
          </Card>
          <Card>
            <CardHeader>
              <CardTitle>{t("claimGroups")}</CardTitle>
            </CardHeader>
            <CardContent>
              {doc.claim_groups.length === 0 ? (
                <p className="text-sm text-muted-foreground">{t("noClaimGroups")}</p>
              ) : (
                <ul className="divide-y text-sm">
                  {doc.claim_groups.map((group) => (
                    <li key={group.member_id} className="flex flex-wrap items-center justify-between gap-2 py-2">
                      <span className="font-mono text-xs">{shortId(group.claim_group_id)}</span>
                      <span className="flex items-center gap-2">
                        <Badge tone="neutral">{claimStatus(group.status)}</Badge>
                        <StatusBadge value={group.membership} />
                      </span>
                    </li>
                  ))}
                </ul>
              )}
              <Link href="/intelligence?tab=claims" className="mt-3 inline-block text-sm font-medium text-primary hover:underline">
                {t("openClaims")}
              </Link>
            </CardContent>
          </Card>
        </div>
      </div>
      <ReviewDialog target={target} onClose={() => setTarget(null)} />
      <SourceDialog factId={source} onClose={() => setSource(null)} />
    </>
  );
}
