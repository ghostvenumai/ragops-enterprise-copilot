"use client";

import { Languages } from "lucide-react";
import { useLocale, useTranslations } from "next-intl";
import { useRouter } from "next/navigation";
import * as React from "react";
import { LOCALES, LOCALE_COOKIE, LOCALE_COOKIE_MAX_AGE, type Locale } from "@/i18n/config";
import { cn } from "@/lib/utils";

/** Stores only the UI language; tenant, session and data stay exactly as they are. */
export function writeLocaleCookie(locale: Locale): void {
  const secure = window.location.protocol === "https:" ? "; secure" : "";
  document.cookie = `${LOCALE_COOKIE}=${locale}; path=/; max-age=${LOCALE_COOKIE_MAX_AGE}; samesite=lax${secure}`;
}

export function LanguageSwitcher({ className, tone = "light" }: { className?: string; tone?: "light" | "dark" }) {
  const t = useTranslations("language");
  const current = useLocale();
  const router = useRouter();
  const [pending, startTransition] = React.useTransition();

  function choose(locale: Locale) {
    if (locale === current) return;
    writeLocaleCookie(locale);
    startTransition(() => router.refresh());
  }

  return (
    <div role="group" aria-label={t("label")} className={cn("flex items-center gap-1", className)} aria-busy={pending}>
      <Languages aria-hidden className={cn("size-4", tone === "dark" ? "text-sidebar-muted" : "text-muted-foreground")} />
      {LOCALES.map((locale) => {
        const active = locale === current;
        return (
          <button
            key={locale}
            type="button"
            lang={locale}
            onClick={() => choose(locale)}
            aria-pressed={active}
            aria-label={t(locale === "de-DE" ? "switchToGerman" : "switchToEnglish")}
            data-testid={`locale-${locale}`}
            className={cn(
              "rounded px-2 py-1 text-xs font-semibold transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
              active
                ? "bg-primary text-primary-foreground"
                : tone === "dark"
                  ? "text-sidebar-foreground hover:bg-sidebar-accent"
                  : "text-foreground hover:bg-muted",
            )}
          >
            {locale === "de-DE" ? "DE" : "EN"}
          </button>
        );
      })}
    </div>
  );
}
