import { formatDate, formatDecimal, formatMoney, formatNumber } from "@/lib/format";
import { parseMetrics } from "@/lib/metrics";

describe("exact decimal formatting", () => {
  it("formats German and English separators without floating point", () => {
    expect(formatDecimal("1234567.5", "de-DE")).toBe("1.234.567,50");
    expect(formatDecimal("1234567.5", "en-US")).toBe("1,234,567.50");
    // 0.1 + 0.2 style artefacts cannot occur: digits are copied, not computed.
    expect(formatDecimal("9007199254740993.01", "en-US")).toBe("9,007,199,254,740,993.01");
  });

  it("places the currency according to the locale", () => {
    expect(formatMoney("1234.50", "EUR", "de-DE").replace(/\s/g, " ")).toBe("1.234,50 €");
    expect(formatMoney("1234.50", "EUR", "en-US")).toBe("€1,234.50");
    expect(formatMoney("12.00", null, "de-DE")).toBe("12,00");
  });

  it("returns unknown shapes unchanged instead of guessing", () => {
    expect(formatDecimal("abc", "de-DE")).toBe("abc");
  });

  it("formats dates per locale without shifting the day", () => {
    expect(formatDate("2026-03-01", "de-DE")).toBe("01.03.2026");
    expect(formatDate("2026-03-01", "en-US")).toBe("03/01/2026");
    expect(formatDate(null, "de-DE")).toBe("—");
  });

  it("formats integers per locale", () => {
    expect(formatNumber(12345, "de-DE")).toBe("12.345");
    expect(formatNumber(12345, "en-US")).toBe("12,345");
  });
});

describe("metrics parser", () => {
  it("reads Prometheus lines and ignores comments and garbage", () => {
    expect(parseMetrics("# HELP x\nragops_request_count 3\nragops_ingestion_queue_depth -1\nbad line\n")).toEqual({
      ragops_request_count: 3,
      ragops_ingestion_queue_depth: -1,
    });
  });
});
