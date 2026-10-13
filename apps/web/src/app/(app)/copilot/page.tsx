import { getTranslations } from "next-intl/server";
import { Copilot } from "@/components/copilot/copilot";

export async function generateMetadata() {
  const t = await getTranslations("nav");
  return { title: t("copilot") };
}

export default function CopilotPage() {
  return <Copilot />;
}
