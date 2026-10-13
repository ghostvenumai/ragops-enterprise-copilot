import { z } from "zod";

/*
 * Zod contracts for the FastAPI responses the UI reads, written against the routes in
 * src/ragops/api/app.py and src/ragops/api/intelligence.py. Objects are not strict so an
 * additive backend field never breaks the UI; a missing or mistyped field does.
 */
const id = z.string().min(1);
const nullableString = z.string().nullable();
/** Decimal amounts travel as strings ("1234.50"); they are never parsed into floats for display. */
export const decimalString = z.string().regex(/^-?\d+(\.\d+)?$/);

export const healthSchema = z.object({ status: z.string() });

export const readinessSchema = z.object({
  status: z.enum(["ready", "unready", "shutting_down"]),
  components: z.record(
    z.string(),
    z.object({ critical: z.boolean(), status: z.string(), reason: z.string() }),
  ),
  documents: z.number().int().optional(),
});
export type Readiness = z.infer<typeof readinessSchema>;

// ------------------------------------------------------------------ query
export const citationSchema = z.object({
  source_id: z.string(),
  title: z.string(),
  tenant_id: z.string(),
  score: z.number(),
  document_id: nullableString.optional(),
  document_version_id: nullableString.optional(),
  chunk_id: nullableString.optional(),
  page_number: z.number().int().nullable().optional(),
});
export type Citation = z.infer<typeof citationSchema>;

export const queryResponseSchema = z.object({
  answer: z.string(),
  abstained: z.boolean(),
  evidence_score: z.number(),
  citations: z.array(citationSchema),
  metrics: z.object({
    tokens: z.number().int(),
    estimated_cost_eur: z.number(),
    retrieval_latency_ms: z.number(),
    llm_latency_ms: z.number(),
    prompt_injection_detected: z.boolean(),
  }),
  correlation_id: z.string(),
});
export type QueryResponse = z.infer<typeof queryResponseSchema>;

export const queryRequestSchema = z
  .object({ question: z.string().trim().min(1).max(4000), top_k: z.number().int().min(1).max(20) })
  .strict();

// ------------------------------------------------------------------ knowledge
export const workspaceSchema = z.object({ id, tenant_id: z.string(), name: z.string(), slug: z.string(), status: z.string() });
export const collectionSchema = z.object({
  id,
  workspace_id: id,
  name: z.string(),
  access_level: z.string(),
  active: z.boolean(),
});
export const uploadAcceptedSchema = z.object({ document_id: id, version_id: id, job_id: id, status: z.string() });
export const versionSchema = z.object({
  id,
  version: z.number().int(),
  filename: z.string(),
  content_hash: z.string(),
  size: z.number().int(),
  ingestion_status: z.string(),
});
export const jobSchema = z.object({
  job_id: id,
  status: z.string(),
  stage: z.string().nullable(),
  progress: z.number().nullable(),
  document_id: id,
  version_id: id,
  error_code: nullableString,
  title: nullableString,
  filename: nullableString,
  review_required: z.boolean(),
  pages_total: z.number().int(),
  pages_review: z.number().int(),
});
export type Job = z.infer<typeof jobSchema>;
export const jobActionSchema = z.object({ job_id: id, status: z.string() });

export const pageReportSchema = z.object({
  document_id: id,
  version_id: id,
  ingestion_status: z.string(),
  review_required: z.boolean(),
  pages: z.array(
    z.object({
      page_number: z.number().int(),
      classification: nullableString,
      extraction_method: nullableString,
      quality_state: nullableString,
      mean_confidence: z.number().nullable(),
      char_count: z.number().int().nullable(),
      word_count: z.number().int().nullable(),
      flags: z.array(z.string()),
      engine: nullableString.optional(),
      language: nullableString.optional(),
      line_count: z.number().int().optional(),
    }),
  ),
});
export type PageReport = z.infer<typeof pageReportSchema>;

// ------------------------------------------------------------------ intelligence
export const intelligenceDocumentSchema = z.object({
  document_id: id,
  version_id: id,
  title: z.string(),
  classification: nullableString,
  classification_status: z.string(),
  debtor_status: z.string(),
  review_required: z.boolean(),
  analysis_status: z.string(),
  review_version: z.number().int().nullable(),
});
export type IntelligenceDocument = z.infer<typeof intelligenceDocumentSchema>;

export const effectiveValueSchema = z.object({
  origin: z.enum(["document", "rule", "user", "rejected"]),
  text: nullableString.optional(),
  amount: decimalString.nullable().optional(),
  currency: nullableString.optional(),
  date: nullableString.optional(),
});

