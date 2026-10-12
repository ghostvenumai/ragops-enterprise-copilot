"""Precision/recall of the ``rules-de-v1`` extractor on the frozen synthetic corpus.

Runs over every letter of ``scripts/intelligence_fixtures.py`` (no selection) and reports per
field family true/false positives and negatives, classification accuracy, debtor attribution
and claim grouping errors. False positives and negatives are listed by letter key and fact type
only — never with a value or any corpus text.

Usage: ``.venv/bin/python scripts/intelligence_eval.py [--write]`` (``--write`` stores
``evidence/product-v1/document-intelligence-eval.json``).
"""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ragops.intelligence.claims import claim_keys  # noqa: E402
from ragops.intelligence.identity import debtor_status, propose_links  # noqa: E402
from ragops.intelligence.rules import (  # noqa: E402
    EXTRACTOR_VERSION,
    FACT_TYPES,
    TAXONOMY_VERSION,
    analyze,
    lines_from_text,
    normalize_name,
)
from scripts import intelligence_fixtures as corpus  # noqa: E402

OUT = ROOT / "evidence" / "product-v1" / "document-intelligence-eval.json"


def _ratio(numerator: int, denominator: int) -> float:
    return round(numerator / denominator, 4) if denominator else 1.0


def evaluate_letters(letters: tuple[corpus.Letter, ...]) -> dict[str, Any]:
    aliases = {
        normalize_name(alias): label for label, names in corpus.PERSONS.items() for alias in names
    }
    counts: dict[str, dict[str, int]] = defaultdict(lambda: {"tp": 0, "fp": 0, "fn": 0})
    false_positives: list[dict[str, str]] = []
    false_negatives: list[dict[str, str]] = []
    correct_class = forbidden = wrong_person = missed_person = status_errors = 0
    keys_by_letter: dict[str, set[str]] = {}
    for letter in letters:
        analysis = analyze(lines_from_text(letter.pages))
        correct_class += analysis.primary_class == letter.document_class
        found = {(fact.fact_type, fact.value_repr()) for fact in analysis.facts}
        forbidden += len(found & letter.forbidden)
        for fact_type, value in sorted(found | letter.facts):
            family = FACT_TYPES[fact_type]
            if (fact_type, value) in found and (fact_type, value) in letter.facts:
                counts[family]["tp"] += 1
            elif (fact_type, value) in found:
                counts[family]["fp"] += 1
                false_positives.append({"letter": letter.key, "fact_type": fact_type})
            else:
                counts[family]["fn"] += 1
                false_negatives.append({"letter": letter.key, "fact_type": fact_type})
        links = [link for link in propose_links(analysis.facts, aliases) if link.role == "debtor"]
        identified = {link.person_id for link in links if link.state == "evidenced"}
        wrong_person += len(identified - set(letter.debtors))
        missed_person += len(set(letter.debtors) - identified)
        has_label = any(fact.fact_type == "debtor" for fact in analysis.facts)
        status = debtor_status(has_label, [link.state for link in links])
        status_errors += status != letter.debtor_status
        keys_by_letter[letter.key] = {
            key.digest for key in claim_keys(analysis.facts, analysis.primary_class)
        }
    groups: dict[str, set[str]] = defaultdict(set)
    for key, digests in keys_by_letter.items():
        for digest in digests:
            groups[digest].add(key)
    expected_of = {letter.key: letter.claim for letter in letters}
    expected_claims: dict[str, set[str]] = defaultdict(set)
    for letter in letters:
        if letter.claim:
            expected_claims[letter.claim].add(letter.key)
    false_merges = sum(
        1
        for members in groups.values()
        if len({expected_of[m] for m in members}) > 1 or any(not expected_of[m] for m in members)
    )
    found_sets = {frozenset(members) for members in groups.values()}
    missed_merges = sum(
        1 for members in expected_claims.values() if frozenset(members) not in found_sets
    )
    fields = {
        family: {
            **values,
            "precision": _ratio(values["tp"], values["tp"] + values["fp"]),
            "recall": _ratio(values["tp"], values["tp"] + values["fn"]),
        }
        for family, values in sorted(counts.items())
    }
    return {
        "documents": len(letters),
        "classification": {
            "correct": correct_class,
            "accuracy": _ratio(correct_class, len(letters)),
        },
        "fields": fields,
        "false_positives": false_positives,
        "false_negatives": false_negatives,
        "forbidden_violations": forbidden,
        "debtor": {
            "wrong_person": wrong_person,
            "missed_person": missed_person,
            "status_errors": status_errors,
        },
        "claims": {
            "expected_groups": len(expected_claims),
            "found_groups": len(groups),
            "false_merges": false_merges,
            "missed_merges": missed_merges,
        },
    }


def evaluate() -> dict[str, Any]:
    """Design corpus (in-sample, gated) and held-out letters (reported as measured)."""
    from scripts.intelligence_holdout import HOLDOUT, holdout_digest

    return {
        "extractor_version": EXTRACTOR_VERSION,
        "taxonomy_version": TAXONOMY_VERSION,
        "corpus_digest": corpus.corpus_digest(),
        "selection": "all_letters_frozen_corpus",
        "note": "design corpus metrics are in-sample: the rules were written against it",
        **evaluate_letters(corpus.LETTERS),
        "holdout": {
            "digest": holdout_digest(),
            "note": "written after the rules; rules not tuned on it",
            **evaluate_letters(HOLDOUT),
        },
    }


def main() -> int:
    report = evaluate()
    if "--write" in sys.argv:
        OUT.parent.mkdir(parents=True, exist_ok=True)
        OUT.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
