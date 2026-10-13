"use client";

import { CalendarClock, ClipboardCheck, FileSearch, Landmark, Scale, Users } from "lucide-react";
import { useLocale, useTranslations } from "next-intl";
import Link from "next/link";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import * as React from "react";
import { z } from "zod";
import { ApiErrorAlert } from "@/components/common/api-error";
import { PageHeader } from "@/components/common/page-header";
import { StatusBadge } from "@/components/common/status-badge";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent } from "@/components/ui/card";
import { EmptyState, LoadingRows } from "@/components/ui/feedback";
import { Label } from "@/components/ui/label";
import { NativeSelect } from "@/components/ui/native-select";
import { TBody, TD, TH, THead, TR, Table } from "@/components/ui/table";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { useApiQuery } from "@/lib/api/hooks";
import { creditorSchema, deadlineSchema, findingSchema, intelligenceDocumentSchema } from "@/lib/api/schemas";
import { formatDate, shortId } from "@/lib/format";
import { ClaimsView } from "./claims-view";
import { PersonsView } from "./persons-view";
import { useLabel } from "./use-label";

const TABS = ["documents", "findings", "claims", "creditors", "deadlines", "persons"] as const;
type Tab = (typeof TABS)[number];
const CLASSES = [
  "invoice",
  "payment_reminder",
  "formal_demand",
  "debt_collection_letter",
  "payment_order_or_court_notice",
  "enforcement_document",
  "creditor_statement",
  "loan_agreement",
  "payment_receipt",
  "settlement_agreement",
  "correspondence",
  "unrelated_document",
  "unknown",
];

function DocumentsTab() {
  const t = useTranslations("intelligence.documents");
  const classes = useLabel("intelligence.classes");
  const debtor = useLabel("intelligence.debtorStatus");
  const [classification, setClassification] = React.useState("");
  const [review, setReview] = React.useState("");
  const query = new URLSearchParams();
  if (classification) query.set("classification", classification);
  if (review) query.set("review_required", review);
  const documents = useApiQuery(`/v1/intelligence/documents${query.size ? `?${query}` : ""}`, z.array(intelligenceDocumentSchema));
  return (
    <div className="space-y-4">
      <div className="grid gap-3 sm:grid-cols-2 lg:w-2/3">
        <div className="space-y-1.5">
          <Label htmlFor="filter-class">{t("filterClass")}</Label>
          <NativeSelect id="filter-class" value={classification} onChange={(event) => setClassification(event.target.value)}>
            <option value="">{t("all")}</option>
            {CLASSES.map((value) => (
              <option key={value} value={value}>
                {classes(value)}
              </option>
            ))}
          </NativeSelect>
        </div>
        <div className="space-y-1.5">
          <Label htmlFor="filter-review">{t("filterReview")}</Label>
          <NativeSelect id="filter-review" value={review} onChange={(event) => setReview(event.target.value)}>
            <option value="">{t("all")}</option>
            <option value="true">{t("needsReview")}</option>
            <option value="false">{t("noReview")}</option>
          </NativeSelect>
        </div>
      </div>
      {documents.isPending ? (
        <LoadingRows label={t("loading")} rows={5} />
      ) : documents.isError ? (
        <ApiErrorAlert error={documents.error} />
      ) : documents.data.length === 0 ? (
        <EmptyState icon={FileSearch} title={t("empty")} description={t("emptyHint")} />
      ) : (
        <Table caption={t("caption")} data-testid="intelligence-documents">
          <THead>
            <TR>
              <TH>{t("title")}</TH>
              <TH>{t("classification")}</TH>
              <TH className="hidden md:table-cell">{t("debtor")}</TH>
              <TH>{t("status")}</TH>
            </TR>
          </THead>
          <TBody>
            {documents.data.map((doc) => (
              <TR key={doc.document_id} data-testid="intelligence-document-row">
                <TD className="max-w-[20rem]">
                  <Link href={`/intelligence/documents/${doc.document_id}`} className="font-medium text-primary hover:underline">
                    {doc.title}
                  </Link>
                </TD>
                <TD>{classes(doc.classification)}</TD>
                <TD className="hidden md:table-cell">{debtor(doc.debtor_status)}</TD>
                <TD>
                  <div className="flex flex-wrap gap-1">
                    <StatusBadge value={doc.analysis_status} />
                    {doc.review_required ? <StatusBadge value="review_required" /> : null}
                  </div>
                </TD>
              </TR>
            ))}
          </TBody>
        </Table>
      )}
    </div>
  );
}

