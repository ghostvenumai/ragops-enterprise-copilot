"""document_intelligence RC gate: classification, entities and claim groups on real services.

The gate runs the real API and worker processes (gate-owned PostgreSQL database, Redis
namespace and disposable Qdrant, real Keycloak tokens) with the locally installed Tesseract
engine. Wholly synthetic German letters of the frozen corpus (``scripts/intelligence_fixtures``)
are uploaded through the authenticated API — most as native-text PDFs, the annotated ones as
image-only scans read by real OCR, plus a deliberately degraded scan. The worker extracts,
indexes and analyses every version in its own transaction; the gate then asks only the
structured API (``/v1/intelligence``) and checks the answers against the corpus annotations:
classes, creditors, debtors, amount types, dates, references, pages, provenance, claim groups,
the claims summary, the review workflow under concurrency, idempotent reprocessing through the
worker, low-quality OCR, tenant isolation, roles and access levels, forged input and injected
instructions. Nothing is mocked: without Tesseract (deu+eng) the gate is BLOCKED.

Evidence holds identifiers, counts, booleans, letter keys and fact types only: no document
text, raw values, names, amounts, references, tokens or credentials.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import browser_e2e_gate as e2e  # noqa: E402
from scripts import intelligence_fixtures as corpus  # noqa: E402
from scripts import ocr_fixtures as fx  # noqa: E402
from scripts import ocr_ingestion_gate as og  # noqa: E402
from scripts import rag_query_gate as rq  # noqa: E402

EVIDENCE = ROOT / "evidence" / "product-v1" / "rc-live" / "document-intelligence.json"
PrerequisiteMissing = e2e.PrerequisiteMissing
MIN_MEMORY_GIB = 3.5
BASE = "/v1/intelligence"
CONCURRENT_REVIEWERS = 10
# A degraded scan: real OCR still reads text, the page quality gate marks it for review.
LOW_QUALITY_SCAN = {"noise": 0.15, "seed": 11, "text_px": 26, "blur": 1.8}
# A letter claiming the invoice number of claim C1 for another creditor (claim-group poisoning).
POISON_PAGES = (
    (
        "Fremdfirma Beispiel GmbH",
        "Nebenweg 9, 50999 Musterstadt",
        "Frau Anna Beispiel",
        "Musterstadt, 28.02.2026",
        "Rechnung",
        "Rechnungsnummer: VL-2026-0147",
        "Rechnungsempfängerin: Anna Beispiel",
        "Rechnungsbetrag: 9.999,00 EUR",
    ),
)
# A two-page statement: page provenance must follow the facts to page 2.
TWO_PAGE = (
    (
        "Musterbank AG",
        "Bankplatz 2, 60311 Beispielfurt",
        "Forderungsaufstellung",
        "Darlehensnummer: DN-777-001",
    ),
    ("Stand: 30.04.2026", "Hauptforderung: 800,00 EUR", "Restforderung: 650,00 EUR"),
)

REQUIRED = frozenset(
    {
        "real_ocr_engine_used",
        "real_api_upload_used",
        "worker_used",
        "analysis_runs_recorded",
        "document_classification_verified",
        "creditor_extraction_verified",
        "debtor_attribution_verified",
        "ambiguous_debtor_abstained",
        "amount_types_verified",
        "dates_and_references_verified",
        "forbidden_facts_absent",
        "source_page_verified",
        "provenance_verified",
        "ocr_provenance_verified",
        "duplicate_claims_not_double_counted",
        "same_amount_distinct_claims_not_merged",
        "claim_group_poisoning_rejected",
        "no_unsupported_legal_status",
        "payment_and_settlement_recognized",
        "review_workflow_verified",
        "concurrent_corrections_safe",
        "idempotent_reprocessing",
        "low_quality_ocr_not_treated_as_verified",
        "structured_search_verified",
        "tenant_isolation_verified",
        "access_control_verified",
        "forged_source_rejected",
        "fabricated_amount_rejected",
        "person_identity_collision_rejected",
        "injection_resistance_verified",
        "rag_query_path_unchanged",
        "no_document_text_in_logs",
        # cleanup (recorded by the shared environment)
        "processes_stopped",
        "keycloak_gate_objects_removed",
        "redis_gate_keys_removed",
        "unrelated_redis_data_unchanged",
        "gate_database_removed",
        "unrelated_postgres_data_unchanged",
        "unrelated_files_unchanged",
        "disposable_qdrant_removed",
    }
)
CLEANUP_CHECKS = (
    "processes_stopped",
    "keycloak_gate_objects_removed",
    "redis_gate_keys_removed",
    "gate_database_removed",
    "disposable_qdrant_removed",
)
UNRELATED_CHECKS = (
    "unrelated_redis_data_unchanged",
    "unrelated_postgres_data_unchanged",
    "unrelated_files_unchanged",
)
ZERO_COUNTERS = (
    "tenant_leakage",
    "cross_tenant_leakage",
    "paid_provider_calls",
    "wrong_debtor_attributions",
    "false_claim_merges",
)


@dataclass
class DiGateResult(e2e.GateResult):
    cross_tenant_leakage: int = 0
    wrong_debtor_attributions: int = 0
    false_claim_merges: int = 0
    ocr_engine: str = ""
    documents: int = 0
    facts: int = 0
    claim_groups: int = 0
    mismatches: list[dict[str, str]] = field(default_factory=list)
    concurrency: dict[str, int] = field(default_factory=dict)
    performance: dict[str, Any] = field(default_factory=dict)
    throttled: int = 0


def classify(result: DiGateResult) -> tuple[str, str]:
    missing = sorted(REQUIRED - set(result.checks))
    if missing:
        return "FAIL", f"document_intelligence invariant not executed: {missing[0]}"
    failed = sorted(name for name in REQUIRED if not result.checks[name])
    if failed:
        return "FAIL", f"document_intelligence invariant failed: {failed[0]}"
    for counter in ZERO_COUNTERS:
        if getattr(result, counter):
            return "FAIL", f"{counter} must be 0"
    if result.ocr_engine != "tesseract":
        return "FAIL", "the real OCR engine was not used"
    return "PASS", "documents were classified, extracted and grouped conservatively with sources"


def build_evidence(result: DiGateResult, evaluation: dict[str, Any]) -> dict[str, Any]:
    def ok(name: str) -> bool:
        return bool(result.checks.get(name))

    named = (
        "document_classification_verified",
        "creditor_extraction_verified",
        "debtor_attribution_verified",
        "ambiguous_debtor_abstained",
        "amount_types_verified",
        "dates_and_references_verified",
        "forbidden_facts_absent",
        "source_page_verified",
        "provenance_verified",
        "ocr_provenance_verified",
        "duplicate_claims_not_double_counted",
        "same_amount_distinct_claims_not_merged",
        "claim_group_poisoning_rejected",
        "no_unsupported_legal_status",
        "payment_and_settlement_recognized",
        "review_workflow_verified",
        "concurrent_corrections_safe",
        "idempotent_reprocessing",
        "low_quality_ocr_not_treated_as_verified",
        "structured_search_verified",
        "tenant_isolation_verified",
        "access_control_verified",
        "forged_source_rejected",
        "fabricated_amount_rejected",
        "person_identity_collision_rejected",
        "injection_resistance_verified",
        "real_ocr_engine_used",
        "rag_query_path_unchanged",
    )
    return {
        "gate": "document_intelligence",
        "extractor_version": "rules-de-v1",
        "taxonomy_version": "doc-taxonomy-v1",
        "extraction_engine": "deterministic_rules_offline",
        "ocr_engine": result.ocr_engine,
        **{name: ok(name) for name in named},
        "corpus_digest": corpus.corpus_digest(),
        "corpus_letters": len(corpus.LETTERS),
        "documents_analysed": result.documents,
        "facts_observed": result.facts,
        "claim_groups_observed": result.claim_groups,
        "mismatches": result.mismatches,
        "concurrency": result.concurrency,
        "concurrent_reviewers": result.concurrency.get("reviewers", 0),
        "concurrent_applied": result.concurrency.get("applied", 0),
        "evaluation": evaluation,
        "wrong_debtor_attributions": result.wrong_debtor_attributions,
        "false_claim_merges": result.false_claim_merges,
        "tenant_leakage": result.tenant_leakage,
        "cross_tenant_leakage": result.cross_tenant_leakage,
        "provider_invocations": result.provider_invocations,
        "paid_provider_calls": result.paid_provider_calls,
        "throttled_requests": result.throttled,
        "performance": result.performance,
        "cleanup_status": "PASS" if all(ok(name) for name in CLEANUP_CHECKS) else "FAIL",
        "unrelated_data_unchanged": all(ok(name) for name in UNRELATED_CHECKS),
        "metrics": result.metrics,
        "checks": dict(sorted(result.checks.items())),
        "errors": result.errors,
    }


# --------------------------------------------------------------------------- http


def retry_after(headers: dict[str, str]) -> float:
    """Seconds the server asks to wait; header names are case-insensitive (uvicorn sends them
    lowercase), so a missed header never shortens the wait inside the rate-limit window."""
    value = next((v for k, v in headers.items() if k.lower() == "retry-after"), "") or "5"
    try:
        return min(60.0, max(0.0, float(value)))
    except ValueError:
        return 5.0


class Api:
    """Structured API calls; a 429 is waited out (bounded), never counted as an answer."""

    def __init__(self, env: e2e.Environment, result: DiGateResult) -> None:
        self.env, self.result = env, result
        self._tokens: dict[str, str] = {}
        self._lock = threading.Lock()

    def token(self, label: str) -> str:
        with self._lock:
            if label not in self._tokens:
                self._tokens[label] = self.env.token(label)
            return self._tokens[label]

    def call(
        self,
        path: str,
        label: str | None = "a-admin",
        method: str = "GET",
        data: object | None = None,
    ) -> tuple[int, Any]:
        for _ in range(4):
            status, body, headers = e2e.http_json(
                f"{self.env.api_url}{path}",
                method,
                data,
                token=self.token(label) if label else None,
                timeout=60,
            )
            if status != 429:
                return status, body
            with self._lock:
                self.result.throttled += 1
            time.sleep(retry_after(headers) + 0.5)
        return status, body


@dataclass
class Uploaded:
    key: str
    document_id: str = ""
    version_id: str = ""
    job_id: str = ""
    status: str = ""
    latency_ms: float = 0.0


def upload(
    env: e2e.Environment, label: str, key: str, name: str, mime: str, content: bytes
) -> Uploaded:
    started = time.perf_counter()
    status, accepted = og.upload(env, label, name, mime, content, key)
    if status != 202 or not accepted:
        raise RuntimeError(f"upload {key} was not accepted ({status})")
    item = Uploaded(key, accepted["document_id"], accepted["version_id"], accepted["job_id"])
    job = og.wait_for_job(env, label, item.job_id)
    item.status = str(job.get("status"))
    item.latency_ms = round((time.perf_counter() - started) * 1000, 1)
    return item


def letter_content(letter: corpus.Letter) -> tuple[str, str, bytes]:
    pages = [list(page) for page in letter.pages]
    if letter.scanned:
        return f"{letter.key}.pdf", "application/pdf", fx.scanned_pdf(pages)
    return f"{letter.key}.pdf", "application/pdf", fx.native_pdf(pages)


def low_quality_content() -> bytes:
    from PIL import ImageFilter

    lines = list(corpus.by_key("shared_address_ambiguous").pages[0])
    image = fx.scan_image(
        lines,
        size=(1240, 1754),
        noise=LOW_QUALITY_SCAN["noise"],
        seed=int(LOW_QUALITY_SCAN["seed"]),
        text_px=int(LOW_QUALITY_SCAN["text_px"]),
    ).filter(ImageFilter.GaussianBlur(LOW_QUALITY_SCAN["blur"]))
    return fx.image_bytes(image, "PNG")


def pairs(detail: dict[str, Any]) -> set[tuple[str, str]]:
    out = set()
    for fact in detail.get("facts", []):
        if fact["category"] == "amount":
            value = f"{fact['amount']} {fact['currency']}" if fact["currency"] else fact["amount"]
        elif fact["category"] == "date":
            value = fact["date"]
        elif fact["category"] == "status":
            value = "true"
        else:
            value = fact["value"]
        out.add((fact["fact_type"], value))
    return out


# --------------------------------------------------------------------------- stages


class Run:
    def __init__(self, env: e2e.Environment, result: DiGateResult) -> None:
        self.env, self.result = env, result
        self.api = Api(env, result)
        self.docs: dict[str, Uploaded] = {}
        self.details: dict[str, dict[str, Any]] = {}
        self.persons: dict[str, str] = {}
        self.foreign: dict[str, Uploaded] = {}

    def keys_of(self, listed: Any) -> set[str]:
        """Gate keys of listed documents (by document id; unknown ids keep their id)."""
        by_id = {item.document_id: key for key, item in self.docs.items()}
        return {by_id.get(item["document_id"], item["document_id"]) for item in listed or []}

    # -- setup
    def provision(self) -> None:
        for label, aliases in corpus.PERSONS.items():
            status, body = self.api.call(f"{BASE}/persons", method="POST", data={"label": label})
            if status != 201:
                raise RuntimeError(f"person could not be created ({status})")
            self.persons[label] = body["id"]
            for alias in aliases:
                status, _ = self.api.call(
                    f"{BASE}/persons/{body['id']}/aliases",
                    method="POST",
                    data={"kind": "name", "value": alias},
                )
                if status != 201:
                    raise RuntimeError(f"alias could not be added ({status})")
        # Tenant B configures the same name for its own person: tenants never share identity.
        status, body = self.api.call(f"{BASE}/persons", "b-admin", "POST", {"label": "Person A"})
        status_alias, _ = self.api.call(
            f"{BASE}/persons/{(body or {}).get('id', '')}/aliases",
            "b-admin",
            "POST",
            {"kind": "name", "value": corpus.PERSONS["Person A"][0]},
        )
        self.result.check(
            "person_identity_collision_rejected", status == 201 and status_alias == 201
        )
        self.foreign_person = (body or {}).get("id", "")

    def ingest(self) -> None:
        timings: dict[str, float] = {}
        for letter in corpus.LETTERS:
            name, mime, content = letter_content(letter)
            item = upload(self.env, "a-admin", letter.key, name, mime, content)
            self.docs[letter.key] = item
            timings[letter.key] = item.latency_ms
        extra = {
            "poison": (
                "poison.pdf",
                "application/pdf",
                fx.native_pdf([list(p) for p in POISON_PAGES]),
            ),
            "two_page": (
                "two_page.pdf",
                "application/pdf",
                fx.native_pdf([list(p) for p in TWO_PAGE]),
            ),
            "low_quality": ("low_quality.png", "image/png", low_quality_content()),
        }
        for key, (name, mime, content) in extra.items():
            item = upload(self.env, "a-admin", key, name, mime, content)
            self.docs[key] = item
            timings[key] = item.latency_ms
        # Tenant B: the same C1 invoice text (another tenant's claim) and nothing else.
        name, mime, content = letter_content(corpus.by_key("invoice_c1"))
        self.foreign["invoice_c1"] = upload(self.env, "b-admin", "invoice_c1", name, mime, content)
        completed = [
            item.status == "completed" for key, item in self.docs.items() if key != "low_quality"
        ]
        self.result.check("real_api_upload_used", bool(self.docs))
        self.result.check(
            "worker_used",
            all(completed) and self.foreign["invoice_c1"].status == "completed",
        )
        self.result.documents = len(self.docs) + len(self.foreign)
        self.result.performance["upload_to_completion_ms"] = {
            "max": max(timings.values()),
            "median": sorted(timings.values())[len(timings) // 2],
            "documents": len(timings),
        }
        for key, item in self.docs.items():
            status, body = self.api.call(f"{BASE}/documents/{item.document_id}")
            if status != 200:
                raise RuntimeError(f"analysis of {key} is not readable ({status})")
            self.details[key] = body
        self.result.facts = sum(len(detail.get("facts", [])) for detail in self.details.values())
        self.result.check(
            "analysis_runs_recorded",
            all(detail.get("analysis_status") == "completed" for detail in self.details.values()),
        )

    # -- correctness
    def verify_extraction(self) -> None:
        classes = amounts = dates = forbidden = True
        for letter in corpus.LETTERS:
            detail = self.details[letter.key]
            found = pairs(detail)
            if detail.get("classification") != letter.document_class:
                classes = False
                self.result.mismatches.append({"letter": letter.key, "kind": "classification"})
            for family in ("amount", "date", "identifier"):
                expected = {pair for pair in letter.facts if corpus_category(pair[0]) == family}
                actual = {pair for pair in found if corpus_category(pair[0]) == family}
                if expected != actual:
                    self.result.mismatches.append({"letter": letter.key, "kind": family})
                    if family == "amount":
                        amounts = False
                    else:
                        dates = False
            if found & letter.forbidden:
                forbidden = False
                self.result.mismatches.append({"letter": letter.key, "kind": "forbidden"})
        self.result.check("document_classification_verified", classes)
        self.result.check("amount_types_verified", amounts)
        self.result.check("dates_and_references_verified", dates)
        self.result.check("forbidden_facts_absent", forbidden)
        court = pairs(self.details["court_c2"])
        enforcement = pairs(self.details["enforcement_notice"])
        self.result.check(
            "no_unsupported_legal_status",
            ("enforcement_mentioned", "true") not in court
            and ("disputed", "true") not in court
            and ("enforcement_mentioned", "true") in enforcement
            and not any(
                "enforceable" in fact["fact_type"]
                for d in self.details.values()
                for fact in d["facts"]
            ),
        )
        receipt = pairs(self.details["receipt_c4_paid"])
        self.result.check(
            "payment_and_settlement_recognized",
            {
                ("payment_made_amount", "120.50 EUR"),
                ("payment_reported", "true"),
                ("settled", "true"),
            }
            <= receipt,
        )

    def verify_creditors(self) -> None:
        status, creditors = self.api.call(f"{BASE}/creditors")
        expected = {
            value
            for letter in corpus.LETTERS
            for kind, value in letter.facts
            if kind in {"creditor", "original_creditor", "current_claimant"}
        } | {"fremdfirma beispiel gmbh", "musterbank ag"}
        names = {item["creditor"] for item in creditors or []}
        inkasso = pairs(self.details["inkasso_c1"])
        self.result.check(
            "creditor_extraction_verified",
            status == 200
            and names == expected
            and ("creditor", "beispiel inkasso gmbh") not in inkasso
            and ("collection_agency", "beispiel inkasso gmbh") in inkasso,
        )

    def verify_persons(self) -> None:
        ok = True
        for label, person_id in self.persons.items():
            status, listed = self.api.call(f"{BASE}/documents?person_id={person_id}")
            keys = self.keys_of(listed)
            expected = {letter.key for letter in corpus.LETTERS if label in letter.debtors}
            # The poisoned letter names Person A as invoice recipient: attributing it is right.
            wrong = keys - expected - {"poison"}
            self.result.wrong_debtor_attributions += len(wrong)
            ok = ok and status == 200 and expected <= keys and not wrong
        self.result.check("debtor_attribution_verified", ok)
        debtor = {key: self.details[key]["debtor_status"] for key in self.details}
        self.result.check(
            "ambiguous_debtor_abstained",
            all(debtor[letter.key] == letter.debtor_status for letter in corpus.LETTERS)
            and debtor["shared_address_ambiguous"] == "ambiguous"
            and debtor["similar_name_not_matched"] == "unmatched",
        )

    def verify_provenance(self) -> None:
        detail = self.details["inkasso_c1"]
        total = next(f for f in detail["facts"] if f["fact_type"] == "stated_total_claim")
        status, fact = self.api.call(f"{BASE}/facts/{total['id']}")
        source = (fact or {}).get("source", {})
        chunk_status, chunk = self.api.call(
            f"/v1/documents/{self.docs['inkasso_c1'].document_id}/versions/"
            f"{self.docs['inkasso_c1'].version_id}/chunks/"
            f"{source.get('chunk_index', -1)}/provenance"
        )
        native_ok = (
            status == 200
            and source.get("page_number") == 1
            and source.get("document_version_id") == self.docs["inkasso_c1"].version_id
            and source.get("document_id") == self.docs["inkasso_c1"].document_id
            and source.get("extraction_method") == "native_text"
            and source.get("regions") == []
            and isinstance(source.get("raw_value"), str)
            and source.get("char_end", 0) > source.get("char_start", 0)
            and chunk_status == 200
            and (chunk or {}).get("page_number") == 1
        )
        self.result.check("provenance_verified", native_ok)
        scanned = self.details["reminder_c1"]
        balance = next(
            (f for f in scanned["facts"] if f["fact_type"] == "stated_remaining_balance"), None
        )
        ocr_ok = False
        if balance is not None:
            status, fact = self.api.call(f"{BASE}/facts/{balance['id']}")
            source = (fact or {}).get("source", {})
            regions = source.get("regions") or []
            chunk_status, chunk = self.api.call(
                f"/v1/documents/{self.docs['reminder_c1'].document_id}/versions/"
                f"{self.docs['reminder_c1'].version_id}/chunks/"
                f"{source.get('chunk_index', -1)}/provenance"
            )
            chunk_lines = {region.get("line") for region in (chunk or {}).get("regions", [])}
            ocr_ok = (
                status == 200
                and source.get("extraction_method") == "ocr"
                and source.get("coordinate_space") == "page-normalized-v1"
                and len(regions) == 1
                and og.box_valid(regions[0].get("box"))
                and chunk_status == 200
                and regions[0].get("line") in chunk_lines
                and fact.get("quality_state") == "accepted"
            )
        self.result.check("ocr_provenance_verified", ocr_ok)
        self.result.check("real_ocr_engine_used", ocr_ok and self.result.ocr_engine == "tesseract")
        pages_ok = all(
            fact["page_number"] == 1
            for key in (letter.key for letter in corpus.LETTERS)
            for fact in self.details[key]["facts"]
        )
        two = {f["fact_type"]: f["page_number"] for f in self.details["two_page"]["facts"]}
        self.result.check(
            "source_page_verified",
            pages_ok
            and two.get("contract_reference") == 1
            and two.get("stated_remaining_balance") == 2
            and two.get("principal_amount") == 2,
        )

    def claims(self, label: str = "a-admin", person: str | None = None) -> dict[str, Any]:
        query = f"?person_id={person}" if person else ""
        status, body = self.api.call(f"{BASE}/claims{query}", label)
        if status != 200:
            raise RuntimeError(f"claims summary failed ({status})")
        return dict(body)

    def group_of(self, summary: dict[str, Any], key: str) -> dict[str, Any] | None:
        document = self.docs[key].document_id
        for claim in summary["claims"]:
            if any(
                d["document_id"] == document and d["membership"] != "rejected"
                for d in claim["documents"]
            ):
                return dict(claim)
        return None

    def verify_claims(self) -> None:
        summary = self.claims()
        by_document = {item.document_id: key for key, item in self.docs.items()}
        found = []
        for claim in summary["claims"]:
            members = {by_document.get(d["document_id"], "?") for d in claim["documents"]}
            found.append(members)
        self.result.claim_groups = len(found)
        expected_of = {letter.key: letter.claim for letter in corpus.LETTERS}
        merges = sum(
            1
            for members in found
            if len({expected_of.get(member, member) for member in members}) > 1
        )
        self.result.false_claim_merges += merges
        expected = {frozenset(v) for v in corpus.EXPECTED_CLAIMS.values()}
        corpus_found = {frozenset(m) for m in found if m <= set(expected_of)}
        c1 = self.group_of(self.claims(person=self.persons["Person A"]), "invoice_c1")
        current = (c1 or {}).get("potentially_current") or {}
        text = json.dumps(summary)
        self.result.check(
            "duplicate_claims_not_double_counted",
            c1 is not None
            and len(c1["documents"]) == 3
            and current.get("amount") == corpus.C1_LATEST_TOTAL
            and corpus.C1_NAIVE_SUM not in text
            and summary["complete"] is False
            and summary["consolidated_total"] is None
            and any(item.get("reason") == "no_claim_identifier" for item in summary["unresolved"]),
        )
        groups = {
            key: self.group_of(summary, key)
            for key in ("invoice_c1", "invoice_c2_same_amount", "invoice_c3_same_creditor_amount")
        }
        ids = {key: (group or {}).get("claim_group_id") for key, group in groups.items()}
        self.result.check(
            "same_amount_distinct_claims_not_merged",
            corpus_found == expected
            and len(set(ids.values())) == 3
            and None not in ids.values()
            and merges == 0,
        )
        poison = self.group_of(summary, "poison")
        self.result.check(
            "claim_group_poisoning_rejected",
            poison is not None
            and poison["claim_group_id"] != ids["invoice_c1"]
            and len(poison["documents"]) == 1
            and len((c1 or {}).get("documents", [])) == 3,
        )
        settled = self.group_of(summary, "receipt_c4_paid") or {}
        self.result.metrics["c4_settled"] = bool(settled.get("settled"))
        self.result.check(
            "payment_and_settlement_recognized",
            settled.get("settled") is True
            and (settled.get("potentially_current") or {}).get("amount") == "0.00",
        )

    def verify_search(self) -> None:
        status, listed = self.api.call(f"{BASE}/documents?classification=debt_collection_letter")
        titles = self.keys_of(listed)
        status_d, deadlines = self.api.call(
            f"{BASE}/deadlines?person_id={self.persons['Person A']}"
        )
        dates = {item["date"] for item in deadlines or []}
        expected_dates = {
            value
            for letter in corpus.LETTERS
            if "Person A" in letter.debtors
            for kind, value in letter.facts
            if kind in {"due_date", "payment_deadline"}
        }
        status_f, findings = self.api.call(f"{BASE}/findings")
        reasons = {(item.get("document_id"), item.get("reason")) for item in findings or []}
        low = self.docs["low_quality"].document_id
        shared = self.docs["shared_address_ambiguous"].document_id
        status_bad, _ = self.api.call(f"{BASE}/documents?classification=not_a_class")
        self.result.check(
            "structured_search_verified",
            status == 200
            and titles == {"inkasso_c1"}
            and status_d == 200
            and dates == expected_dates
            and status_f == 200
            and (low, "low_quality_source") in reasons
            and (shared, "ambiguous_debtor") in reasons
            and status_bad == 422,
        )

    def verify_low_quality(self) -> None:
        item = self.docs["low_quality"]
        detail = self.details["low_quality"]
        facts = detail.get("facts", [])
        summary = self.claims()
        in_claims = any(
            d["document_id"] == item.document_id for c in summary["claims"] for d in c["documents"]
        )
        self.result.check(
            "low_quality_ocr_not_treated_as_verified",
            item.status == "failed"
            and bool(facts)
            and all(f["quality_state"] == "review_required" for f in facts)
            and all(f["review_state"] == "review_required" for f in facts)
            and detail["review_required"] is True
            and detail["debtor_status"] != "identified"
            and not in_claims,
        )

    def verify_injection(self) -> None:
        detail = self.details["injected_instructions"]
        found = pairs(detail)
        letter = corpus.by_key("injected_instructions")
        self.result.check(
            "injection_resistance_verified",
            detail.get("ignored_instruction_lines") == 2
            and not found & letter.forbidden
            and detail["classification"] == "payment_reminder"
            and detail["debtor_status"] == "identified",
        )

    # -- workflow
    def verify_reviews(self) -> None:
        detail = self.details["inkasso_c1"]
        total = next(f for f in detail["facts"] if f["fact_type"] == "stated_total_claim")
        primary = next(c for c in detail["classifications"] if c["is_primary"])
        status_c, _ = self.api.call(
            f"{BASE}/reviews",
            method="POST",
            data={
                "entity_type": "classification",
                "entity_id": primary["id"],
                "action": "confirm",
                "expected_version": primary["review_version"],
                "reason_category": "verified_against_source",
            },
        )
        barrier = threading.Barrier(CONCURRENT_REVIEWERS)

        def correct(index: int) -> int:
            barrier.wait(timeout=30)
            status, _ = self.api.call(
                f"{BASE}/reviews",
                method="POST",
                data={
                    "entity_type": "fact",
                    "entity_id": total["id"],
                    "action": "correct",
                    "expected_version": total["review_version"],
                    "reason_category": "extraction_error",
                    "value": {"amount": f"{450 + index}.00", "currency": "EUR"},
                },
            )
            return status

        for label in ("a-admin",):
            self.api.token(label)
        with ThreadPoolExecutor(max_workers=CONCURRENT_REVIEWERS) as pool:
            outcomes = list(pool.map(correct, range(CONCURRENT_REVIEWERS)))
        applied, conflicts = outcomes.count(200), outcomes.count(409)
        self.result.concurrency = {
            "reviewers": CONCURRENT_REVIEWERS,
            "applied": applied,
            "conflicts": conflicts,
            "other": len(outcomes) - applied - conflicts,
        }
        status, after = self.api.call(f"{BASE}/facts/{total['id']}")
        self.result.check(
            "concurrent_corrections_safe",
            applied == 1
            and conflicts == CONCURRENT_REVIEWERS - 1
            and status == 200
            and after["review_version"] == total["review_version"] + 1,
        )
        stale_status, _ = self.api.call(
            f"{BASE}/reviews",
            method="POST",
            data={
                "entity_type": "fact",
                "entity_id": total["id"],
                "action": "confirm",
                "expected_version": total["review_version"],
                "reason_category": "verified_against_source",
            },
        )
        effective = (after or {}).get("effective", {})
        self.result.check(
            "review_workflow_verified",
            status_c == 200
            and stale_status == 409
            and after["amount"] == "445.00"
            and after["review_state"] == "corrected"
            and effective.get("origin") == "user"
            and effective.get("amount") != "445.00",
        )
        # Forged provenance and fabricated values are refused before any change.
        forged, _ = self.api.call(
            f"{BASE}/reviews",
            method="POST",
            data={
                "entity_type": "fact",
                "entity_id": total["id"],
                "action": "confirm",
                "expected_version": after["review_version"],
                "reason_category": "verified_against_source",
                "source": {"page_number": 7, "chunk_index": 0},
            },
        )
        create_fact, _ = self.api.call(
            f"{BASE}/facts", method="POST", data={"fact_type": "stated_total_claim"}
        )
        foreign_fact, _ = self.api.call(
            f"{BASE}/reviews",
            method="POST",
            data={
                "entity_type": "fact",
                "entity_id": str(uuid.uuid4()),
                "action": "confirm",
                "expected_version": 1,
                "reason_category": "verified_against_source",
            },
        )
        self.result.check(
            "forged_source_rejected",
            forged == 422 and create_fact in {404, 405} and foreign_fact == 404,
        )
        negative, _ = self.api.call(
            f"{BASE}/reviews",
            method="POST",
            data={
                "entity_type": "fact",
                "entity_id": total["id"],
                "action": "correct",
                "expected_version": after["review_version"],
                "reason_category": "extraction_error",
                "value": {"amount": "-445.00", "currency": "EUR"},
            },
        )
        currency, _ = self.api.call(
            f"{BASE}/reviews",
            method="POST",
            data={
                "entity_type": "fact",
                "entity_id": total["id"],
                "action": "correct",
                "expected_version": after["review_version"],
                "reason_category": "extraction_error",
                "value": {"amount": "1.00", "currency": "XX1"},
            },
        )
        clash, _ = self.api.call(
            f"{BASE}/persons/{self.persons['Person B']}/aliases",
            method="POST",
            data={"kind": "name", "value": corpus.PERSONS["Person A"][0].upper()},
        )
        _, unchanged = self.api.call(f"{BASE}/facts/{total['id']}")
        self.result.check(
            "fabricated_amount_rejected",
            negative == 422
            and currency == 422
            and (unchanged or {}).get("review_version") == after["review_version"],
        )
        self.result.check(
            "person_identity_collision_rejected",
            clash == 409
            and self.details["similar_name_not_matched"]["debtor_status"] == "unmatched",
        )

    def snapshot(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for key, item in self.docs.items():
            status, detail = self.api.call(f"{BASE}/documents/{item.document_id}")
            if status != 200:
                raise RuntimeError("snapshot read failed")
            out[key] = sorted(
                (f["id"], f["fact_type"], f["review_state"], f["review_version"])
                for f in detail["facts"]
            ) + sorted((c["document_class"], c["rank"]) for c in detail["classifications"])
        summary = self.claims()
        out["_claims"] = sorted(
            (c["claim_group_id"], tuple(sorted(d["member_id"] for d in c["documents"])))
            for c in summary["claims"]
        )
        return out

    def verify_reprocessing(self) -> None:
        before = self.snapshot()
        jobs = []
        for key in ("inkasso_c1", "reminder_c1", "invoice_c1"):
            status, accepted = self.api.call(
                f"{BASE}/documents/{self.docs[key].document_id}/reprocess", method="POST"
            )
            if status != 202:
                raise RuntimeError(f"reprocess of {key} was not accepted ({status})")
            jobs.append(accepted["job_id"])
        finished = [og.wait_for_job(self.env, "a-admin", job).get("status") for job in jobs]
        after = self.snapshot()
        self.result.check(
            "idempotent_reprocessing", finished == ["completed"] * len(jobs) and before == after
        )

    def verify_isolation_and_access(self) -> None:
        foreign = self.foreign["invoice_c1"]
        a_doc = self.docs["inkasso_c1"]
        total = next(
            f for f in self.details["inkasso_c1"]["facts"] if f["fact_type"] == "stated_total_claim"
        )
        leaks = 0
        status, listed = self.api.call(f"{BASE}/documents", "b-admin")
        leaks += sum(1 for item in listed or [] if item["document_id"] != foreign.document_id)
        denied = [
            self.api.call(f"{BASE}/documents/{a_doc.document_id}", "b-admin")[0],
            self.api.call(f"{BASE}/facts/{total['id']}", "b-admin")[0],
            self.api.call(f"{BASE}/documents/{a_doc.document_id}/reprocess", "b-admin", "POST")[0],
            self.api.call(
                f"{BASE}/reviews",
                "b-admin",
                "POST",
                {
                    "entity_type": "fact",
                    "entity_id": total["id"],
                    "action": "confirm",
                    "expected_version": 1,
                    "reason_category": "verified_against_source",
                },
            )[0],
        ]
        b_claims = self.claims("b-admin")
        leaks += sum(
            1
            for claim in b_claims["claims"]
            for d in claim["documents"]
            if d["document_id"] != foreign.document_id
        )
        _, b_creditors = self.api.call(f"{BASE}/creditors", "b-admin")
        leaks += sum(
            1
            for item in b_creditors or []
            for ref in item["documents"]
            if ref["document_id"] != foreign.document_id
        )
        _, b_detail = self.api.call(f"{BASE}/documents/{foreign.document_id}", "b-admin")
        b_person_links = {p["person_id"] for p in (b_detail or {}).get("persons", [])}
        a_detail = self.details["invoice_c1"]
        a_person_links = {p["person_id"] for p in a_detail.get("persons", [])}
        a_c1 = self.group_of(self.claims(), "invoice_c1") or {}
        b_c1 = (b_claims["claims"] or [{}])[0]
        isolated = (
            status == 200
            and denied == [404, 404, 404, 404]
            and b_person_links == {self.foreign_person}
            and self.foreign_person not in a_person_links
            and a_c1.get("claim_group_id") != b_c1.get("claim_group_id")
            and len(a_c1.get("documents", [])) == 3
        )
        self.result.tenant_leakage += leaks
        self.result.cross_tenant_leakage += leaks
        self.result.check("tenant_isolation_verified", isolated and leaks == 0)
        # Roles and access levels.
        routes = [
            ("GET", f"{BASE}/documents"),
            ("GET", f"{BASE}/documents/{a_doc.document_id}"),
            ("GET", f"{BASE}/claims"),
            ("GET", f"{BASE}/creditors"),
            ("GET", f"{BASE}/deadlines"),
            ("GET", f"{BASE}/findings"),
            ("GET", f"{BASE}/facts/{total['id']}"),
            ("GET", f"{BASE}/persons"),
            ("POST", f"{BASE}/persons"),
            ("POST", f"{BASE}/reviews"),
            ("POST", f"{BASE}/documents/{a_doc.document_id}/reprocess"),
        ]
        anonymous = [self.api.call(path, None, method)[0] for method, path in routes]
        viewer_read = self.api.call(f"{BASE}/documents/{a_doc.document_id}", "a-viewer")[0]
        viewer_writes = [
            self.api.call(f"{BASE}/persons", "a-viewer", "POST", {"label": "X"})[0],
            self.api.call(
                f"{BASE}/reviews",
                "a-viewer",
                "POST",
                {
                    "entity_type": "fact",
                    "entity_id": total["id"],
                    "action": "confirm",
                    "expected_version": 1,
                    "reason_category": "verified_against_source",
                },
            )[0],
            self.api.call(f"{BASE}/documents/{a_doc.document_id}/reprocess", "a-viewer", "POST")[0],
            self.api.call(f"{BASE}/persons", "a-support", "POST", {"label": "Y"})[0],
        ]
        restricted = self.docs["statement_components"]
        restrict(self.env, restricted.document_id)
        restricted_fact = self.details["statement_components"]["facts"][0]["id"]
        hidden = [
            self.api.call(f"{BASE}/documents/{restricted.document_id}", "a-viewer")[0],
            self.api.call(f"{BASE}/documents/{restricted.document_id}", "a-support")[0],
            self.api.call(f"{BASE}/facts/{restricted_fact}", "a-viewer")[0],
        ]
        viewer_titles = self.keys_of(self.api.call(f"{BASE}/documents", "a-viewer")[1])
        viewer_claims = json.dumps(self.claims("a-viewer"))
        admin_sees = self.api.call(f"{BASE}/documents/{restricted.document_id}")[0]
        self.result.check(
            "access_control_verified",
            anonymous == [401] * len(routes)
            and viewer_read == 200
            and viewer_writes == [403, 403, 403, 403]
            and hidden == [404, 404, 404]
            and "statement_components" not in viewer_titles
            and restricted.document_id not in viewer_claims
            and admin_sees == 200,
        )

    def verify_rag_unchanged(self) -> None:
        """/v1/query still answers through the vector path; intelligence adds no routing."""
        status, body, _ = e2e.http_json(
            f"{self.env.api_url}/v1/query",
            "POST",
            {"question": "Welche Unterlagen liegen zur Forderungsaufstellung vor?", "top_k": 1},
            token=self.api.token("a-admin"),
            timeout=60,
        )
        citations = (body or {}).get("citations", [])
        self.result.check(
            "rag_query_path_unchanged",
            status == 200
            and isinstance(body, dict)
            and "answer" in body
            and all(c.get("tenant_id") == e2e.TENANT_A for c in citations)
            and "claims" not in body,
        )


def corpus_category(fact_type: str) -> str:
    from ragops.intelligence.rules import FACT_TYPES

    return FACT_TYPES[fact_type]


def restrict(env: e2e.Environment, document_id: str) -> None:
    """Raise one gate document to 'restricted' directly in the gate-owned database."""
    from sqlalchemy import create_engine, update

    from ragops.persistence.models import Document

    engine = create_engine(env.database_url, hide_parameters=True)
    try:
        with engine.begin() as connection:
            connection.execute(
                update(Document)
                .where(Document.id == uuid.UUID(document_id))
                .values(access_level="restricted")
            )
    finally:
        engine.dispose()


def run(result: DiGateResult, evidence: dict[str, Any]) -> tuple[str, ...]:
    from ragops.ingestion.ocr import OcrError, TesseractEngine

    try:
        info = TesseractEngine().probe()
    except OcrError as exc:
        raise PrerequisiteMissing(
            f"DEPENDENCY_BLOCKED: Tesseract with deu+eng language data is required ({exc.code})"
        ) from None
    if not {"deu", "eng"} <= set(info.languages):
        raise PrerequisiteMissing("DEPENDENCY_BLOCKED: Tesseract deu+eng language data missing")
    result.ocr_engine = info.name
    found: list[str] = list(corpus.all_text())
    found += [line for page in POISON_PAGES + TWO_PAGE for line in page]
    with e2e.environment(
        result,
        evidence,
        query_mode="vector",
        observe_qdrant=False,
        idp_down_dashboard=False,
        with_dashboard=False,
    ) as env:
        found += [env.client_secret, *(user.password for user in env.users.values())]
        store = rq.Store(env)
        worker = next(service for service in env.services if service.name == "worker")
        a = rq.Tenancy("a-admin", e2e.TENANT_A, "", "")
        b = rq.Tenancy("b-admin", e2e.TENANT_B, "", "")
        gate = Run(env, result)
        try:
            with og.MemorySampler(worker.process.pid) as sampler:
                stages: list[tuple[str, Callable[[], None]]] = [
                    ("provision_accounting", lambda: rq.provision_accounting(env, store, [a, b])),
                    ("provision_persons", gate.provision),
                    ("ingest", gate.ingest),
                    ("extraction", gate.verify_extraction),
                    ("creditors", gate.verify_creditors),
                    ("persons", gate.verify_persons),
                    ("provenance", gate.verify_provenance),
                    ("claims", gate.verify_claims),
                    ("search", gate.verify_search),
                    ("low_quality", gate.verify_low_quality),
                    ("injection", gate.verify_injection),
                    ("reprocessing", gate.verify_reprocessing),
                    ("reviews", gate.verify_reviews),
                    ("isolation_access", gate.verify_isolation_and_access),
                    ("rag_query", gate.verify_rag_unchanged),
                ]
                for name, stage in stages:
                    result.metrics["last_stage"] = name
                    started = time.perf_counter()
                    stage()
                    result.performance.setdefault("stage_ms", {})[name] = round(
                        (time.perf_counter() - started) * 1000, 1
                    )
            result.performance["worker_peak_rss_mib"] = round(sampler.peak_kib / 1024, 1)
        finally:
            store.close()
        log = "\n".join(
            path.read_text(encoding="utf-8", errors="replace")
            for path in (env.work / "logs").glob("*.log")
        )
        sensitive = [line for line in corpus.all_text() if len(line) > 12]
        result.check("no_document_text_in_logs", not any(line in log for line in sensitive))
    return tuple(found)


def _finish(
    evidence: dict[str, Any], status: str, reason: str, exit_code: int, found: tuple[str, ...] = ()
) -> int:
    from scripts.security_gate import leaks

    evidence.update(
        {
            "status": status,
            "reason": reason,
            "exit_code": exit_code,
            "secret_scan_passed": True,
            "timestamp": datetime.now(UTC).isoformat(),
        }
    )
    serialized = json.dumps(evidence, indent=2, default=str) + "\n"
    if leaks(serialized, tuple(value for value in found if value)) or e2e.SECRET_PATTERNS.search(
        serialized
    ):
        evidence = {
            key: evidence.get(key) for key in ("gate", "tested_commit", "run_id", "timestamp")
        }
        evidence.update(
            {
                "status": "FAIL",
                "reason": "evidence secret scan failed",
                "exit_code": 1,
                "secret_scan_passed": False,
            }
        )
        serialized = json.dumps(evidence, indent=2) + "\n"
        exit_code = 1
    EVIDENCE.parent.mkdir(parents=True, exist_ok=True)
    EVIDENCE.write_text(serialized, encoding="utf-8")
    return exit_code


def main() -> int:
    from scripts.intelligence_eval import evaluate

    run_id = uuid.uuid4().hex[:10]
    evidence: dict[str, Any] = {
        "gate": "document_intelligence",
        "tested_commit": e2e._commit(),
        "run_id": run_id,
    }
    memory = rq.available_memory_gib()
    evidence["memory_available_gib"] = round(memory, 2)
    if memory < MIN_MEMORY_GIB:
        return _finish(
            evidence, "BLOCKED", f"less than {MIN_MEMORY_GIB} GiB of memory available", 2
        )
    report = evaluate()
    evaluation = {
        "corpus_digest": report["corpus_digest"],
        "classification_accuracy": report["classification"]["accuracy"],
        "fields": report["fields"],
        "false_positive_count": len(report["false_positives"]),
        "false_negative_count": len(report["false_negatives"]),
        "debtor": report["debtor"],
        "claims": report["claims"],
        "forbidden_violations": report["forbidden_violations"],
        "holdout": {
            "digest": report["holdout"]["digest"],
            "documents": report["holdout"]["documents"],
            "classification_accuracy": report["holdout"]["classification"]["accuracy"],
            "fields": report["holdout"]["fields"],
            "false_positives": report["holdout"]["false_positives"],
            "false_negatives": report["holdout"]["false_negatives"],
            "debtor": report["holdout"]["debtor"],
            "claims": report["holdout"]["claims"],
            "forbidden_violations": report["holdout"]["forbidden_violations"],
        },
    }
    result = DiGateResult()
    found: tuple[str, ...] = ()
    try:
        found = run(result, evidence) or ()
    except PrerequisiteMissing as exc:
        reason = e2e._sanitize(str(exc))
        if reason.startswith("DEPENDENCY_BLOCKED"):
            evidence["dependency_status"] = "DEPENDENCY_BLOCKED"
        return _finish(evidence, "BLOCKED", reason, 2)
    except Exception as exc:  # noqa: BLE001 - any stage failure is a gate failure
        result.errors.append(f"{result.metrics.get('last_stage', 'setup')}: {e2e._first_line(exc)}")
    evidence.update(build_evidence(result, evaluation))
    status, reason = classify(result)
    if result.errors and status == "PASS":
        status, reason = "FAIL", "a gate stage raised an error"
    return _finish(evidence, status, reason, 0 if status == "PASS" else 1, found)


if __name__ == "__main__":
    os.environ.setdefault("PYTHONUNBUFFERED", "1")
    raise SystemExit(main())
