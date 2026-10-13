import { claimsSchema, factSchema, queryResponseSchema, reviewRequestSchema } from "@/lib/api/schemas";

describe("API contracts", () => {
  it("rejects client-supplied source references, pages or raw values in reviews", () => {
    const base = { entity_type: "fact", entity_id: "f", action: "confirm", expected_version: 1, reason_category: "other" };
    expect(reviewRequestSchema.safeParse(base).success).toBe(true);
    for (const extra of [{ source: { page_number: 1 } }, { raw_value: "x" }, { page_number: 2 }, { tenant_id: "tenant-beta" }]) {
      expect(reviewRequestSchema.safeParse({ ...base, ...extra }).success).toBe(false);
    }
    expect(reviewRequestSchema.safeParse({ ...base, value: { amount: "-1.00", currency: "EUR" } }).success).toBe(false);
    expect(reviewRequestSchema.safeParse({ ...base, value: { amount: "1.005", currency: "EUR" } }).success).toBe(false);
    expect(reviewRequestSchema.safeParse({ ...base, expected_version: 0 }).success).toBe(false);
  });

  it("keeps amounts as exact decimal strings", () => {
    const fact = {
      id: "f",
      document_version_id: "v",
      fact_type: "stated_remaining_balance",
      category: "amount",
      assertion: "document_stated",
      value: null,
      amount: "1234.50",
      currency: "EUR",
      date: null,
      as_of: null,
      page_number: 1,
      quality_state: "accepted",
      review_state: "unreviewed",
      review_version: 1,
      extractor_version: "x",
      effective: { origin: "document", text: null, amount: "1234.50", currency: "EUR", date: null },
    };
    expect(factSchema.parse(fact).amount).toBe("1234.50");
    expect(factSchema.safeParse({ ...fact, amount: 1234.5 }).success).toBe(false);
  });

  it("accepts a claims summary without a consolidated total when unresolved", () => {
    const parsed = claimsSchema.parse({
      claims: [],
      unresolved: [{ document_id: "d", title: "t", reason: "no_claim_identifier" }],
      complete: false,
      consolidated_total: null,
      resolved_subtotal: {},
      aggregation_rule: "latest_stated_balance_per_resolved_claim_v1",
      person_id: null,
    });
    expect(parsed.consolidated_total).toBeNull();
  });

  it("refuses a query response missing citations", () => {
    expect(queryResponseSchema.safeParse({ answer: "a", abstained: false, evidence_score: 1, metrics: {}, correlation_id: "c" }).success).toBe(false);
  });
});
