import { getTranslations } from "next-intl/server";
import { RequireRole } from "@/components/common/require-role";
import { Monitoring } from "@/components/ops/monitoring";

export async function generateMetadata() {
  const t = await getTranslations("nav");
  return { title: t("monitoring") };
}

export default function MonitoringPage() {
  return (
    <RequireRole role="admin">
      <Monitoring />
    </RequireRole>
  );
}
