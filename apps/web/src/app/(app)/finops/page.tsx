import { getTranslations } from "next-intl/server";
import { RequireRole } from "@/components/common/require-role";
import { FinOps } from "@/components/ops/finops";

export async function generateMetadata() {
  const t = await getTranslations("nav");
  return { title: t("finops") };
}

export default function FinOpsPage() {
  return (
    <RequireRole role="admin">
      <FinOps />
    </RequireRole>
  );
}
