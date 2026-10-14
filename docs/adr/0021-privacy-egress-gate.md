# ADR 0021: Privacy egress gate for external model providers (ENT-13.1)

## Status

Accepted (ENT-13.1). Addresses ENT-13 audit findings P01 (complete context and question sent
unmasked), P03 (no enforceable local-only policy) and the request-level part of P02
(`store`). Retention and deletion (P04) and better PII detection (P05) are not addressed
here.

## Context

The productive query path passed the user question and the full retrieved context (up to 20
chunks, about 6000 tokens of document text) to the configured provider. Masking applied only
to the 260-character citation snippets shown to the user, and the regex masking covers e-mail,
phone and compact IBANs only. Model policies lived in process memory; an empty policy allowed
every provider, and production required an external provider. A second path, the demo
workflow, called `provider_from_env()` without routing, accounting or any policy.

## Decision

**Invariant.** No confidential source text, user question or extracted fact leaves the local
environment unless a durable, tenant-specific policy authorizes exactly this purpose,
provider, model and these data classes, and the decision was recorded first. Default: deny.

**Locality.** Whether an adapter sends data out is a property of its class
(`egress_locality`): `DeterministicTestProvider` is `local`, `OpenAIProvider` and
`AzureOpenAIProvider` are `external`, and anything without the attribute counts as external.
It is never configuration. Pointing `OPENAI_BASE_URL` at a local server keeps the adapter
external (conservative).

**Data classes.** Assigned by the backend only: `user_question` for the question,
`document_text` for a chunk of the verified tenant with document and version provenance,
`unclassified` otherwise. `unclassified` can never be authorized. `extracted_fact` and
`synthetic_probe` exist for future/probe use. The regex PII detector is not used to
declassify anything.

**Policies (PostgreSQL, migration 0011).**
- `privacy_egress_policies`: tenant-owned; unique per (tenant, purpose, provider, model);
  `data_classes` (non-empty, known, never `unclassified`), `data_scope`
  (`synthetic_test_data` | `personal_data`), `state` (`active` | `suspended` | `revoked`,
  revocation final), `version` (optimistic concurrency), mandatory `valid_from`/`valid_until`,
  `approved_by`. `personal_data` requires `legal_basis_ref`, `processor_agreement_ref`,
  `transfer_mechanism_ref` and a `retention_control` the provider supports (OpenAI:
  `store_false` or `zero_data_retention`; Azure OpenAI chat completions: only an operator-
  attested `zero_data_retention`), enforced by CHECK constraints and in code.
- `privacy_tenant_profiles`: the operator's statement that a tenant holds synthetic test
  data only. A `synthetic_test_data` policy applies only to such a tenant; without a profile a
  tenant is treated as holding personal data.
- `privacy_egress_policy_revisions` and `privacy_egress_decisions`: append-only (PostgreSQL
  triggers refuse UPDATE and DELETE). Decisions hold correlation id, stage, purpose,
  provider, model, data classes, policy id/version, allow/deny and a reason code — never the
  question, document text or a payload digest.

A policy is a technical authorization by the platform operator. It is **not** a legal basis
and **not** data-subject consent; the references in `personal_data` policies only record that
the operator has established those prerequisites elsewhere. There is no wildcard and no
global allow switch, in any environment.

**Administration.** Policies change only through `scripts/privacy_egress_admin.py` (operator
CLI with database access), which writes the new state and a revision with actor and reason in
one transaction and requires the expected version for every state change. The API offers a
read-only view for tenant admins of their own tenant (`GET /v1/admin/privacy/egress-policies`:
policies and the last 50 decisions); the BFF and the Copilot UI expose no policy changes.

**Enforcement (one guard, two checks).** `PrivacyEgressGuard.decide` reads the tenant's
policies (on PostgreSQL `FOR SHARE`, so a concurrent revocation is serialized), evaluates
them, records the decision and commits; any database, configuration or recording failure
denies.
1. *Precheck* in `VectorQueryService` right after routing and before the budget
   reservation: an unauthorized external primary model denies the request
   (`409 external processing not authorized`) with nothing reserved; unauthorized external
   fallback candidates are removed from the chain. A denial is never rerouted.
