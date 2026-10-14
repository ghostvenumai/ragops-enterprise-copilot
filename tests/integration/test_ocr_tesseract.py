"""Real OCR with the locally installed Tesseract engine and German language data.

These tests never use scripted OCR. They are skipped only when the engine is not installed;
the ocr_ingestion release gate requires the engine and fails as DEPENDENCY_BLOCKED instead.
Exact-string accuracy (dates, amounts, umlauts) is checked separately from retrievability.
"""

from __future__ import annotations

import shutil

import pytest
from scripts.ocr_fixtures import (
    PdfPage,
    full_page,
    image_bytes,
    jpeg_with_orientation,
    native_pdf,
    pdf,
    scan_image,
    scanned_pdf,
)

from ragops.ingestion.ocr import OcrLimits, TesseractEngine
from ragops.ingestion.pages import DocumentExtractor, ExtractionMethod, PageClass

pytestmark = pytest.mark.skipif(
    shutil.which("tesseract") is None or shutil.which("pdftoppm") is None,
    reason="local Tesseract or poppler is not installed",
)

LETTER = [
    "Zahlungserinnerung",
    "Rechnungsnummer: RE-2026-0815",
    "Datum: 03.02.2026",
    "Offener Betrag: 1.234,56 EUR",
    "Bitte überweisen Sie bis zum 15.03.2026.",
    "Grüße aus Köln",
]
EXACT = ("RE-2026-0815", "03.02.2026", "1.234,56", "15.03.2026", "überweisen", "Grüße", "Köln")


def engine() -> TesseractEngine:
    return TesseractEngine()


def test_engine_version_and_german_language_data_are_installed() -> None:
    info = engine().probe()
    assert info.name == "tesseract" and info.version.split(".")[0] >= "4"
    assert {"deu", "eng"} <= set(info.languages)


def _assert_exact(text: str) -> None:
    missing = [token for token in EXACT if token not in text]
    assert not missing, f"exact tokens not recognized: {missing}"


def test_scanned_pdf_is_recognized_exactly() -> None:
    result = DocumentExtractor(engine=engine()).extract("brief.pdf", scanned_pdf([LETTER]))
    page = result.pages[0]
    assert page.classification is PageClass.SCANNED_IMAGE, page.quality.flags
    assert page.engine == "tesseract" and page.quality.state == "accepted"
    assert page.quality.mean_confidence is not None and page.quality.mean_confidence >= 80
    _assert_exact(page.text)
    assert all(line.box.valid() for line in page.lines)


def test_jpeg_png_and_exif_rotated_jpeg_are_recognized() -> None:
    image = scan_image(LETTER, size=(1240, 900), text_px=34)
    for name, content in (
        ("brief.png", image_bytes(image, "PNG")),
        ("brief.jpg", image_bytes(image, "JPEG", quality=92)),
        ("gedreht.jpg", jpeg_with_orientation(image, 6)),
    ):
        page = DocumentExtractor(engine=engine()).extract(name, content).pages[0]
        assert page.classification is PageClass.SCANNED_IMAGE, (name, page.quality.flags)
        _assert_exact(page.text)


@pytest.mark.parametrize("number", ["202188", "792928", "128228", "887142"])
def test_reference_numbers_and_umlauts_survive_small_images_with_legible_text(number) -> None:
    """Regression (ocr_ingestion gate, ENT-12-UI closure): these numbers come from runs of the
    gate's PNG and JPEG samples in which the old upscale (1.29x) of the 1240x1000 page made
    Tesseract read an "8" as "3" or drop the umlaut words, at high confidence.
    """
    statement = [
        "Hausverwaltung Südstadt",
        "Nebenkostenabrechnung",
        f"Kundennummer {number}",
        "Zeitraum: 2025",
        "Nachzahlung: 412,75 EUR",
        "Fällig zum 30.09.2026.",
        "Heizöl und Müllgebühren",
    ]
    receipt = [
        "Hotel Rheinblick Düsseldorf",
        "Reisekostenbeleg",
        f"Belegnummer {number}",
        "Anreise: 07.06.2026",
        "Übernachtung: 186,40 EUR",
        "Frühstück inklusive.",
        "Gezahlt mit Firmenkarte",
    ]
    for name, content, exact in (
        (
            "bild.png",
            image_bytes(scan_image(statement, size=(1240, 1000)), "PNG"),
            (
                number,
                "Nebenkostenabrechnung",
                "30.09.2026",
                "412,75",
                "Südstadt",
                "Fällig",
                "Müllgebühren",
            ),
        ),
        (
            "foto.jpg",
            jpeg_with_orientation(scan_image(receipt, size=(1240, 1000)), 6),
            (
                number,
                "Reisekostenbeleg",
                "07.06.2026",
                "186,40",
                "Düsseldorf",
                "Übernachtung",
                "Frühstück",
            ),
        ),
    ):
        page = DocumentExtractor(engine=engine()).extract(name, content).pages[0]
        text = " ".join(page.text.split())
        missing = [token for token in exact if token not in text]
        assert not missing, (name, number, missing)
        assert not any(step.startswith("upscale:") for step in page.transformations), name


