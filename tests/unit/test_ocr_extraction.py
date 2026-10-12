"""Per-page extraction routing, preprocessing records, quality gate and coordinate mapping.

Routing is proven with a scripted OCR engine that records each call; recognition accuracy
itself is proven against the real Tesseract engine in tests/integration/test_ocr_tesseract.py.
"""

from __future__ import annotations

import shutil

import pytest
from PIL import Image
from scripts.ocr_fixtures import (
    A4_POINTS,
    PdfPage,
    blank_image,
    full_page,
    image_bytes,
    jpeg_with_orientation,
    native_pdf,
    pdf,
    scan_image,
    scanned_pdf,
)
from tests.security.ocr_helpers import ScriptedEngine, words

from ragops.ingestion.ocr import (
    COORDINATE_SPACE,
    OcrEngineUnavailable,
    OcrLimits,
    OcrWord,
    TesseractEngine,
)
from ragops.ingestion.pages import (
    Box,
    DocumentExtractor,
    ExtractionMethod,
    PageClass,
    assess_ocr,
)
from ragops.ingestion.raster import Affine, prepare_for_ocr

pytestmark = pytest.mark.skipif(shutil.which("pdftoppm") is None, reason="poppler missing")

NATIVE = ["Prüfbescheid vom 03.02.2026", "Betrag: 1.234,56 € fällig"]
SCANNED = ["Mahnung Nummer 4711", "Fällig am 15.03.2026"]


def extractor(engine: ScriptedEngine, **limits: object) -> DocumentExtractor:
    return DocumentExtractor(engine=engine, limits=OcrLimits(**limits))  # type: ignore[arg-type]


def test_native_pdf_uses_the_text_layer_and_never_runs_ocr() -> None:
    engine = ScriptedEngine(words("darf nicht vorkommen"))
    result = extractor(engine).extract("a.pdf", native_pdf([NATIVE, ["Zweite Seite"]]))
    assert engine.calls == []
    assert [page.page_number for page in result.pages] == [1, 2]
    first = result.pages[0]
    assert first.classification is PageClass.NATIVE_TEXT
    assert first.method is ExtractionMethod.NATIVE_TEXT
    assert "Prüfbescheid vom 03.02.2026" in first.text and "1.234,56 €" in first.text
    assert first.quality.state == "not_applicable" and not result.review_required


def test_image_only_pdf_is_ocr_processed_per_page() -> None:
    engine = ScriptedEngine(words("Mahnung Nummer 4711 Fällig am 15.03.2026"))
    result = extractor(engine).extract("s.pdf", scanned_pdf([SCANNED]))
    page = result.pages[0]
    assert len(engine.calls) == 1
    assert page.classification is PageClass.SCANNED_IMAGE
    assert page.method is ExtractionMethod.OCR
    assert page.text.split() == "Mahnung Nummer 4711 Fällig am 15.03.2026".split()
    assert page.engine == "scripted" and page.language == "deu+eng"
    assert page.coordinate_space == COORDINATE_SPACE
    assert page.unit == "pt" and (page.width, page.height) == A4_POINTS
    assert page.lines and all(line.box.valid() for line in page.lines)
    assert all(word.line is not None for word in page.words)


def test_seven_page_pdf_keeps_original_numbers_of_scanned_pages_3_and_5() -> None:
    pages = [
        PdfPage(images=[full_page(scan_image([f"Scan Seite {n}"]))])
        if n in (3, 5)
        else PdfPage(text_lines=[f"Textseite {n} mit genug Inhalt für die Erkennung"])
        for n in range(1, 8)
    ]
    engine = ScriptedEngine(words("Gescannter Inhalt dieser Seite"))
    result = extractor(engine).extract("seven.pdf", pdf(pages))
    assert [page.page_number for page in result.pages] == [1, 2, 3, 4, 5, 6, 7]
    assert {page.page_number for page in result.pages if page.method is ExtractionMethod.OCR} == {
        3,
        5,
    }
    assert len(engine.calls) == 2
    assert "Textseite 4" in result.pages[3].text