function FindingsTab() {
  const t = useTranslations("intelligence.findings");
  const kinds = useLabel("intelligence.findingKinds");
  const reasons = useLabel("intelligence.findingReasons");
  const factTypes = useLabel("intelligence.factTypes");
  const findings = useApiQuery("/v1/intelligence/findings", z.array(findingSchema));
  if (findings.isPending) return <LoadingRows label={t("loading")} rows={4} />;
  if (findings.isError) return <ApiErrorAlert error={findings.error} />;
  if (findings.data.length === 0) return <EmptyState icon={ClipboardCheck} title={t("empty")} description={t("emptyHint")} />;
  return (
    <Table caption={t("caption")} data-testid="findings-table">
      <THead>
        <TR>
          <TH>{t("kind")}</TH>
          <TH>{t("reason")}</TH>
          <TH>{t("subject")}</TH>
          <TH className="text-right">{t("open")}</TH>
        </TR>
      </THead>
      <TBody>
        {findings.data.map((finding, index) => (
          <TR key={`${finding.kind}-${finding.id}-${index}`} data-testid="finding-row">
            <TD>
              <Badge tone="warning">{kinds(finding.kind)}</Badge>
            </TD>
            <TD>{reasons(finding.reason)}</TD>
            <TD>{finding.fact_type ? factTypes(finding.fact_type) : <span className="font-mono text-xs">{shortId(finding.id)}</span>}</TD>
            <TD className="text-right">
              {finding.document_id ? (
                <Link href={`/intelligence/documents/${finding.document_id}`} className="text-sm font-medium text-primary hover:underline">
                  {t("openDocument")}
                </Link>
              ) : (
                <Link href="/intelligence?tab=claims" className="text-sm font-medium text-primary hover:underline">
                  {t("openClaims")}
                </Link>
              )}
            </TD>
          </TR>
        ))}
      </TBody>
    </Table>
  );
}

function CreditorsTab() {
  const t = useTranslations("intelligence.creditors");
  const factTypes = useLabel("intelligence.factTypes");
  const creditors = useApiQuery("/v1/intelligence/creditors", z.array(creditorSchema));
  if (creditors.isPending) return <LoadingRows label={t("loading")} rows={4} />;
  if (creditors.isError) return <ApiErrorAlert error={creditors.error} />;
  if (creditors.data.length === 0) return <EmptyState icon={Landmark} title={t("empty")} />;
  return (
    <Table caption={t("caption")} data-testid="creditors-table">
      <THead>
        <TR>
          <TH>{t("creditor")}</TH>
          <TH>{t("roles")}</TH>
          <TH>{t("documents")}</TH>
        </TR>
      </THead>
      <TBody>
        {creditors.data.map((creditor) => (
          <TR key={creditor.creditor}>
            <TD className="font-medium">{creditor.creditor}</TD>
            <TD>
              <div className="flex flex-wrap gap-1">
                {creditor.roles.map((role) => (
                  <Badge key={role} tone="neutral">
                    {factTypes(role)}
                  </Badge>
                ))}
              </div>
            </TD>
            <TD>
              <div className="flex flex-wrap gap-2">
                {creditor.documents.map((reference) => (
                  <Link key={reference.fact_id} href={`/intelligence/documents/${reference.document_id}`} className="font-mono text-xs text-primary hover:underline">
                    {shortId(reference.document_id)}
                  </Link>
                ))}
              </div>
            </TD>
          </TR>
        ))}
      </TBody>
    </Table>
  );
}

