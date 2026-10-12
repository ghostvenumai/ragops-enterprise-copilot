"""Deterministic classification and entity extraction on the frozen synthetic corpus."""

from __future__ import annotations

from decimal import Decimal

import pytest
from scripts import intelligence_fixtures as corpus

from ragops.intelligence.rules import (
    DOCUMENT_CLASSES,
    EXTRACTOR_VERSION,
    FACT_TYPES,
    TAXONOMY_VERSION,
    SourceLine,
    analyze,
    lines_from_text,
    normalize_name,
)


def run(key: str):
    return analyze(lines_from_text(corpus.by_key(key).pages))


def pairs(analysis) -> set[tuple[str, str]]:
    return {(fact.fact_type, fact.value_repr()) for fact in analysis.facts}


def test_versions_and_taxonomy_are_explicit() -> None:
    assert TAXONOMY_VERSION == "doc-taxonomy-v1" and EXTRACTOR_VERSION == "rules-de-v1"
    assert set(DOCUMENT_CLASSES) == {
        "invoice",
        "payment_reminder",
        "formal_demand",
        "debt_collection_letter",
        "payment_order_or_court_notice",
        "enforcement_document",
        "creditor_statement",
        "loan_agreement",
        "payment_receipt",
        "settlement_agreement",
        "correspondence",
        "unrelated_document",
        "unknown",
    }
    assert {"debtor", "addressee", "creditor", "guarantor", "representative"} <= set(FACT_TYPES)
    assert FACT_TYPES["stated_total_claim"] == "amount"


@pytest.mark.parametrize("letter", corpus.LETTERS, ids=lambda letter: letter.key)
def test_every_corpus_letter_is_classified_as_annotated(letter) -> None:
    analysis = analyze(lines_from_text(letter.pages))
    assert analysis.primary_class == letter.document_class
    assert analysis.classification_status == "classified"


@pytest.mark.parametrize("letter", corpus.LETTERS, ids=lambda letter: letter.key)
def test_forbidden_facts_are_never_extracted(letter) -> None:
    assert not pairs(analyze(lines_from_text(letter.pages))) & letter.forbidden


def test_inkasso_letter_distinguishes_agency_creditor_and_debtor() -> None:
    found = pairs(run("inkasso_c1"))
    assert ("collection_agency", "beispiel inkasso gmbh") in found
    assert ("creditor", "versandhaus lindenhof kg") in found
    assert ("debtor", "anna beispiel") in found
    # The agency is never the creditor and the addressee is a separate role.
    assert ("creditor", "beispiel inkasso gmbh") not in found
    assert ("addressee", "anna beispiel") in found


def test_amount_semantics_are_typed_not_summed() -> None:
    facts = {fact.fact_type: fact for fact in run("inkasso_c1").facts if fact.category == "amount"}
    assert facts["principal_amount"].amount == Decimal("400.00")
    assert facts["interest_amount"].amount == Decimal("5.00")
    assert facts["collection_cost_amount"].amount == Decimal("40.00")
    assert facts["stated_total_claim"].amount == Decimal("445.00")
    assert all(fact.currency == "EUR" for fact in facts.values())
    assert all(isinstance(fact.amount, Decimal) for fact in facts.values())


def test_statement_components_payments_and_as_of_date() -> None:
    analysis = run("statement_components")
    by_type = {fact.fact_type: fact for fact in analysis.facts}
    assert by_type["payment_made_amount"].amount == Decimal("600.00")
    remaining = by_type["stated_remaining_balance"]
    assert remaining.amount == Decimal("2027.35") and remaining.as_of.isoformat() == "2026-03-31"
    # The principal is a component, not a balance; nothing is labelled "current".
    assert by_type["principal_amount"].as_of is None
    assert not any("current" in fact.fact_type for fact in analysis.facts)


def test_receipt_reports_payment_and_settlement_from_explicit_text() -> None:
    found = pairs(run("receipt_c4_paid"))
    assert ("payment_made_amount", "120.50 EUR") in found
    assert ("payment_reported", "true") in found and ("settled", "true") in found
    payment = next(f for f in run("receipt_c4_paid").facts if f.fact_type == "payment_made_amount")
    assert payment.as_of.isoformat() == "2026-03-03"


