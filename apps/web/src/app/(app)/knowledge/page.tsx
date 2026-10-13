import { getTranslations } from "next-intl/server";
import { KnowledgeBase } from "@/components/knowledge/knowledge-base";

export async function generateMetadata() {
  const t = await getTranslations("nav");
  return { title: t("knowledge") };
}

export default function KnowledgePage() {
  return <KnowledgeBase />;
}
