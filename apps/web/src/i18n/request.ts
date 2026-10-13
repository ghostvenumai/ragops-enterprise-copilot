import { cookies } from "next/headers";
import { getRequestConfig } from "next-intl/server";
import { LOCALE_COOKIE, resolveLocale } from "./config";
import { messagesFor } from "./messages";

/** Locale from the preference cookie only: no URL prefix, so switching never changes routes. */
export default getRequestConfig(async () => {
  const jar = await cookies();
  const locale = resolveLocale(jar.get(LOCALE_COOKIE)?.value);
  return {
    locale,
    messages: messagesFor(locale),
    timeZone: "Europe/Berlin",
    formats: {
      dateTime: {
        short: { day: "2-digit", month: "2-digit", year: "numeric" },
        long: { day: "2-digit", month: "short", year: "numeric", hour: "2-digit", minute: "2-digit" },
      },
      number: {
        precise: { maximumFractionDigits: 3 },
        percent: { style: "percent", maximumFractionDigits: 1 },
      },
    },
  };
});
