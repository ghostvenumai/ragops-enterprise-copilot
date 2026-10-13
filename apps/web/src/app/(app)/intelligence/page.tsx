import { getTranslations } from "next-intl/server";
import { Suspense } from "react";
import { IntelligenceHub } from "@/components/intelligence/intelligence-hub";

export async function generateMetadata() {
  const t = await getTranslations("nav");
  return { title: t("intelligence") };
}

export default function IntelligencePage() {
  return (
    <Suspense>
      <IntelligenceHub />
    </Suspense>
  );
}
