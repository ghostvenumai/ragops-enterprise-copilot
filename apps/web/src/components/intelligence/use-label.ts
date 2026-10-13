"use client";

import { useTranslations } from "next-intl";

type Namespace =
  | "intelligence.classes"
  | "intelligence.factTypes"
  | "intelligence.reviewStates"
  | "intelligence.assertions"
  | "intelligence.amountRoles"
  | "intelligence.findingKinds"
  | "intelligence.findingReasons"
  | "intelligence.claimStatus"
  | "intelligence.debtorStatus"
  | "intelligence.classificationStatus"
  | "intelligence.categories"
  | "intelligence.evidenceStrength"
  | "intelligence.extractionMethods";

/** Translates a backend vocabulary value; unknown values are shown verbatim, never dropped. */
export function useLabel(namespace: Namespace): (value: string | null | undefined) => string {
  const t = useTranslations(namespace);
  return (value) => {
    if (!value) return "—";
    return t.has(value as never) ? t(value as never) : value;
  };
}
