"use client";

import { useTranslations } from "next-intl";
import * as React from "react";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";

/** When the BFF reports an ended session, say so and offer a fresh sign-in at the same place. */
export function SessionExpiryWatcher() {
  const t = useTranslations("session");
  const [expired, setExpired] = React.useState(false);
  React.useEffect(() => {
    const onExpired = () => setExpired(true);
    window.addEventListener("ragops:session-expired", onExpired);
    return () => window.removeEventListener("ragops:session-expired", onExpired);
  }, []);
  const returnTo = typeof window === "undefined" ? "/" : `${window.location.pathname}${window.location.search}`;
  return (
    <Dialog open={expired} onOpenChange={() => undefined}>
      <DialogContent closeLabel={t("close")} data-testid="session-expired-dialog" onEscapeKeyDown={(event) => event.preventDefault()} onPointerDownOutside={(event) => event.preventDefault()}>
        <DialogHeader>
          <DialogTitle>{t("expiredTitle")}</DialogTitle>
          <DialogDescription>{t("expiredBody")}</DialogDescription>
        </DialogHeader>
        <DialogFooter>
          <Button asChild>
            <a href={`/auth/login?returnTo=${encodeURIComponent(returnTo)}`} data-testid="session-expired-signin">
              {t("signInAgain")}
            </a>
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
