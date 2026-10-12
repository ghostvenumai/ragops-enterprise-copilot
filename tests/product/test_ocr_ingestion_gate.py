"""ocr_ingestion gate: classification, evidence contract, dependency blocking and RC state."""

from __future__ import annotations

import json

import pytest
from scripts import ocr_ingestion_gate as gate
from scripts import rc_live_gate, security_gate

ALL_CHECKS = sorted(gate.REQUIRED)


def passing() -> gate.OcrGateResult:
    result = gate.OcrGateResult()
    for name in ALL_CHECKS:
        result.check(name, True)
    result.ocr_engine, result.ocr_engine_version = "tesseract", "5.3.4"
    result.similarities = {"scan_page_2": 0.97}
    result.chunk_count = result.vector_count = 9
    return result


def test_a_complete_run_passes() -> None:
    assert gate.classify(passing()) == (
        "PASS",
        "scanned documents were read locally, indexed and cited with their pages",
    )


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ({"tenant_leakage": 1}, "tenant_leakage must be 0"),
        ({"cross_tenant_leakage": 1}, "cross_tenant_leakage must be 0"),
        ({"paid_provider_calls": 1}, "paid_provider_calls must be 0"),
        ({"ocr_engine": "scripted"}, "the real OCR engine was not used"),
        ({"similarities": {"jpg_page_1": 0.5}}, "recognition below the accuracy criterion"),
    ],
)
def test_each_violation_fails(change, reason) -> None:
    result = passing()
    for name, value in change.items():
        setattr(result, name, value)
    status, message = gate.classify(result)
    assert status == "FAIL" and message.startswith(reason)


def test_missing_or_failed_checks_fail() -> None:
    assert gate.classify(gate.OcrGateResult())[0] == "FAIL"
    for check in ("citation_page_verified", "poor_quality_flagged", "gate_database_removed"):
        result = passing()
        result.checks[check] = False
        assert gate.classify(result) == ("FAIL", f"ocr_ingestion invariant failed: {check}")


def test_evidence_carries_every_field_the_security_control_needs() -> None:
    evidence = gate.build_evidence(passing())
    (control,) = [c for c in security_gate.CONTROLS if c.id == "LIVE-13"]
    for key, expected in control.required:
        if key in ("status", "secret_scan_passed"):
            continue
        assert evidence[key] == expected, key
    for key, minimum in control.minimums:
        assert evidence[key] >= minimum
    assert evidence["cleanup_status"] == "PASS" and evidence["paid_provider_calls"] == 0


def test_a_missing_engine_blocks_the_gate_instead_of_faking_ocr(monkeypatch, tmp_path) -> None:
    from ragops.ingestion import ocr

    monkeypatch.setattr(gate, "EVIDENCE", tmp_path / "ocr-ingestion.json")
    monkeypatch.setattr(gate.rq, "available_memory_gib", lambda: 8.0)
    monkeypatch.setattr(ocr.shutil, "which", lambda name: None)
    assert gate.main() == 2
    written = json.loads((tmp_path / "ocr-ingestion.json").read_text())
    assert written["status"] == "BLOCKED"
    assert written["dependency_status"] == "DEPENDENCY_BLOCKED"
    assert "tesseract-ocr-deu" in written["reason"]


