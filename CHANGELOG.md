# Changelog

## 0.2.0.dev0 — ENT-13.1 privacy egress gate

- External model providers receive data only under a durable, tenant-specific grant for the
  exact purpose, provider, model and data classes (default deny; ADR 0021). Policies,
  revisions and decisions live in PostgreSQL (migration 0011, append-only evidence); the
  operator CLI `scripts/privacy_egress_admin.py` is the only write path, tenant admins get a
  read-only view.
- A precheck before the budget reservation and an authoritative check on the exact payload
  at the provider boundary; external adapters require a payload-bound, single-use permit.
  Denials are never rerouted or billed. The demo workflow is local-only.
- Permits are confirmed again against the policy store at dispatch (linearization point):
  a committed revocation, suspension, version change or expiry stops any new request.
- OpenAI Responses requests set `store=False`. Not zero data retention.
- New mandatory release gate `privacy_egress` (22 gates) and security control LIVE-16
  (33 controls). The historical paid-provider evidence (d9690cb) is no longer eligible; a
  separately authorized synthetic run is required before release.
- Copilot (DE/EN) explains the refusal "external processing not authorized".
- OCR: a PDF page that is a single scanned image is rendered at the scan's own resolution
  instead of being resampled to 300 dpi, which made Tesseract misread digits ("7" as "/" or
  "]"); strict eligibility, small text and every uncertain page keep the configured
  resolution, limits unchanged (ADR 0018, found in the ENT-13.1 operator closure).

## 0.2.0.dev0 — ENT-12-UI enterprise web frontend

- Added `apps/web`: Next.js 15 (App Router), React 19, TypeScript strict, Tailwind CSS,
  shadcn/ui-style components on Radix, Lucide, Recharts, TanStack Query and Zod contracts.
- Backend-for-Frontend: Authorization Code + PKCE, server-side sessions behind `__Host-`
  HttpOnly/Secure/SameSite cookies, token verification and refresh in the BFF, Origin +
  synchronizer CSRF token on every state change, allowlisted API proxy with `no-store`
  responses and sanitized errors, nonce CSP. FastAPI stays the authority (ADR 0020).
- Modules: Enterprise Cockpit, AI Copilot (idempotent questions, citations with pages,
  uncertainty and controlled refusals), Knowledge Base (uploads, versions, jobs, retry, OCR
  page reports), Document Intelligence (documents, creditors, claims with historical
  balances, deadlines, persons and aliases, findings, source provenance, versioned reviews
  with 409 conflicts), Monitoring, FinOps, Governance & Operations.
- Full de-DE / en-US internationalisation with `next-intl`, typed catalogs, parity and
  hardcoded-text tests; source evidence and model answers are never translated.
- New mandatory release gate `web_e2e` (21 gates) and security control LIVE-15 (32 controls).
- OCR fix found by the `ocr_ingestion` gate: small images are upscaled only when their text
  is small. The fractional 1.29x upscale of legible 1240 px pages made Tesseract misread
  digits and umlaut words at high confidence (ADR 0018); regression tests with the
  reproduced reference numbers.
- Release evidence integrity: `final-release-gate.json` no longer keeps statuses from the previous
  summary. Every mandatory gate is re-validated from its own evidence (gate name, commit, fresh
  timestamp, no exit/evidence contradiction); missing or stale evidence is BLOCKED, malformed or
  contradictory evidence FAIL. Only the paid provider gate may use an ancestor's PASS, and only
  while its paths are unchanged (docs/RELEASE_CANDIDATE.md).

## 0.2.0.dev0 — Phase 10H Qdrant live gate

- Added a disposable, isolated host-side Qdrant RC gate using the production
  `VectorIndex` adapter and deterministic synthetic embeddings.
- Enforced both vector validity-window boundaries inside Qdrant filters.
- Added dynamic loopback port discovery and sanitized per-gate evidence.

## 0.2.0.dev0 — Phase 2 identity boundary

- Added immutable authenticated user context and provider abstraction.
- Added development identity restricted to non-production environments.
- Added provider-neutral asymmetric OIDC/JWT validation with configurable
  issuer, audience, tenant and role claims.
- Added FastAPI bearer authentication and tenant-scoped admin dependencies.
- OIDC routes ignore client-supplied identity fields; legacy selectors remain
  development/demo compatibility only.
- Added negative JWT, configuration, admin and tenant-override tests.

Production remains blocked pending persistent API integration, workers, Qdrant,
OIDC deployment key management and release gates.

## 0.2.0.dev0 — Phase 3 knowledge management

- Added tenant-owned workspace, collection, document and document-version metadata.
- Added explicit lifecycle transitions, secure local blob storage, MIME/size/hash
  validation, duplicate detection and historical version preservation.
- Added tenant-scoped management service, workspace/collection/upload/version and
  reindex API contracts, and Knowledge Base workspace/collection UI controls.
- Added Phase 3 migration, isolation/security tests and machine-readable evidence.

## 0.2.0.dev0 — Phase 4 asynchronous ingestion

- Added explicit ingestion job status, stages, retry metadata and timestamps.
- Added deterministic and Redis queue adapters plus a bounded idempotent worker.
- Uploads now create and enqueue a job and return `202 Accepted` with job IDs.
- Added job status/list/cancel APIs, processing status UI, async configuration,
  Compose Redis/worker preparation and Phase 4 evidence.

