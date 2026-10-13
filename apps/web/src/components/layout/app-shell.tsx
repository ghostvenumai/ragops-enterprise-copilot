"use client";

import { LogOut, Menu, UserRound } from "lucide-react";
import { useTranslations } from "next-intl";
import Link from "next/link";
import { usePathname } from "next/navigation";
import * as React from "react";
import { useSession } from "@/components/providers/session-context";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogTitle } from "@/components/ui/dialog";
import { cn } from "@/lib/utils";
import { BrandMark } from "./brand";
import { LanguageSwitcher } from "./language-switcher";
import { NAV_ITEMS, isActive } from "./navigation";
import { SessionExpiryWatcher } from "./session-expiry";

function NavList({ onNavigate }: { onNavigate?: () => void }) {
  const t = useTranslations("nav");
  const pathname = usePathname();
  const { roles } = useSession();
  return (
    <ul className="space-y-1">
      {NAV_ITEMS.filter((item) => item.visible(roles)).map((item) => {
        const active = isActive(item.href, pathname);
        const Icon = item.icon;
        return (
          <li key={item.key}>
            <Link
              href={item.href}
              onClick={onNavigate}
              aria-current={active ? "page" : undefined}
              data-testid={`nav-${item.key}`}
              className={cn(
                "group flex items-center gap-3 rounded-md px-3 py-2 text-sm font-medium transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sidebar-active",
                active
                  ? "bg-sidebar-accent text-white"
                  : "text-sidebar-muted hover:bg-sidebar-accent/70 hover:text-sidebar-foreground",
              )}
            >
              <Icon aria-hidden className={cn("size-[18px]", active ? "text-sidebar-active" : "text-sidebar-muted group-hover:text-sidebar-foreground")} />
              {t(item.key)}
            </Link>
          </li>
        );
      })}
    </ul>
  );
}

function SidebarBody({ onNavigate }: { onNavigate?: () => void }) {
  const t = useTranslations("shell");
  return (
    <div className="flex h-full flex-col bg-sidebar text-sidebar-foreground">
      <div className="flex items-center gap-2.5 border-b border-sidebar-border px-5 py-4">
        <BrandMark className="size-8" />
        <div className="leading-tight">
          <p className="text-sm font-semibold tracking-tight text-white">RAGOps</p>
          <p className="text-xs text-sidebar-muted">{t("productLine")}</p>
        </div>
      </div>
      <nav aria-label={t("mainNavigation")} className="flex-1 overflow-y-auto px-3 py-4">
        <NavList onNavigate={onNavigate} />
      </nav>
      <div className="border-t border-sidebar-border px-5 py-3 text-xs text-sidebar-muted">{t("syntheticNotice")}</div>
    </div>
  );
}

function LogoutButton() {
  const t = useTranslations("shell");
  const { csrfToken } = useSession();
  const [busy, setBusy] = React.useState(false);
  async function logout() {
    setBusy(true);
    try {
      const response = await fetch("/auth/logout", {
        method: "POST",
        headers: { "x-csrf-token": csrfToken },
        credentials: "same-origin",
      });
      const payload = (await response.json().catch(() => ({}))) as { redirect?: string };
      window.location.assign(payload.redirect ?? "/signin?signedOut=1");
    } catch {
      window.location.assign("/signin?signedOut=1");
    }
  }
  return (
    <Button variant="ghost" size="sm" onClick={logout} disabled={busy} data-testid="logout">
      <LogOut aria-hidden />
      <span className="hidden sm:inline">{t("signOut")}</span>
      <span className="sr-only sm:hidden">{t("signOut")}</span>
    </Button>
  );
}

export function AppShell({ children }: { children: React.ReactNode }) {
  const t = useTranslations("shell");
  const tRoles = useTranslations("roles");
  const session = useSession();
  const [open, setOpen] = React.useState(false);
  const menuButton = React.useRef<HTMLButtonElement>(null);
  const pathname = usePathname();
  React.useEffect(() => {
    setOpen(false);
  }, [pathname]);

  return (
    <div className="min-h-screen lg:grid lg:grid-cols-[256px_1fr]">
      <a href="#main" className="sr-only z-50 rounded bg-primary px-3 py-2 text-primary-foreground focus:not-sr-only focus:fixed focus:left-3 focus:top-3">
        {t("skipToContent")}
      </a>
      <aside className="sticky top-0 hidden h-screen lg:block" data-testid="sidebar">
        <SidebarBody />
      </aside>
      <Dialog open={open} onOpenChange={setOpen}>
        <DialogContent
          side="left"
          closeLabel={t("closeMenu")}
          className="bg-sidebar text-sidebar-foreground"
          aria-describedby={undefined}
          onCloseAutoFocus={(event) => {
            // The dialog has no trigger element of its own: return focus to the menu button.
            event.preventDefault();
            menuButton.current?.focus();
          }}
        >
          <DialogTitle className="sr-only">{t("mainNavigation")}</DialogTitle>
          <DialogDescription className="sr-only">{t("mainNavigation")}</DialogDescription>
          <SidebarBody onNavigate={() => setOpen(false)} />
        </DialogContent>
      </Dialog>
      <div className="flex min-w-0 flex-col">
        <header className="sticky top-0 z-30 flex h-14 items-center gap-2 border-b bg-card/95 px-3 backdrop-blur sm:px-5">
          <Button ref={menuButton} variant="ghost" size="icon" className="lg:hidden" onClick={() => setOpen(true)} aria-label={t("openMenu")} aria-expanded={open} data-testid="open-menu">
            <Menu aria-hidden />
          </Button>
          <div className="flex min-w-0 items-center gap-2 lg:hidden">
            <BrandMark className="size-7" />
            <span className="truncate text-sm font-semibold">RAGOps</span>
          </div>
          <div className="ml-auto flex items-center gap-1 sm:gap-3">
            <Badge tone="brand" className="hidden md:inline-flex" data-testid="tenant-badge" data-tenant={session.tenantId} title={t("tenant")}>
              <span className="sr-only">{t("tenant")}: </span>
              {session.tenantId}
            </Badge>
            <LanguageSwitcher />
            <div className="hidden items-center gap-2 border-l pl-3 sm:flex" data-testid="user-info">
              <UserRound aria-hidden className="size-4 text-muted-foreground" />
              <div className="leading-tight">
                <p className="max-w-[12rem] truncate text-sm font-medium">{session.name ?? session.email ?? t("unknownUser")}</p>
                <p className="text-xs text-muted-foreground">
                  {session.roles.length ? session.roles.map((role) => (tRoles.has(role as never) ? tRoles(role as never) : role)).join(", ") : t("noRole")}
                </p>
              </div>
            </div>
            <LogoutButton />
          </div>
        </header>
        <main id="main" tabIndex={-1} className="flex-1 px-4 py-6 sm:px-6 lg:px-8">
          <div className="mx-auto w-full max-w-7xl">{children}</div>
        </main>
      </div>
      <SessionExpiryWatcher />
    </div>
  );
}