def test_mixed_page_keeps_native_text_and_ocrs_only_the_image_region() -> None:
    width, height = A4_POINTS
    picture = scan_image(["Stempel Eingang"], size=(1240, 600))
    page = PdfPage(
        text_lines=["Sehr geehrte Damen und Herren, anbei die Unterlagen zum Vorgang."],
        images=[(picture, (0.0, 0.0, float(width), height * 0.4))],
    )
    engine = ScriptedEngine(words("Stempel Eingang\nSehr geehrte Damen und Herren,"))
    result = extractor(engine).extract("mixed.pdf", pdf([page]))
    extracted = result.pages[0]
    assert extracted.classification is PageClass.MIXED_CONTENT
    assert extracted.method is ExtractionMethod.HYBRID
    assert len(engine.calls) == 1
    full_height = round(height / 72 * 300)
    assert engine.calls[0][1] < full_height * 0.6  # only the image region was recognized
    region = extracted.ocr_regions[0]
    assert region.y0 >= 0.55 and region.y1 == pytest.approx(1.0, abs=0.01)
    assert extracted.text.count("Sehr geehrte Damen und Herren") == 1  # no duplicate text
    assert "Stempel Eingang" in extracted.text
    ocr_words = [word.text for word in extracted.words if word.line is not None]
    assert ocr_words == ["Stempel", "Eingang"]


def test_invisible_text_layer_is_not_trusted_as_native_text() -> None:
    page = PdfPage(
        invisible_lines=["unsichtbarer alter Textlayer mit falschem Inhalt"],
        images=[full_page(scan_image(SCANNED))],
    )
    engine = ScriptedEngine(words("Mahnung Nummer 4711"))
    result = extractor(engine).extract("layer.pdf", pdf([page]))
    extracted = result.pages[0]
    assert extracted.classification is PageClass.SCANNED_IMAGE
    assert "invisible_text_layer" in extracted.quality.flags
    assert "falschem" not in extracted.text and len(engine.calls) == 1


def test_empty_and_blank_pages_are_classified_without_ocr() -> None:
    engine = ScriptedEngine(words("nichts"))
    result = extractor(engine).extract(
        "empty.pdf", pdf([PdfPage(), PdfPage(images=[full_page(blank_image())])])
    )
    assert [page.classification for page in result.pages] == [PageClass.EMPTY_PAGE] * 2
    assert engine.calls == [] and not result.review_required


def test_inked_page_without_recognized_text_requires_review() -> None:
    engine = ScriptedEngine([])
    result = extractor(engine).extract("drawing.pdf", pdf([PdfPage(vector_drawing=True)]))
    page = result.pages[0]
    assert page.classification is PageClass.OCR_REVIEW_REQUIRED
    assert "no_text_recognized" in page.quality.flags
    assert result.review_required and result.pages_needing_review == (1,)


def test_jpeg_and_png_are_virtual_page_one() -> None:
    image = scan_image(SCANNED, size=(900, 700), text_px=30)
    for name, content in (
        ("scan.jpg", image_bytes(image, "JPEG", quality=90)),
        ("scan.png", image_bytes(image, "PNG")),
    ):
        engine = ScriptedEngine(words("Mahnung Nummer 4711"))
        result = extractor(engine).extract(name, content)
        assert len(result.pages) == 1 and len(engine.calls) == 1
        page = result.pages[0]
        assert page.page_number == 1 and page.classification is PageClass.SCANNED_IMAGE
        assert page.unit == "px" and (page.width, page.height) == (900, 700)


def test_exif_orientation_is_applied_and_recorded() -> None:
    portrait = scan_image(SCANNED, size=(700, 1000), text_px=30)
    engine = ScriptedEngine(words("Mahnung Nummer 4711"))
    result = extractor(engine).extract("rotated.jpg", jpeg_with_orientation(portrait, 6))
    page = result.pages[0]
    assert "exif_orientation:6" in page.transformations
    assert (page.width, page.height) == (700, 1000)
    assert engine.calls[0][0] < engine.calls[0][1]  # recognized upright (portrait)


def test_ocr_timeout_marks_only_that_page_unreadable() -> None:
    engine = ScriptedEngine(words("x"), timeout=True)
    document = pdf([PdfPage(text_lines=NATIVE), PdfPage(images=[full_page(scan_image(SCANNED))])])
    result = extractor(engine).extract("t.pdf", document)
    assert result.pages[0].classification is PageClass.NATIVE_TEXT
    assert result.pages[1].classification is PageClass.UNREADABLE
    assert "ocr_timeout" in result.pages[1].quality.flags
    assert result.pages_needing_review == (2,)