## Unreleased

## 0.2.0.dev0 — Phase 5 vector persistence

- Added `VectorIndex` abstraction with deterministic and Qdrant adapters.
- Added deterministic vector IDs, payload schema, mandatory tenant/RBAC/lifecycle
  filters, strict dimensions and explicit version/document deletion operations.
- Added embedding provider boundary, Qdrant configuration validation, vector
  contract/security tests and Phase 5 evidence.

- Hardened prompt-injection detection: added multilingual phrase patterns
  (EN/DE/FR/ES), jailbreak/role-play and system-prompt-extraction patterns,
  hidden-document-dump and security-bypass patterns, compact matching against
  letter-spacing and leet-speak evasion, invisible/bidi character stripping,
  HTML-comment scanning, and recursive base64 payload decoding.
- Added 39 prompt-injection regression tests covering every attack class and
  benign-text false-positive checks; the external audit probe now passes with
  detection, abstention, and zero foreign citations on all cases.
- Documented the layered injection defense and its honest limits in
  `docs/SECURITY_NOTES.md`.
- Silenced the five informational Bandit findings (B404/B603) with explicit
  per-line `nosec` justifications; Bandit now reports zero findings at any
  severity.

- Replaced text-decoding placeholders for PDF and DOCX ingestion with real
  binary format parsers (`pypdf`, `python-docx`).
- Regenerated the synthetic PDF and DOCX fixtures as real PDF 1.3 and OOXML
  `.docx` files with identical front matter and body content, so ingestion,
  retrieval, and evaluation behavior is unchanged.
- Added dedicated loader unit tests covering PDF extraction, DOCX extraction,
  suffix dispatch, and unsupported-suffix rejection.
- Updated `docs/ASSUMPTIONS.md` to reflect real binary parsing instead of the
  previous text-layer-decoding placeholder.

## 0.3.1 - 2026-08-11

- Added a fail-closed `VERIFY_NARRATION` phase before TTS cache or provider access.
- Bound every German narration sentence to concrete repository evidence and visible demo terms.
- Added machine-readable narration QA reports for claims, code, demo alignment, timing, and hype.
- Replaced fixed scene trimming with validated audio duration plus explicit pre/post pauses.
- Added bounded FFmpeg video/audio crossfades between scenes.
- Changed subtitles to a synchronized sidecar by default with explicit optional burn-in.
- Corrected two narration claims that overstated incremental indexing and injection refusal behavior.
- Added focused narration, timing, transition, and subtitle regression tests.

## 0.3.0 - 2026-08-10

- Added a persistent master state machine from application discovery through video QA.
- Added bounded transient retries, atomic state, machine-readable history, resume, and dry run.
- Added a real deterministic demo controller and allowlisted dashboard recording scenes.
- Added a German 150-second timeline, narration, SRT generation, headless capture, and FFmpeg rendering.
- Added persistent content-addressable OpenAI TTS caching with per-scene invalidation.
- Added FFprobe validation, atomic cache/manifest writes, file locks, secret redaction, and lazy credentials.
- Added dry-run, cache-only, force, API-call and retry limits plus five-build cost-control simulations.
- Added H.264/AAC, 1080p, frame-rate, duration, full-decode, and sample-frame video QA.
- Extended MyPy, Ruff, Bandit, tests, documentation, and security checks to automation and video code.

## 0.2.1 - 2026-08-04

- Localized the complete Streamlit user interface to German.
- Kept established RAG, LLM, Retrieval, Recall, and Precision terminology.
- Updated UI tests, demo instructions, and visual validation evidence.
- Added source-bound German response composition for deterministic RAG answers.
- Added German CRM evidence and regression coverage against raw English chunk output.
- Expanded the synthetic knowledge base from 11 to 20 documents across three tenants.
- Added Unicode-aware lexical evidence gating and controlled no-evidence refusal.
- Restricted API citations to sources actually used by the composed answer.
- Expanded the executable gold dataset from 25 to 28 cases.

## 0.2.0 - 2026-08-03

- Replaced the static evidence dashboard with a modern chat-first copilot workspace.
- Added cited answers, response telemetry, prompt suggestions, and session reset.
- Added tenant-filtered knowledge-base, observability, and governance views.
- Connected Streamlit to FastAPI over the Compose application network.
- Changed evaluation and audit endpoints to return structured dashboard data.
- Added dashboard client tests and visual desktop/mobile validation evidence.
- Fixed make build so Compose-profiled services are included.
- Made the classic isolated Docker builder the reproducible host default.
- Documented GUI architecture, identity boundaries, operation, and demo flow.

## 0.1.0

- Initial portfolio project foundation.
- Added controlled autonomous loop design.
- Added deterministic local RAG architecture.
# 0.2.0.dev0

- ENT-07: added tenant budgets, quota policy foundation, budget alerts, deterministic preflight decisions and forecast service.
- ENT-10: added verification-first release-candidate gate and machine-readable blocked-gate evidence.
- ENT-06: added deterministic model catalog, tenant policy constraints, cost-aware routing, bounded provider fallback and normalized usage accounting.
- Added protected model/provider administration and routing simulation endpoints.
