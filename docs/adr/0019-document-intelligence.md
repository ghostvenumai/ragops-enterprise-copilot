# ADR 0019: Deterministic document intelligence (classification, entities, claim groups)

## Status

Accepted (ENT-11.8).

## Context

Tenants upload letters about payment obligations — invoices, reminders, collection letters,
court notices, receipts — and need structured answers: which documents concern a debt, which
creditors appear, which documents belong to which person, which letters describe the same
claim, which amounts, references and deadlines they state, and where exactly each finding is in
the source. ENT-11.7 already extracts every stored document version into a page contract with
original page numbers, OCR quality and `page-normalized-v1` line boxes, and records page reports
and chunk provenance in PostgreSQL.

The failure modes that matter are not missing features but false statements: a wrong debtor, a
merged pair of different claims, a total that adds a reminder to the invoice it repeats, an old
balance presented as the current one, a court notice presented as an enforceable title, or a
low-quality OCR guess presented as a verified fact.

## Decision

**One pipeline.** Document intelligence runs inside the existing ingestion worker, in the job's
transaction, directly after the page extraction is recorded (`record_extraction`). It reads the
in-memory page contract of that run; there is no second OCR, extraction or vector pipeline and
Qdrant is not involved. It runs before the "no indexable text" decision, so a document whose
pages all need OCR review is still analysed — with every finding marked review-required. A
defect in the analysis is contained in a savepoint and recorded as a failed analysis run; it
never changes the ingestion outcome.

**Deterministic and offline.** The extractor (`ragops.intelligence`, `rules-de-v1`) is a
transparent rule system over German labels, headings and value grammars (amounts, dates,
identifiers, legal forms). No model is called and nothing leaves the host. No approved local
model exists on this host whose benefit could be demonstrated within its memory budget, so none
is used; the code does not claim semantic understanding. It prefers abstention to coverage:
an unlabeled amount stays `unclassified_amount`, an unlabeled date stays `other_date`, an
unknown debtor stays unknown.

**Taxonomy `doc-taxonomy-v1`.** invoice, payment_reminder, formal_demand,
debt_collection_letter, payment_order_or_court_notice, enforcement_document,
creditor_statement, loan_agreement, payment_receipt, settlement_agreement, correspondence,
unrelated_document, unknown. Classification is scored from context, not from a word: a heading
(a short line naming the document type) is strong evidence, a sender or issuing-authority
context supports it, a term in running text is weak and a negated term ("kein Mahnbescheid")
counts for nothing. Every candidate is stored with its rule evidence (page, line, rule id) and a
deterministic evidence strength (`strong`/`moderate`/`weak`), never a probability. Two
strongest candidates of equal strength make the document `ambiguous` and review-required.
A Vollstreckungsbescheid or Mahnbescheid is a court notice; neither is ever reported as an
enforceable title, only as `enforcement_mentioned`/court context.

**Facts, inferences and user decisions are separate.** Each extracted value is one immutable
row in `document_facts` with a precise semantic type (37 types: parties, amount components,
identifiers, dates, status indicators), its raw stated surface text (bounded), the normalized
value (Decimal amounts with explicit ISO currency, ISO dates, normalized names and identifiers),
an `assertion` of `document_stated` (an explicit label in the text) or `rule_inferred` (for
example "the sender of an invoice is its creditor"), and its provenance: document version, page,
source line, character span in that line, chunk index, extraction method, OCR line index and
box (OCR only — native text never gets an invented box) and the page's quality state. Facts
from a review-required OCR page are `review_required` and never count as verified. User
decisions are append-only `intelligence_review_events` (actor, tenant, entity, previous and new
state, reason category, entity version); a correction is stored next to the fact
(`corrected_*`), never over it. Every reviewable row carries `review_version`; a review names
the version it saw and is applied with a conditional update, so a stale concurrent edit gets
409 and the event table's unique `(entity, version)` makes the race impossible at the database.

**Fact identity makes reprocessing idempotent.** A fact's key is a digest of extractor version,
type, page, line, span and normalized value, unique per version. Reprocessing the same version
inserts nothing new; a fact the extractor no longer produces is marked `withdrawn`, never
deleted, because review events refer to it.

**Persons.** `person_profiles` are tenant-scoped records with stable UUIDs and a neutral label;
a name is never an identifier. An authorized reviewer configures `person_aliases` (`name` or
`identifier`); an alias belongs to at most one person per tenant (unique; a collision is 409).
A document is linked to a person as debtor only when an explicitly labelled debtor party
(Schuldner, Antragsgegner, Darlehensnehmer, …) equals one of that person's name aliases after
normalization (case, whitespace, salutation) — exact, never fuzzy. A match only as addressee,
only by identifier, or for several persons is `review_required`; an address never links anyone;
nothing links two people with similar names, and no relationship between people is inferred.
A document's debtor status is `identified`, `ambiguous` or `unknown`.

**Claim groups.** Documents are grouped only by strong evidence: the same normalized claim
creditor (the original creditor where stated, else the creditor; a collection agency is never
the claim creditor) together with the same invoice number or contract reference. Amount,
address, date or wording similarity never groups anything. A group whose members carry
different identified debtors is `contradictory`; a group whose creditor is unknown is
`review_required`. Users confirm or dismiss groups and confirm or reject memberships; a rejected
membership is never re-added. Group and membership rows are unique per key, so reprocessing or
two workers create each at most once.

**Financial aggregation is conservative.** The claims summary reports, per group, the source
amounts with their role (`historical`, `potentially_current`, `component`, `payment`); the
potentially current amount is the stated total or remaining balance of the latest member by
issue date, labelled with that date, never called a current balance. A consolidated total per
currency exists only when every document with amounts is in a resolved group (strong or
confirmed, no proposed members, no review-required amounts, an unambiguous latest amount) and
only one currency is involved; otherwise the result is partial and lists every unresolved item.
Amounts of different currencies are never combined.

**PostgreSQL is the authority.** Migration `0010_document_intelligence` adds eight tenant-owned
tables with composite tenant foreign keys, unique keys, bounded columns and explicit workflow
states. It is purely additive: no existing row is read, reclassified or rewritten; versions
ingested earlier have no analysis until they are reprocessed.

**Access.** The structured API (`/v1/intelligence/...`) uses the existing identity, tenant and
access-level rules: a document above the caller's level or in another tenant does not exist for
the caller (404, absent from every list). Reading needs document access; reviews, person
management and reprocessing need the `admin` or `compliance` role (403 otherwise). There is no
unauthenticated route. Raw values are returned only by these authorized routes; release
evidence, logs and telemetry carry identifiers and counts only. `/v1/query` is unchanged; no
intent routing was added.

## Consequences

* Coverage is limited to the label vocabulary of `rules-de-v1`; unusual layouts produce fewer
  facts and more `unknown`/`review_required`, not wrong ones. The frozen synthetic evaluation
  corpus (`scripts/intelligence_fixtures.py`) measures precision and recall per field family and
  reports every false positive and false negative.
* The analysis is not legal advice: it never decides that a claim is owed, valid, time-barred or
  enforceable.
* Documents ingested before ENT-11.8 need a reprocess (`POST
  /v1/intelligence/documents/{id}/reprocess`) to be analysed.
* A full review UI and PDF highlighting are out of scope; the API returns page, span and boxes.
