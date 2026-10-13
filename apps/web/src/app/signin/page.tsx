import { LogIn, ShieldCheck } from "lucide-react";
import { getTranslations } from "next-intl/server";
import { BrandMark } from "@/components/layout/brand";
import { LanguageSwitcher } from "@/components/layout/language-switcher";
import { Alert } from "@/components/ui/feedback";
import { buttonVariants } from "@/components/ui/button";
import { safeReturnTo } from "@/server/login-transaction";

export const dynamic = "force-dynamic";

const ERRORS = ["idp_unavailable", "invalid_state", "denied", "token_rejected", "session_expired"] as const;
type SignInError = (typeof ERRORS)[number];

export async function generateMetadata() {
  const t = await getTranslations("signin");
  return { title: t("title") };
}

export default async function SignInPage({ searchParams }: { searchParams: Promise<Record<string, string | string[] | undefined>> }) {
  const t = await getTranslations("signin");
  const params = await searchParams;
  const raw = typeof params.error === "string" ? params.error : null;
  const error = (ERRORS as readonly string[]).includes(raw ?? "") ? (raw as SignInError) : null;
  const signedOut = params.signedOut === "1";
  const returnTo = safeReturnTo(typeof params.returnTo === "string" ? params.returnTo : "/");
  return (
    <main id="main" className="grid min-h-screen lg:grid-cols-2">
      <section className="hidden flex-col justify-between bg-sidebar p-10 text-sidebar-foreground lg:flex" aria-hidden>
        <div className="flex items-center gap-3">
          <BrandMark className="size-9" />
          <span className="text-lg font-semibold text-white">RAGOps Enterprise Copilot</span>
        </div>
        <div className="max-w-md space-y-4">
          <p className="text-2xl font-semibold leading-snug text-white">{t("claim")}</p>
          <p className="text-sm text-sidebar-muted">{t("claimDetail")}</p>
        </div>
        <p className="text-xs text-sidebar-muted">{t("footer")}</p>
      </section>
      <section className="flex flex-col px-6 py-6 sm:px-10">
        <div className="flex justify-end">
          <LanguageSwitcher />
        </div>
        <div className="mx-auto flex w-full max-w-sm flex-1 flex-col justify-center gap-6">
          <div className="flex items-center gap-3 lg:hidden">
            <BrandMark className="size-9" />
            <span className="text-lg font-semibold">RAGOps</span>
          </div>
          <div className="space-y-2">
            <h1 className="text-2xl font-semibold tracking-tight">{t("title")}</h1>
            <p className="text-sm text-muted-foreground">{t("subtitle")}</p>
          </div>
          {error ? (
            <Alert tone={error === "session_expired" ? "warning" : "danger"} title={t(`errors.${error}.title`)} live>
              {t(`errors.${error}.body`)}
            </Alert>
          ) : null}
          {signedOut ? (
            <Alert tone="success" title={t("signedOutTitle")} live>
              {t("signedOutBody")}
            </Alert>
          ) : null}
          <a href={`/auth/login?returnTo=${encodeURIComponent(returnTo)}`} className={buttonVariants({ size: "lg" })} data-testid="signin">
            <LogIn aria-hidden />
            {t("action")}
          </a>
          <p className="flex items-start gap-2 text-xs text-muted-foreground">
            <ShieldCheck aria-hidden className="mt-0.5 size-4 shrink-0 text-primary" />
            {t("securityNote")}
          </p>
        </div>
      </section>
    </main>
  );
}
