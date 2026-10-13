"use client";

import { useTranslations } from "next-intl";
import { Alert } from "@/components/ui/feedback";
import { ApiError, ContractError } from "@/lib/api/client";

const KNOWN = [
  "unauthenticated",
  "session_expired",
  "forbidden",
  "not_found",
  "conflict",
  "rate_limited",
  "unavailable",
  "api_unreachable",
  "network_error",
  "csrf_rejected",
  "too_large",
  "validation_error",
  "bad_request",
] as const;
type Known = (typeof KNOWN)[number];

export function errorKey(error: unknown): Known | "contract" | "unknown" {
  if (error instanceof ContractError) return "contract";
  if (error instanceof ApiError && (KNOWN as readonly string[]).includes(error.code)) return error.code as Known;
  return "unknown";
}

/** Generic, translated messages; upstream details and stack traces are never shown. */
export function ApiErrorAlert({ error, className }: { error: unknown; className?: string }) {
  const t = useTranslations("errors");
  const key = errorKey(error);
  const retry = error instanceof ApiError && error.retryAfterSeconds ? t("retryAfter", { seconds: error.retryAfterSeconds }) : null;
  return (
    <Alert tone={key === "forbidden" || key === "not_found" ? "warning" : "danger"} title={t(`${key}.title`)} className={className} live>
      {t(`${key}.body`)}
      {retry ? ` ${retry}` : null}
    </Alert>
  );
}
