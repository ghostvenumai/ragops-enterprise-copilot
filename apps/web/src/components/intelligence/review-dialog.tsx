"use client";

import { useQueryClient } from "@tanstack/react-query";
import { Loader2 } from "lucide-react";
import { useTranslations } from "next-intl";
import * as React from "react";
import { toast } from "sonner";
import { ApiErrorAlert } from "@/components/common/api-error";
import { useSession } from "@/components/providers/session-context";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Alert } from "@/components/ui/feedback";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { NativeSelect } from "@/components/ui/native-select";
import { ApiError, api } from "@/lib/api/client";
import {
  REASON_CATEGORIES,
  reviewRequestSchema,
  reviewResultSchema,
  type REVIEW_ACTIONS,
  type REVIEW_ENTITY_TYPES,
  type ReviewRequest,
} from "@/lib/api/schemas";

type EntityType = (typeof REVIEW_ENTITY_TYPES)[number];
type Action = (typeof REVIEW_ACTIONS)[number];

/** Actions the backend accepts per entity (ragops.intelligence.queries.IntelligenceReviewer). */
export const ACTIONS_BY_ENTITY: Record<EntityType, readonly Action[]> = {
  fact: ["confirm", "reject", "correct", "mark_for_review"],
  classification: ["confirm", "reject", "mark_for_review"],
  person_link: ["confirm", "reject"],
  claim_group: ["confirm", "dismiss"],
  claim_member: ["confirm", "reject"],
  document: ["mark_for_review", "clear_review_flag", "assign_person"],
};

export interface ReviewTarget {
  entityType: EntityType;
  entityId: string;
  expectedVersion: number;
  label: string;
  /** For facts: which typed correction the backend accepts. */
  category?: string;
  currency?: string | null;
  persons?: { id: string; label: string }[];
}

