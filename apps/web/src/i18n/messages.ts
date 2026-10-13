import de from "../../messages/de-DE.json";
import en from "../../messages/en-US.json";
import type { Locale } from "./config";

export type Messages = typeof de;

/** en-US is checked against de-DE at compile time; a missing key is a type error. */
const english: Messages = en;

export function messagesFor(locale: Locale): Messages {
  return locale === "en-US" ? english : de;
}