def _gate_letter(number: str) -> list[str]:
    """The ocr_ingestion gate's scanned letter (scripts/ocr_ingestion_gate.py: letter)."""
    return [
        "Stadtwerke Musterstadt GmbH",
        "Zahlungserinnerung",
        f"Vorgangsnummer {number}",
        "Datum: 03.02.2026",
        "Offener Betrag: 1.234,56 EUR",
        "Bitte überweisen Sie bis zum 15.03.2026.",
        "Mit freundlichen Grüßen aus Köln",
    ]


@pytest.mark.parametrize("number", ["875167", "987657", "231467", "777777"])
def test_reference_numbers_on_a_150_dpi_scan_are_read_exactly(number) -> None:
    """Regression (ocr_ingestion gate, ENT-13.1 closure): rendered at a fixed 300 dpi, the
    gate's 150 dpi scan was only resampled 2x and Tesseract read these numbers as "87516/",
    "98/7657", "23146/" and "77777]" at high confidence. Fixed numbers, never random ones.
    """
    cover = ["Anschreiben zur Akte", "Allgemeine Hinweise zur Ablage", "Seite eins der Sendung"]
    result = DocumentExtractor(engine=engine()).extract(
        "scan.pdf", scanned_pdf([cover, _gate_letter(number)])
    )
    page = result.pages[1]
    assert page.page_number == 2 and page.classification is PageClass.SCANNED_IMAGE
    assert "native_dpi:150" in page.transformations
    text = " ".join(page.text.split())
    exact = (number, "Zahlungserinnerung", "03.02.2026", "1.234,56", "15.03.2026", "überweisen")
    missing = [token for token in (*exact, "Grüßen", "Köln") if token not in text]
    assert not missing, (number, missing)
    # Provenance: the number's line box is where the scan drew it (x=110, y=270 of 1240x1754).
    word = next(word for word in page.words if word.text == number)
    assert word.line is not None
    box = page.lines[word.line].box
    assert box.valid() and 0.07 <= box.x0 <= 0.11 and 0.13 <= box.y0 <= 0.18


def test_small_text_on_a_150_dpi_scan_is_still_read_at_the_configured_resolution() -> None:
    page = (
        DocumentExtractor(engine=engine())
        .extract("klein.pdf", scanned_pdf([LETTER], text_px=18))
        .pages[0]
    )
    assert not any(step.startswith("native_dpi:") for step in page.transformations)
    assert page.classification is PageClass.SCANNED_IMAGE, page.quality.flags
    _assert_exact(page.text)


def test_a_300_dpi_scan_is_read_exactly_at_the_configured_resolution() -> None:
    image = scan_image(LETTER, size=(2480, 3508), text_px=68, origin=(220, 280))
    page = (
        DocumentExtractor(engine=engine())
        .extract("fein.pdf", pdf([PdfPage(images=[full_page(image)])]))
        .pages[0]
    )
    assert not any(step.startswith("native_dpi:") for step in page.transformations)
    assert page.classification is PageClass.SCANNED_IMAGE, page.quality.flags
    _assert_exact(page.text)


def test_boxes_point_at_the_text_on_the_original_page() -> None:
    image = scan_image(["Kennung ZX-4711"], size=(1240, 900), text_px=40, origin=(600, 700))
    page = DocumentExtractor(engine=engine()).extract("box.png", image_bytes(image, "PNG")).pages[0]
    line = next(line for line in page.lines if line.word_count)
    # The text starts at x=600/1240≈0.48 and y=700/900≈0.78 of the page.
    assert 0.45 <= line.box.x0 <= 0.52 and 0.74 <= line.box.y0 <= 0.82


def test_mixed_pdf_routes_pages_and_keeps_numbers() -> None:
    document = pdf(
        [
            PdfPage(text_lines=["Deckblatt der Akte mit ausreichend nativem Text"]),
            PdfPage(images=[full_page(scan_image(LETTER))]),
            PdfPage(text_lines=["Anlage drei bleibt nativer Text ohne OCR"]),
        ]
    )
    result = DocumentExtractor(engine=engine()).extract("akte.pdf", document)
    methods = [(page.page_number, page.method) for page in result.pages]
    assert methods == [
        (1, ExtractionMethod.NATIVE_TEXT),
        (2, ExtractionMethod.OCR),
        (3, ExtractionMethod.NATIVE_TEXT),
    ]
    _assert_exact(result.pages[1].text)


def test_unreadable_noise_requires_review_and_timeouts_are_bounded() -> None:
    noise = scan_image([], noise=0.25)
    page = (
        DocumentExtractor(engine=engine()).extract("noise.png", image_bytes(noise, "PNG")).pages[0]
    )
    assert page.classification in {PageClass.OCR_REVIEW_REQUIRED, PageClass.EMPTY_PAGE}
    tight = DocumentExtractor(engine=engine(), limits=OcrLimits(page_timeout_seconds=0.01))
    page = tight.extract("brief.pdf", scanned_pdf([LETTER])).pages[0]
    assert page.classification is PageClass.UNREADABLE and "ocr_timeout" in page.quality.flags


def test_native_text_pdf_is_not_sent_to_the_engine(monkeypatch) -> None:
    real = engine()
    calls: list[object] = []
    monkeypatch.setattr(real, "recognize", lambda *args, **kwargs: calls.append(args) or [])
    result = DocumentExtractor(engine=real).extract("n.pdf", native_pdf([LETTER]))
    assert calls == [] and result.pages[0].method is ExtractionMethod.NATIVE_TEXT
