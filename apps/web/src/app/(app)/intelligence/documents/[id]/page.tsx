import { notFound } from "next/navigation";
import { getTranslations } from "next-intl/server";
import { DocumentDetail } from "@/components/intelligence/document-detail";

export async function generateMetadata() {
  const t = await getTranslations("nav");
  return { title: t("intelligence") };
}

const UUID = /^[0-9a-fA-F-]{36}$/;

export default async function IntelligenceDocumentPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  if (!UUID.test(id)) notFound();
  return <DocumentDetail documentId={id} />;
}