export function ReviewDialog({ target, onClose }: { target: ReviewTarget | null; onClose: () => void }) {
  const t = useTranslations("intelligence.review");
  const { csrfToken } = useSession();
  const queryClient = useQueryClient();
  const actions = target ? ACTIONS_BY_ENTITY[target.entityType] : [];
  const [action, setAction] = React.useState<Action>("confirm");
  const [reason, setReason] = React.useState<(typeof REASON_CATEGORIES)[number]>("verified_against_source");
  const [amount, setAmount] = React.useState("");
  const [currency, setCurrency] = React.useState("EUR");
  const [date, setDate] = React.useState("");
  const [text, setText] = React.useState("");
  const [personId, setPersonId] = React.useState("");
  const [role, setRole] = React.useState<"debtor" | "addressee">("debtor");
  const [busy, setBusy] = React.useState(false);
  const [error, setError] = React.useState<unknown>(null);
  const [invalid, setInvalid] = React.useState<string | null>(null);

  React.useEffect(() => {
    if (!target) return;
    setAction(ACTIONS_BY_ENTITY[target.entityType][0] ?? "confirm");
    setReason("verified_against_source");
    setAmount("");
    setCurrency(target.currency ?? "EUR");
    setDate("");
    setText("");
    setPersonId(target.persons?.[0]?.id ?? "");
    setError(null);
    setInvalid(null);
  }, [target]);

  const correcting = action === "correct" && target?.entityType === "fact";
  const conflict = error instanceof ApiError && error.status === 409;

  function buildRequest(): ReviewRequest | null {
    if (!target) return null;
    const request: Record<string, unknown> = {
      entity_type: target.entityType,
      entity_id: target.entityId,
      action,
      expected_version: target.expectedVersion,
      reason_category: reason,
    };
    if (correcting) {
      if (target.category === "amount") request.value = { amount: amount.trim().replace(",", "."), currency: currency.trim().toUpperCase() };
      else if (target.category === "date") request.value = { date };
      else request.value = { text: text.trim() };
    }
    if (action === "assign_person") {
      request.person_id = personId;
      request.role = role;
    }
    const parsed = reviewRequestSchema.safeParse(request);
    return parsed.success ? parsed.data : null;
  }

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    const request = buildRequest();
    if (!request) {
      setInvalid(correcting ? t(`invalid.${target?.category === "amount" ? "amount" : target?.category === "date" ? "date" : "text"}`) : t("invalid.generic"));
      return;
    }
    setInvalid(null);
    setBusy(true);
    setError(null);
    try {
      await api.post("/v1/intelligence/reviews", request, reviewResultSchema, { csrfToken });
      toast.success(t("saved"));
      await queryClient.invalidateQueries({ queryKey: ["api"] });
      onClose();
    } catch (failure) {
      setError(failure);
      if (failure instanceof ApiError && failure.status === 409) {
        // Someone else decided first: reload so the reviewer sees the current version.
        await queryClient.invalidateQueries({ queryKey: ["api"] });
      }
    } finally {
      setBusy(false);
    }
  }

  return (
    <Dialog open={target !== null} onOpenChange={(open) => (open ? undefined : onClose())}>
      <DialogContent closeLabel={t("close")} data-testid="review-dialog">
        <form onSubmit={submit} className="space-y-4" noValidate>
          <DialogHeader>
            <DialogTitle>{t("title")}</DialogTitle>
            <DialogDescription>{target?.label}</DialogDescription>
          </DialogHeader>
          <Alert tone="info">{t("auditNotice")}</Alert>
          <div className="space-y-1.5">
            <Label htmlFor="review-action">{t("action")}</Label>
            <NativeSelect id="review-action" value={action} onChange={(event) => setAction(event.target.value as Action)} data-testid="review-action">
              {actions.map((value) => (
                <option key={value} value={value}>
                  {t(`actions.${value}`)}
                </option>
              ))}
            </NativeSelect>
          </div>
          <div className="space-y-1.5">
            <Label htmlFor="review-reason">{t("reason")}</Label>
            <NativeSelect id="review-reason" value={reason} onChange={(event) => setReason(event.target.value as (typeof REASON_CATEGORIES)[number])} data-testid="review-reason">
              {REASON_CATEGORIES.map((value) => (
                <option key={value} value={value}>
                  {t(`reasons.${value}`)}
                </option>
              ))}
            </NativeSelect>
          </div>
          {correcting && target?.category === "amount" ? (
            <div className="grid grid-cols-[1fr_96px] gap-3">
              <div className="space-y-1.5">
                <Label htmlFor="review-amount">{t("amount")}</Label>
                <Input id="review-amount" inputMode="decimal" value={amount} onChange={(event) => setAmount(event.target.value)} placeholder="1234.50" data-testid="review-amount" />
              </div>
              <div className="space-y-1.5">
                <Label htmlFor="review-currency">{t("currency")}</Label>
                <Input id="review-currency" value={currency} maxLength={3} onChange={(event) => setCurrency(event.target.value)} />
              </div>
              <p className="col-span-2 text-xs text-muted-foreground">{t("amountHint")}</p>
            </div>
          ) : null}
          {correcting && target?.category === "date" ? (
            <div className="space-y-1.5">
              <Label htmlFor="review-date">{t("date")}</Label>
              <Input id="review-date" type="date" value={date} onChange={(event) => setDate(event.target.value)} />
            </div>
          ) : null}
          {correcting && target && target.category !== "amount" && target.category !== "date" ? (
            target.category === "status" ? (
              <Alert tone="warning">{t("statusNotCorrectable")}</Alert>
            ) : (
              <div className="space-y-1.5">
                <Label htmlFor="review-text">{t("text")}</Label>
                <Input id="review-text" value={text} maxLength={255} onChange={(event) => setText(event.target.value)} />
              </div>
            )
          ) : null}
          {action === "assign_person" ? (
            <div className="grid gap-3 sm:grid-cols-2">
              <div className="space-y-1.5">
                <Label htmlFor="review-person">{t("person")}</Label>
                <NativeSelect id="review-person" value={personId} onChange={(event) => setPersonId(event.target.value)}>
                  {(target?.persons ?? []).map((person) => (
                    <option key={person.id} value={person.id}>
                      {person.label}
                    </option>
                  ))}
                </NativeSelect>
              </div>
              <div className="space-y-1.5">
                <Label htmlFor="review-role">{t("role")}</Label>
                <NativeSelect id="review-role" value={role} onChange={(event) => setRole(event.target.value as "debtor" | "addressee")}>
                  <option value="debtor">{t("roles.debtor")}</option>
                  <option value="addressee">{t("roles.addressee")}</option>
                </NativeSelect>
              </div>
            </div>
          ) : null}
          {invalid ? (
            <p role="alert" className="text-sm text-destructive">
              {invalid}
            </p>
          ) : null}
          {conflict ? (
            <Alert tone="warning" title={t("conflictTitle")} live>
              {t("conflictBody")}
            </Alert>
          ) : error ? (
            <ApiErrorAlert error={error} />
          ) : null}
          <DialogFooter>
            <Button variant="outline" onClick={onClose}>
              {t("cancel")}
            </Button>
            <Button type="submit" disabled={busy || conflict} data-testid="review-submit">
              {busy ? <Loader2 aria-hidden className="animate-spin" /> : null}
              {t("submit")}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
