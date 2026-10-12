"""Precision and recall of the rules extractor on the frozen synthetic corpus.

The corpus digest is pinned: editing, adding or removing an example changes it and fails this
test, so the metrics can never come from a selection of convenient examples.
"""

from __future__ import annotations

from scripts import intelligence_eval
from scripts import intelligence_fixtures as corpus

FROZEN_DIGEST = "b2632c189ea3e12298696d6327e16c8010855d62048840d7ce894abc0f6e92ba"


def test_corpus_is_frozen() -> None:
    assert corpus.corpus_digest() == FROZEN_DIGEST
    assert len(corpus.LETTERS) == 16


def test_corpus_covers_the_required_cases() -> None:
    keys = {letter.key for letter in corpus.LETTERS}
    assert {
        "invoice_c1",
        "reminder_c1",
        "inkasso_c1",
        "invoice_c2_same_amount",
        "court_c2",
        "receipt_c4_paid",
        "shared_address_ambiguous",
        "statement_components",
        "misleading_legal_term",
        "unrelated",
        "injected_instructions",
    } <= keys
    assert set(corpus.PERSONS) == {"Person A", "Person B"}
    assert any(letter.scanned for letter in corpus.LETTERS)


def test_evaluation_runs_over_every_letter_and_reports_errors() -> None:
    report = intelligence_eval.evaluate()
    assert report["documents"] == len(corpus.LETTERS)
    assert report["corpus_digest"] == corpus.corpus_digest()
    for family in ("party", "amount", "identifier", "date", "status"):
        metrics = report["fields"][family]
        assert set(metrics) >= {"tp", "fp", "fn", "precision", "recall"}
    # Every error is listed by letter and type, never with its value.
    for item in report["false_positives"] + report["false_negatives"]:
        assert set(item) == {"letter", "fact_type"}


def test_evaluation_meets_the_declared_thresholds() -> None:
    report = intelligence_eval.evaluate()
    assert report["classification"]["accuracy"] == 1.0
    assert report["forbidden_violations"] == 0
    # High-severity failures: a wrong debtor or a falsely merged claim.
    assert report["debtor"]["wrong_person"] == 0
    assert report["claims"]["false_merges"] == 0
    for family, metrics in report["fields"].items():
        assert metrics["precision"] >= 0.95, family
        assert metrics["recall"] >= 0.90, family


def test_evaluation_report_contains_no_corpus_text() -> None:
    import json

    serialized = json.dumps(intelligence_eval.evaluate())
    assert not [line for line in corpus.all_text() if line in serialized]