function DeadlinesTab() {
  const t = useTranslations("intelligence.deadlines");
  const locale = useLocale();
  const factTypes = useLabel("intelligence.factTypes");
  const deadlines = useApiQuery("/v1/intelligence/deadlines", z.array(deadlineSchema));
  if (deadlines.isPending) return <LoadingRows label={t("loading")} rows={4} />;
  if (deadlines.isError) return <ApiErrorAlert error={deadlines.error} />;
  if (deadlines.data.length === 0) return <EmptyState icon={CalendarClock} title={t("empty")} />;
  return (
    <div className="space-y-3">
      <p className="text-sm text-muted-foreground">{t("hint")}</p>
      <Table caption={t("caption")} data-testid="deadlines-table">
        <THead>
          <TR>
            <TH>{t("date")}</TH>
            <TH>{t("type")}</TH>
            <TH>{t("document")}</TH>
            <TH className="hidden md:table-cell">{t("page")}</TH>
            <TH>{t("state")}</TH>
          </TR>
        </THead>
        <TBody>
          {deadlines.data.map((deadline) => (
            <TR key={deadline.fact_id}>
              <TD className="tabular font-medium">{formatDate(deadline.date, locale)}</TD>
              <TD>{factTypes(deadline.fact_type)}</TD>
              <TD className="max-w-[16rem] truncate">
                <Link href={`/intelligence/documents/${deadline.document_id}`} className="text-primary hover:underline">
                  {deadline.title}
                </Link>
              </TD>
              <TD className="tabular hidden md:table-cell">{deadline.page_number ?? "—"}</TD>
              <TD>
                <StatusBadge value={deadline.review_state} />
              </TD>
            </TR>
          ))}
        </TBody>
      </Table>
    </div>
  );
}

export function IntelligenceHub() {
  const t = useTranslations("intelligence");
  const params = useSearchParams();
  const router = useRouter();
  const pathname = usePathname();
  const requested = params.get("tab");
  const tab: Tab = (TABS as readonly string[]).includes(requested ?? "") ? (requested as Tab) : "documents";
  const icons: Record<Tab, React.ElementType> = {
    documents: FileSearch,
    findings: ClipboardCheck,
    claims: Scale,
    creditors: Landmark,
    deadlines: CalendarClock,
    persons: Users,
  };
  return (
    <>
      <PageHeader title={t("title")} description={t("description")} />
      <Tabs value={tab} onValueChange={(value) => router.replace(`${pathname}?tab=${value}`, { scroll: false })}>
        <TabsList aria-label={t("sections")}>
          {TABS.map((value) => {
            const Icon = icons[value];
            return (
              <TabsTrigger key={value} value={value} data-testid={`tab-${value}`}>
                <Icon aria-hidden className="size-4" />
                {t(`tabs.${value}`)}
              </TabsTrigger>
            );
          })}
        </TabsList>
        <Card className="mt-4">
          <CardContent className="p-4 sm:p-5">
            <TabsContent value="documents" className="mt-0">
              <DocumentsTab />
            </TabsContent>
            <TabsContent value="findings" className="mt-0">
              <FindingsTab />
            </TabsContent>
            <TabsContent value="claims" className="mt-0">
              <ClaimsView />
            </TabsContent>
            <TabsContent value="creditors" className="mt-0">
              <CreditorsTab />
            </TabsContent>
            <TabsContent value="deadlines" className="mt-0">
              <DeadlinesTab />
            </TabsContent>
            <TabsContent value="persons" className="mt-0">
              <PersonsView />
            </TabsContent>
          </CardContent>
        </Card>
      </Tabs>
    </>
  );
}