def test_court_notice_is_not_an_enforceable_title_and_objection_is_not_a_dispute() -> None:
    analysis = run("court_c2")
    found = pairs(analysis)
    assert analysis.primary_class == "payment_order_or_court_notice"
    assert ("court_case_number", "26-4471123-0-5") in found
    assert ("enforcement_mentioned", "true") not in found
    assert ("disputed", "true") not in found
    assert not any("enforceable" in fact.fact_type for fact in analysis.facts)


def test_negated_legal_terms_do_not_classify_the_document() -> None:
    analysis = run("misleading_legal_term")
    assert analysis.primary_class == "correspondence"
    assert {c.document_class for c in analysis.classifications} == {"correspondence"}


def test_injected_instructions_are_ignored_and_counted() -> None:
    analysis = run("injected_instructions")
    found = pairs(analysis)
    assert analysis.ignored_instruction_lines == 2
    assert ("debtor", "bernd probe") not in found
    assert ("stated_total_claim", "0.00 EUR") not in found
    assert ("settled", "true") not in found
    assert ("stated_remaining_balance", "400.00 EUR") in found


def test_provenance_spans_point_at_the_stated_text() -> None:
    letter = corpus.by_key("inkasso_c1")
    analysis = analyze(lines_from_text(letter.pages))
    for fact in analysis.facts:
        line = fact.line
        assert line.page_number == 1 and line.method == "native_text"
        assert 0 <= fact.start < fact.end <= len(line.text)
        assert line.text[fact.start : fact.end] == fact.raw_value
        # Native text never gets an invented OCR box.
        assert line.box is None and line.ocr_line is None


def test_ocr_facts_keep_line_boxes_and_low_quality_stays_review_required() -> None:
    lines = [
        SourceLine(1, 0, "Zahlungserinnerung", "ocr", "review_required", 0, (0.1, 0.1, 0.5, 0.15)),
        SourceLine(
            1, 1, "Offener Betrag: 87,40 EUR", "ocr", "review_required", 1, (0.1, 0.2, 0.6, 0.25)
        ),
    ]
    analysis = analyze(lines)
    amount = next(fact for fact in analysis.facts if fact.category == "amount")
    assert amount.line.box == (0.1, 0.2, 0.6, 0.25) and amount.line.ocr_line == 1
    assert amount.quality_state == "review_required"
    assert analysis.review_required is True


def test_unlabelled_amount_and_currency_free_amount_are_not_guessed() -> None:
    analysis = analyze(lines_from_text([["Rechnung", "Betrag: 12,00 EUR", "Summe: 15,00"]]))
    types = {fact.fact_type: fact for fact in analysis.facts if fact.category == "amount"}
    assert types["unclassified_amount"].amount == Decimal("12.00")
    total = types["stated_total_claim"]
    assert total.currency is None and total.quality_state == "review_required"


def test_ambiguous_headings_compete_and_require_review() -> None:
    analysis = analyze(lines_from_text([["Rechnung", "Zahlungsbestätigung", "Betrag: 1,00 EUR"]]))
    assert analysis.classification_status == "ambiguous"
    assert analysis.primary_class is None and analysis.review_required is True
    assert {c.document_class for c in analysis.classifications} >= {"invoice", "payment_receipt"}


def test_empty_document_is_unknown() -> None:
    analysis = analyze([])
    assert analysis.primary_class == "unknown" and analysis.facts == ()


def test_multiple_debtors_on_one_label_are_separate_facts() -> None:
    analysis = analyze(lines_from_text([["Mahnung", "Schuldner: Anna Beispiel und Bernd Probe"]]))
    debtors = sorted(fact.normalized for fact in analysis.facts if fact.fact_type == "debtor")
    assert debtors == ["anna beispiel", "bernd probe"]


def test_name_normalization_is_exact_not_fuzzy() -> None:
    assert normalize_name("Frau  Anna Beispiel") == "anna beispiel"
    assert normalize_name("Herrn Dr. Bernd Probe") == "bernd probe"
    assert normalize_name(corpus.SIMILAR_NAME) != normalize_name("Anna Beispiel")


def test_invalid_dates_are_rejected() -> None:
    analysis = analyze(lines_from_text([["Rechnung", "Rechnungsdatum: 31.02.2026"]]))
    assert not [fact for fact in analysis.facts if fact.category == "date"]


def test_fact_keys_are_stable_and_unique() -> None:
    first, second = run("inkasso_c1"), run("inkasso_c1")
    keys = [fact.key() for fact in first.facts]
    assert keys == [fact.key() for fact in second.facts]
    assert len(keys) == len(set(keys))