export const factSchema = z.object({
  id,
  document_version_id: id,
  fact_type: z.string(),
  category: z.string(),
  assertion: z.string(),
  value: nullableString,
  amount: decimalString.nullable(),
  currency: nullableString,
  date: nullableString,
  as_of: nullableString,
  page_number: z.number().int().nullable(),
  quality_state: z.string(),
  review_state: z.string(),
  review_version: z.number().int(),
  extractor_version: z.string(),
  effective: effectiveValueSchema,
});
export type Fact = z.infer<typeof factSchema>;

export const factDetailSchema = factSchema.extend({
  source: z.object({
    document_version_id: id,
    document_id: id,
    title: z.string(),
    page_number: z.number().int().nullable(),
    line: z.number().int().nullable(),
    char_start: z.number().int().nullable(),
    char_end: z.number().int().nullable(),
    raw_value: nullableString,
    chunk_index: z.number().int().nullable(),
    extraction_method: nullableString,
    ocr_line: z.number().int().nullable(),
    regions: z.array(z.unknown()),
    coordinate_space: nullableString,
  }),
});
export type FactDetail = z.infer<typeof factDetailSchema>;

export const intelligenceDocumentDetailSchema = intelligenceDocumentSchema.extend({
  ignored_instruction_lines: z.number().int(),
  extractor_version: nullableString,
  classifications: z.array(
    z.object({
      id,
      document_class: z.string(),
      evidence_strength: z.string(),
      rank: z.number().int(),
      is_primary: z.boolean(),
      evidence: z.array(z.unknown()),
      taxonomy_version: z.string(),
      review_state: z.string(),
      review_version: z.number().int(),
    }),
  ),
  facts: z.array(factSchema),
  persons: z.array(
    z.object({
      id,
      person_id: id,
      role: z.string(),
      state: z.string(),
      origin: z.string(),
      basis: z.array(z.unknown()),
      review_version: z.number().int(),
    }),
  ),
  claim_groups: z.array(
    z.object({ claim_group_id: id, member_id: id, membership: z.string(), status: z.string(), basis: z.array(z.unknown()) }),
  ),
});
export type IntelligenceDocumentDetail = z.infer<typeof intelligenceDocumentDetailSchema>;

export const creditorSchema = z.object({
  creditor: z.string(),
  roles: z.array(z.string()),
  documents: z.array(z.object({ document_id: id, fact_id: id, assertion: z.string() })),
});

export const deadlineSchema = z.object({
  fact_id: id,
  document_id: id,
  title: z.string(),
  fact_type: z.string(),
  date: nullableString,
  page_number: z.number().int().nullable(),
  review_state: z.string(),
  quality_state: z.string(),
});

export const findingSchema = z.object({
  kind: z.string(),
  id,
  document_id: id.optional(),
  fact_type: z.string().optional(),
  reason: z.string(),
});
export type Finding = z.infer<typeof findingSchema>;

export const personSchema = z.object({ id, label: z.string(), status: z.string(), alias_count: z.number().int() });

const claimAmountSchema = z.object({
  fact_id: id,
  fact_type: z.string(),
  amount: decimalString,
  currency: nullableString,
  as_of: nullableString,
  role: z.enum(["payment", "unclassified", "potentially_current", "component", "historical"]),
  review_state: z.string(),
});

export const claimSchema = z.object({
  claim_group_id: id,
  status: z.string(),
  review_version: z.number().int(),
  creditor: z.string(),
  identifier_type: z.string(),
  identifier: z.string(),
  documents: z.array(
    z.object({
      member_id: id,
      document_id: id,
      version_id: id,
      title: z.string(),
      membership: z.string(),
      review_version: z.number().int(),
      document_class: nullableString,
      issue_date: nullableString,
      amounts: z.array(claimAmountSchema),
    }),
  ),
  potentially_current: z
    .object({
      amount: decimalString,
      currency: nullableString,
      as_of: z.string(),
      fact_id: id,
      document_id: id,
      basis: z.enum(["latest_stated_balance", "latest_principal_only"]),
    })
    .nullable(),
  settled: z.boolean(),
  resolved: z.boolean(),
  unresolved_reasons: z.array(z.string()),
});
export type Claim = z.infer<typeof claimSchema>;

export const claimsSchema = z.object({
  claims: z.array(claimSchema),
  unresolved: z.array(
    z.object({ document_id: id.optional(), claim_group_id: id.optional(), title: z.string().optional(), reason: z.string() }),
  ),
  complete: z.boolean(),
  consolidated_total: z.record(z.string(), decimalString).nullable(),
  resolved_subtotal: z.record(z.string(), decimalString),
  aggregation_rule: z.string(),
  person_id: nullableString,
});
export type Claims = z.infer<typeof claimsSchema>;

