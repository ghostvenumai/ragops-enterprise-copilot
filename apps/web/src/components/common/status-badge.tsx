"use client";

import { useTranslations } from "next-intl";
import { Badge } from "@/components/ui/badge";

type Tone = "neutral" | "brand" | "success" | "warning" | "danger" | "info";

const TONES: Record<string, Tone> = {
  completed: "success",
  indexed: "success",
  ready: "success",
  active: "success",
  accepted: "success",
  up: "success",
  confirmed: "success",
  classified: "success",
  attributed: "success",
  system_linked: "success",
  linked: "success",
  analyzed: "success",
  running: "info",
  processing: "info",
  queued: "info",
  retrying: "info",
  pending: "info",
  proposed: "info",
  review_required: "warning",
  needs_review: "warning",
  ambiguous: "warning",
  unready: "warning",
  corrected: "brand",
  not_configured: "neutral",
  not_analyzed: "neutral",
  unknown: "neutral",
  cancelled: "neutral",
  dismissed: "neutral",
  failed: "danger",
  down: "danger",
  rejected: "danger",
  dead_letter: "danger",
};

/** A known state gets a translated label; an unknown one is shown verbatim, never hidden. */
export function StatusBadge({ value, testId }: { value: string | null | undefined; testId?: string }) {
  const t = useTranslations("states");
  const state = value ?? "unknown";
  const label = t.has(state as never) ? t(state as never) : state;
  return (
    <Badge tone={TONES[state] ?? "neutral"} data-testid={testId} data-state={state}>
      {label}
    </Badge>
  );
}
