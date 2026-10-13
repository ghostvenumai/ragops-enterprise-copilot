import type de from "./messages/de-DE.json";
import type { Locale } from "./src/i18n/config";

declare module "next-intl" {
  interface AppConfig {
    Locale: Locale;
    Messages: typeof de;
  }
}