def test_ocr_page_limit_is_enforced_per_document() -> None:
    engine = ScriptedEngine(words("Mahnung Nummer 4711"))
    result = extractor(engine, max_ocr_pages=1).extract(
        "many.pdf", scanned_pdf([SCANNED, SCANNED, SCANNED])
    )
    assert len(engine.calls) == 1
    assert [page.classification for page in result.pages[1:]] == [PageClass.UNREADABLE] * 2
    assert all("ocr_page_limit" in page.quality.flags for page in result.pages[1:])


def test_missing_ocr_engine_fails_clearly_but_native_pdfs_still_work(tmp_path) -> None:
    missing = TesseractEngine(binary=str(tmp_path / "no-tesseract"))
    with pytest.raises(OcrEngineUnavailable):
        missing.probe()
    with pytest.raises(OcrEngineUnavailable):
        DocumentExtractor(engine=missing).extract("s.pdf", scanned_pdf([SCANNED]))
    native = DocumentExtractor(engine=missing).extract("n.pdf", native_pdf([NATIVE]))
    assert native.pages[0].classification is PageClass.NATIVE_TEXT


def _words(*items: tuple[str, float, tuple[int, int, int, int]]) -> list[OcrWord]:
    return [
        OcrWord(text, left, top, width, height, confidence, (1, 1, index // 3 + 1))
        for index, (text, confidence, (left, top, width, height)) in enumerate(items)
    ]


def test_quality_gate_flags_low_confidence_implausible_and_duplicate_output() -> None:
    size = (1000, 1000)
    good = assess_ocr(
        _words(
            ("Bescheid", 92, (10, 10, 100, 20)),
            ("über", 90, (120, 10, 60, 20)),
            ("1.234,56", 88, (190, 10, 90, 20)),
        ),
        size,
        ink=0.02,
    )
    assert good.state == "accepted" and good.flags == ()
    low = assess_ocr(
        _words(*[("Wort", 30, (10 + 50 * i, 10, 40, 20)) for i in range(6)]), size, 0.02
    )
    assert low.state == "review_required" and "low_confidence" in low.flags
    junk = assess_ocr(
        _words(
            *[
                (token, 90, (10 + 60 * i, 10, 40, 20))
                for i, token in enumerate(["|||", "~~", "_/\\_", "ok"])
            ]
        ),
        size,
        0.02,
    )
    assert "implausible_characters" in junk.flags and junk.state == "review_required"
    repeated = assess_ocr(
        _words(*[("Zeile", 90, (10, 10 + 30 * (i // 3), 40, 20)) for i in range(12)]), size, 0.02
    )
    assert "duplicated_output" in repeated.flags
    broken = assess_ocr(_words(("Text", 90, (990, 10, 50, 20))), size, 0.02)
    assert "invalid_geometry" in broken.flags
    assert assess_ocr([], size, ink=0.05).flags == ("no_text_recognized",)


def test_boxes_are_validated_and_normalized() -> None:
    assert Box(0.1, 0.1, 0.2, 0.3).valid()
    for box in (Box(0.3, 0.1, 0.2, 0.3), Box(-0.1, 0, 0.5, 0.5), Box(0, 0, 1.2, 0.5)):
        assert not box.valid()


def test_affine_inverse_maps_processed_points_back() -> None:
    rotate = Affine.rotation(3.0, (500, 400), (520, 430))
    scale = Affine.scale(2.0, 2.0)
    total = scale @ rotate
    for point in ((0, 0), (123.5, 77.25), (999, 799)):
        back = total.inverse().apply(total.apply(point))
        assert back == pytest.approx(point, abs=1e-6)


def test_preprocessing_records_transformations_and_keeps_a_valid_mapping() -> None:
    skewed = scan_image(SCANNED * 4, size=(1200, 900), text_px=30, skew_degrees=3.0)
    prepared = prepare_for_ocr(skewed, orientation=None)
    assert "grayscale" in prepared.transformations
    assert any(item.startswith("deskew:") for item in prepared.transformations)
    width, height = prepared.image.size
    corners = [(0, 0), (width, 0), (0, height), (width, height)]
    for x, y in (prepared.to_original.apply(corner) for corner in corners):
        assert -0.2 * 1200 <= x <= 1.2 * 1200 and -0.2 * 900 <= y <= 1.2 * 900
    small = prepare_for_ocr(Image.new("L", (300, 200), 255), orientation=None)
    assert any(item.startswith("upscale:") for item in small.transformations)
