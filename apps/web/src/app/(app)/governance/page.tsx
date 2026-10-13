import { getTranslations } from "next-intl/server";
import { RequireRole } from "@/components/common/require-role";
import { Governance } from "@/components/ops/governance";

export async function generateMetadata() {
  const t = await getTranslations("nav");
  return { title: t("governance") };
}

export default function GovernancePage() {
  return (
    <RequireRole role="admin">
      <Governance />
    </RequireRole>
  );
}