export const REVIEW_ENTITY_TYPES = ["fact", "classification", "person_link", "claim_group", "claim_member", "document"] as const;
export const REVIEW_ACTIONS = ["confirm", "reject", "correct", "dismiss", "mark_for_review", "clear_review_flag", "assign_person"] as const;
export const REASON_CATEGORIES = [
  "verified_against_source",
  "extraction_error",
  "wrong_party",
  "not_same_claim",
  "duplicate",
  "not_a_claim",
  "needs_follow_up",
  "other",
] as const;

/** Mirrors ReviewRequest (extra="forbid"): the client can never send a source or raw value. */
export const reviewRequestSchema = z
  .object({
    entity_type: z.enum(REVIEW_ENTITY_TYPES),
    entity_id: z.string().min(1).max(64),
    action: z.enum(REVIEW_ACTIONS),
    expected_version: z.number().int().min(1).max(1_000_000),
    reason_category: z.enum(REASON_CATEGORIES),
    value: z
      .object({
        amount: z.string().regex(/^\d{1,13}(\.\d{1,2})?$/).optional(),
        currency: z.string().regex(/^[A-Z]{3}$/).optional(),
        date: z.string().regex(/^\d{4}-\d{2}-\d{2}$/).optional(),
        text: z.string().min(1).max(255).optional(),
      })
      .strict()
      .optional(),
    person_id: z.string().max(64).optional(),
    role: z.enum(["debtor", "addressee"]).optional(),
  })
  .strict();
export type ReviewRequest = z.infer<typeof reviewRequestSchema>;
export const reviewResultSchema = z.object({ review_version: z.number().int() }).passthrough();

export const personCreateSchema = z.object({ label: z.string().trim().min(1).max(120) }).strict();
export const aliasCreateSchema = z.object({ kind: z.enum(["name", "identifier"]), value: z.string().trim().min(2).max(255) }).strict();

// ------------------------------------------------------------------ governance / finops
export const auditEventSchema = z.object({
  event_id: z.string(),
  correlation_id: z.string(),
  event_type: z.string(),
  tenant_id: z.string(),
  user_id: z.string(),
  timestamp: z.string(),
  outcome: z.string(),
  details: z.record(z.string(), z.union([z.string(), z.number(), z.boolean(), z.null()])).optional(),
});
export type AuditEvent = z.infer<typeof auditEventSchema>;

export const costSummarySchema = z.object({
  request_count: z.number().int(),
  total_tokens: z.number().int(),
  total_cost_eur: z.number(),
});
export const providerSchema = z.object({ provider_id: z.string(), enabled: z.boolean(), health: z.string() });
export const modelSchema = z.object({
  provider_id: z.string(),
  model_id: z.string(),
  display_name: z.string(),
  enabled: z.boolean(),
  routing_tier: z.string(),
  high_risk_allowed: z.boolean(),
});
export const modelPolicySchema = z.record(z.string(), z.unknown());
export const budgetSchema = z.object({
  id: z.string(),
  tenant_id: z.string(),
  budget_amount: z.number(),
  currency: z.string(),
  enforcement_mode: z.string(),
});
export const budgetCreateSchema = z
  .object({
    budget_amount: z.number().positive().max(1_000_000_000),
    currency: z.literal("EUR"),
    enforcement_mode: z.enum(["monitor", "optimize", "hard_limit"]),
  })
  .strict();
export const policySimulationSchema = z.object({
  decision: z.string(),
  state: z.string(),
  reason_codes: z.array(z.string()),
  estimated_post_request_spend: z.string(),
});
export const routeSimulationSchema = z.object({
  routing_class: z.string(),
  provider_id: z.string(),
  model_id: z.string(),
  reason_codes: z.array(z.string()),
  estimated_cost: z.number(),
  fallback_chain: z.array(z.unknown()),
});
export const evaluationSchema = z
  .object({ status: z.string() })
  .passthrough();

export const sessionSchema = z.object({
  authenticated: z.literal(true),
  user: z.object({ name: nullableString, email: nullableString, tenantId: z.string(), roles: z.array(z.string()) }),
  csrfToken: z.string(),
  accessExpiresAt: z.number(),
  identityProvider: z.enum(["available", "unavailable"]),
});
export type SessionInfo = z.infer<typeof sessionSchema>;

export const apiErrorSchema = z.object({ error: z.string(), detail: z.string().optional() });