def test_document_text_and_reference_numbers_never_reach_the_evidence(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(gate, "EVIDENCE", tmp_path / "ocr-ingestion.json")
    number = "482913"
    evidence = {
        "gate": "ocr_ingestion",
        "tested_commit": "abc",
        "detail": f"Vorgangsnummer {number}",
    }
    assert gate._finish(evidence, "PASS", "ok", 0, (number, "Vorgangsnummer")) == 1
    raw = (tmp_path / "ocr-ingestion.json").read_text()
    assert number not in raw and json.loads(raw)["secret_scan_passed"] is False


def test_documents_are_synthetic_and_declare_their_expectations() -> None:
    docs = gate.samples("run", {key: "123456" for key in ("scan", "native", "mixed", "jpg", "png")})
    assert docs["scan"].routes == ["scanned_image", "scanned_image"]
    assert docs["mixed"].routes[-1] == "mixed_content" and len(docs["mixed"].routes) == 5
    assert {docs["jpg"].mime, docs["png"].mime} == {"image/jpeg", "image/png"}
    for sample in docs.values():
        for page in sample.exact:
            assert 1 <= page <= len(sample.routes)
    assert gate.similarity(["Datum: 03.02.2026"], "Datum: 03.02.2026") == 1.0
    assert gate.similarity(["Datum: 03.02.2026"], "D4tum O3.O2.2O26") < gate.MIN_SIMILARITY


def test_rc_runner_runs_ocr_ingestion_before_security() -> None:
    assert len(rc_live_gate.GATES) == 20
    assert rc_live_gate.GATES[-3:] == ("ocr_ingestion", "document_intelligence", "security")
    assert "ocr_ingestion" in rc_live_gate.DETAILED_EVIDENCE_GATES
    prior = {name: "PASS" for name in rc_live_gate.GATES if name != "ocr_ingestion"}
    merged = rc_live_gate.merge_gate_statuses(
        prior, {"ocr_ingestion"}, {"ocr_ingestion": {"status": "PASS"}}
    )
    assert all(value == "PASS" for value in merged.values()) and len(merged) == 20
    assert rc_live_gate.merge_gate_statuses(prior, set(), {})["ocr_ingestion"] == "BLOCKED"


def test_security_has_thirty_one_controls_with_live_fourteen() -> None:
    assert len(security_gate.CONTROLS) == 31
    live = [control for control in security_gate.CONTROLS if control.kind == "live"]
    assert len(live) == 14
    (control,) = [c for c in live if c.evidence_file == "ocr-ingestion.json"]
    assert control.id == "LIVE-13"
    required = dict(control.required)
    assert required["ocr_engine"] == "tesseract" and required["paid_provider_calls"] == 0
    assert required["cross_tenant_leakage"] == 0 and required["poor_quality_flagged"] is True


def test_the_gate_never_uses_scripted_ocr_or_paid_providers() -> None:
    source = (gate.ROOT / "scripts" / "ocr_ingestion_gate.py").read_text()
    assert "ScriptedEngine" not in source and "ocr_helpers" not in source
    for paid in ("OpenAIProvider", "AzureOpenAI", "RAGOPS_ALLOW_PAID_INTEGRATION_TESTS"):
        assert paid not in source


# --------------------------------------------------------------------------- fixed retrieval

KEYS = ("scan", "native", "mixed", "jpg", "png")


def corpus(numbers: dict[str, str]) -> dict[str, str]:
    """Chunk text per page (one chunk per page) of the fixed gate documents."""
    docs = gate.samples("run", numbers)
    return {
        f"{key}:{page}": "\n".join(lines)
        for key, sample in docs.items()
        for page, lines in sample.text.items()
    }


def vectors(texts: dict[str, str]) -> dict[str, list[float]]:
    from ragops.vector.embedding import DeterministicEmbeddingProvider

    provider = DeterministicEmbeddingProvider(64)
    return dict(zip(texts, provider.embed(list(texts.values())), strict=True))


def test_every_page_of_every_fixture_declares_its_text() -> None:
    docs = gate.samples("run", {key: "123456" for key in KEYS})
    for sample in docs.values():
        assert sorted(sample.text) == list(range(1, len(sample.routes) + 1))
        for page, lines in sample.expected.items():
            assert sample.text[page] == lines


def test_fixed_questions_are_constant_and_never_carry_an_identifier() -> None:
    for key, (page, question) in gate.RETRIEVAL_QUERIES.items():
        assert key in KEYS and page in gate.samples("r", dict.fromkeys(KEYS, "1"))[key].exact
        assert not any(char.isdigit() for char in question)
    source = (gate.ROOT / "scripts" / "ocr_ingestion_gate.py").read_text()
    # Questions are never built from the run's numbers or chosen by measured similarity.
    assert "RETRIEVAL_QUERIES.items()" in source
    assert 'f"Was betrifft die Vorgangsnummer {numbers' not in source


def _bucket_representatives() -> list[str]:
    """One six-digit number per hash bucket of the 64-dimensional embedding."""
    from ragops.vector.embedding import DeterministicEmbeddingProvider

    provider, found, candidate = DeterministicEmbeddingProvider(64), {}, 100_000
    while len(found) < 64:
        vector = provider.embed([str(candidate)])[0]
        found.setdefault(vector.index(max(vector)), str(candidate))
        candidate += 1
    return list(found.values())


def test_ranking_margin_holds_for_every_hash_bucket_of_every_reference_number() -> None:
    """Worst case: the target's number in its least helpful bucket, every other chunk's
    number in its most helpful one. Relevance therefore never depends on the values."""
    marker = "{NUMBER}"
    templates = corpus(dict.fromkeys(KEYS, marker))
    variants: dict[str, list[list[float]]] = {}
    for name, text in templates.items():
        texts = (
            [text.replace(marker, n) for n in _bucket_representatives()]
            if marker in text
            else [text]
        )
        variants[name] = list(vectors({str(i): t for i, t in enumerate(texts)}).values())
    from ragops.vector.embedding import DeterministicEmbeddingProvider

    provider = DeterministicEmbeddingProvider(64)
    for key, (page, question) in gate.RETRIEVAL_QUERIES.items():
        query = provider.embed([question])[0]

        def score(vector: list[float], query: list[float] = query) -> float:
            return sum(a * b for a, b in zip(query, vector, strict=True))

        target = min(score(v) for v in variants[f"{key}:{page}"])
        best_other = max(
            score(v) for name, group in variants.items() if name != f"{key}:{page}" for v in group
        )
        assert target - best_other >= gate.MIN_SCORE_MARGIN, (key, target, best_other)


def test_ranking_is_stable_across_many_random_identifiers() -> None:
    # The gate's own generator; the worst-case test above covers every possible value.
    for _ in range(200):
        numbers = {key: gate._number() for key in KEYS}
        stored = vectors(corpus(numbers))
        for key, (page, question) in gate.RETRIEVAL_QUERIES.items():
            assert gate.rank(question, stored)[0][1] == f"{key}:{page}"
            assert gate.score_margin(question, stored, f"{key}:{page}") >= gate.MIN_SCORE_MARGIN


def test_score_margin_is_negative_when_the_expected_chunk_is_not_first() -> None:
    stored = {"a": [1.0, 0.0], "b": [0.0, 1.0]}
    assert gate.score_margin("x", stored, "missing") == -1.0
    question = "Zahlungserinnerung"
    best = gate.rank(question, vectors({"t": "Zahlungserinnerung", "o": "Mahnbescheid"}))
    assert best[0][1] == "t"
    reverse = vectors({"t": "Mahnbescheid", "o": "Zahlungserinnerung"})
    assert gate.score_margin(question, reverse, "t") < 0


class _Point:
    def __init__(self, chunk_id: str, page: int, vector: list[float]) -> None:
        self.payload = {"chunk_id": chunk_id, "page_number": page}
        self.vector = vector


class _Store:
    def __init__(self, points: list[_Point], top: str) -> None:
        self._points, self._top = points, top

    def tenant_points(self, tenant_id: str) -> list[_Point]:
        return self._points

    def search(self, question: str, tenant_id: str, dimension: int) -> str:
        return self._top


def _preflight(top_text: str, other_text: str, top: str) -> tuple[bool, str | None]:
    page, question = gate.RETRIEVAL_QUERIES["scan"]
    stored = vectors({"t": top_text, "o": other_text})
    points = [_Point("t", page, stored["t"]), _Point("o", 1, stored["o"])]
    sample = gate.samples("r", dict.fromkeys(KEYS, "1"))["scan"]
    sample.points = [points[0]]
    result = gate.OcrGateResult()
    expected = gate.retrieval_preflight(_Store(points, top), result, sample, page, question)
    return result.checks["retrieval_preflight_verified"], expected


def test_preflight_passes_only_for_the_expected_first_chunk_with_margin() -> None:
    letter = "\n".join(gate.letter("1"))
    other = "Inhaltsverzeichnis der Akte"
    assert _preflight(letter, other, "t") == (True, "t")
    # Qdrant answering another chunk first, a reversed ranking or a missing margin all FAIL.
    assert _preflight(letter, other, "o") == (False, None)
    assert _preflight(other, letter, "t") == (False, None)
    assert _preflight(letter, letter, "t") == (False, None)