2. *Boundary check* per attempt, immediately before the adapter call, on the exact payload:
   it records the authoritative decision and issues an `EgressPermit` bound to the SHA-256
   of provider, model, question, context and citations, sealed with a per-process key,
   valid for 60 seconds and usable once. A denial here raises `POLICY_DENIED`, which is not
   billed and not a fallback category: the reservation is released and no other provider
   is tried.
3. The external adapters call `require_permit` before building a request; without a valid
   permit for exactly this payload they send nothing, and the permit is confirmed once more
   against the policy store (dispatch check, see below). A caller that bypasses the service
   therefore fails closed.

The demo workflow refuses external providers. The paid provider gate may send only the fixed
synthetic probe (`synthetic_probe_permit`, refused in production and for any other content).

**Provider controls.** The OpenAI Responses request sets `store=False` (no application-state
retention for later retrieval; default is true). This is not zero data retention: abuse
monitoring retention and data location are contractual. Azure OpenAI chat completions have
no equivalent request flag; their retention can only be attested by the operator.

## FinOps and idempotency

Precheck denials happen before `before_billing`: idempotency state RETRYABLE, nothing
reserved; the same key may be retried after a grant. Boundary denials occur after the
provider marker: the reservation is released (`POLICY_DENIED` is not billed), no usage is
booked, and the key settles as FAILED (its 409 is replayed; a new key is needed). Replays of
succeeded keys never contact the provider.

## Revocation semantics (linearization point)

The authorization is linearized at the **dispatch check**: immediately before the SDK call
the adapter consumes the permit (single use) and the guard re-reads exactly the policy that
issued it (`FOR SHARE` on PostgreSQL), re-evaluates state, validity window, data classes and
tenant profile, requires the same policy version and records a `dispatch` decision.

- A revocation, suspension, version change (for example suspend and resume) or expiry that
  committed before the dispatch check denies the request: no new external request is
  started, even with an unexpired, unused permit. An unreadable policy store or an
  unrecordable decision also denies. A denied permit stays consumed.
- A request whose dispatch check already succeeded is in flight and may complete; in-flight
  provider calls are not interrupted.
- On PostgreSQL a revocation (`UPDATE`) and a dispatch check (`FOR SHARE`) on the same policy
  row are serialized, so every request is ordered either before or after the revocation.

The ENT-13.1 review confirmed that the first implementation lacked this check (a permit
issued before a revocation could start a new request within its 60-second lifetime);
`tests/security/test_privacy_permit_revocation.py` reproduces it for revocation,
suspension, version change, expiry and an unavailable store.

## Consequences and limits

- Upgrading denies all external processing until the operator creates policies.
- The historical `external_llm_provider` evidence (d9690cb) is no longer eligible: this
  change modifies `src/ragops/llm/providers.py` and adds `src/ragops/privacy` (now part of
  the exception's path list). A fresh, separately
  authorized paid run (synthetic probe only) is required before the release can be `PASS`.
- Production startup stays blocked (`require_supported_runtime`) and still requires an
  external provider type; a local model adapter is ENT-13.2.
- Not solved: pseudonymisation before authorized egress (P05), deletion/retention (P04),
  provider-side retention beyond `store`, region and transfer checks (operator/legal).

## Tests

`tests/security/test_privacy_egress.py` (default deny, every authorization dimension,
suspension, revocation, expiry, malformed policies, unclassified content, store outage,
unrecordable decisions, revocation between precheck and boundary, fallback, payload binding,
concurrency, trusted tenant, demo workflow and worker, administration),
`tests/security/test_privacy_egress_api.py` (409, idempotent retry and replay, forged fields,
read-only administration), `tests/integration/test_privacy_egress_postgres.py` (append-only
triggers, revocation race), adapter tests in `tests/product/test_external_llm_provider.py`,
and the mandatory live gate `privacy_egress` (security control LIVE-16).
