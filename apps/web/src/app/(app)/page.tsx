import { getTranslations } from "next-intl/server";
import { Cockpit } from "@/components/cockpit/cockpit";

export async function generateMetadata() {
  const t = await getTranslations("nav");
  return { title: t("cockpit") };
}

export default function CockpitPage() {
  return <Cockpit />;
}
