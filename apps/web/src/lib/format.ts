/*
 * Locale-aware formatting. Amounts arrive as decimal strings and are formatted digit by digit,
 * never through binary floating point, so the displayed value is exactly the stated value.
 */

function separators(locale: string): { group: string; decimal: string } {
  const parts = new Intl.NumberFormat(locale).formatToParts(12345.6);
  return {
    group: parts.find((part) => part.type === "group")?.value ?? ",",
    decimal: parts.find((part) => part.type === "decimal")?.value ?? ".",
  };
}

export function formatDecimal(value: string, locale: string, minimumFractionDigits = 2): string {
  const match = /^(-?)(\d+)(?:\.(\d+))?$/.exec(value.trim());
  if (!match) return value;
  const [, sign = "", whole = "0", fraction = ""] = match;
  const { group, decimal } = separators(locale);
  const grouped = whole.replace(/\B(?=(\d{3})+(?!\d))/g, group);
  const digits = fraction.padEnd(minimumFractionDigits, "0");
  return `${sign}${grouped}${digits ? `${decimal}${digits}` : ""}`;
}

export function formatMoney(value: string, currency: string | null | undefined, locale: string): string {
  const number = formatDecimal(value, locale);
  if (!currency || !/^[A-Z]{3}$/.test(currency)) return number;
  try {
    const parts = new Intl.NumberFormat(locale, { style: "currency", currency }).formatToParts(1);
    let numberPlaced = false;
    return parts
      .map((part) => {
        if (["integer", "group", "decimal", "fraction"].includes(part.type)) {
          if (numberPlaced) return "";
          numberPlaced = true;
          return number.replace(/^-/, "");
        }
        return part.value;
      })
      .join("")
      .replace(/^/, number.startsWith("-") ? "-" : "");
  } catch {
    return `${number} ${currency}`;
  }
}

export function formatNumber(value: number, locale: string, maximumFractionDigits = 0): string {
  return new Intl.NumberFormat(locale, { maximumFractionDigits }).format(value);
}

export function formatEuroFloat(value: number, locale: string): string {
  // Only for the API's aggregate float counters (costs/summary), never for document amounts.
  return new Intl.NumberFormat(locale, { style: "currency", currency: "EUR", maximumFractionDigits: 4 }).format(value);
}

export function formatDate(value: string | null | undefined, locale: string): string {
  if (!value) return "—";
  const date = /^\d{4}-\d{2}-\d{2}$/.test(value) ? new Date(`${value}T00:00:00Z`) : new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat(locale, { day: "2-digit", month: "2-digit", year: "numeric", timeZone: "UTC" }).format(date);
}

export function formatDateTime(value: string | null | undefined, locale: string): string {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat(locale, {
    day: "2-digit",
    month: "2-digit",
    year: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    timeZone: "Europe/Berlin",
  }).format(date);
}

export function formatMs(value: number, locale: string): string {
  return `${new Intl.NumberFormat(locale, { maximumFractionDigits: 0 }).format(value)} ms`;
}

export function formatPercent(value: number, locale: string): string {
  return new Intl.NumberFormat(locale, { style: "percent", maximumFractionDigits: 1 }).format(value);
}

export function shortId(value: string): string {
  return value.length > 8 ? value.slice(0, 8) : value;
}
