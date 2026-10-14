"""Scanned PDF pages are rendered at the scan's own resolution only when that is reliable.

ENT-13.1 closure finding: a 150 dpi scan rendered at the configured 300 dpi was only resampled
2x, and Tesseract then misread digits at high confidence ("875167" as "87516/"). These tests
prove the eligibility policy, the fallbacks to the configured resolution and that geometry and
work limits are unchanged; tests/integration/test_ocr_tesseract.py proves the recognition.
"""

from __future__ import annotations

import io
import shutil

import pytest
from pypdf import PdfReader
from scripts.ocr_fixtures import (
    A4_POINTS,
    PdfPage,
    full_page,
    native_pdf,
    pdf,
    scan_image,
    scanned_pdf,
)
from tests.security.ocr_helpers import ScriptedEngine, words

from ragops.ingestion import pages as pages_module
from ragops.ingestion.ocr import OcrLimits
from ragops.ingestion.pages import DocumentExtractor, PageClass
from ragops.ingestion.raster import PdfRenderer

pytestmark = pytest.mark.skipif(shutil.which("pdftoppm") is None, reason="poppler missing")

LINES = ["Mahnung Nummer 4711", "Fällig am 15.03.2026", "Betrag 1.234,56 EUR"]
SCAN_150 = (1240, 1754)  # A4 at 150 dpi
SCAN_300 = (2480, 3508)  # A4 at 300 dpi
# pdftoppm rounds the page size up: 842 pt at 150 dpi is 1754.2 px, so one row more.
RENDER_150 = (1240, 1755)
RENDER_300 = (2480, 3509)


@pytest.fixture
def rendered(monkeypatch) -> list[int]:
    """The resolution of every page render (pdftoppm still runs for real)."""
    calls: list[int] = []
    original = PdfRenderer.render

    def spy(self: PdfRenderer, page_number: int, dpi: int):
        calls.append(dpi)
        return original(self, page_number, dpi)

    monkeypatch.setattr(PdfRenderer, "render", spy)
    return calls


def extract(document: bytes, **limits: object):
    engine = ScriptedEngine(words("Mahnung Nummer 4711\nFällig am 15.03.2026"))
    extractor = DocumentExtractor(engine=engine, limits=OcrLimits(**limits))  # type: ignore[arg-type]
    return extractor.extract("scan.pdf", document), engine


def layout_of(document: bytes, index: int = 0):
    return pages_module._analyze(PdfReader(io.BytesIO(document)).pages[index])


def native_steps(page) -> list[str]:
    return [step for step in page.transformations if step.startswith("native_dpi:")]


# ----------------------------------------------------------------------------- eligible pages


def test_a_150_dpi_full_page_scan_is_rendered_at_its_own_resolution(rendered) -> None:
    result, engine = extract(scanned_pdf([LINES]))
    page = result.pages[0]
    assert rendered == [150]
    assert engine.calls == [RENDER_150]  # the scan's own pixel grid (plus pdftoppm's row)
    assert page.classification is PageClass.SCANNED_IMAGE
    assert native_steps(page) == ["native_dpi:150"]  # recorded with the page's provenance
    assert page.unit == "pt" and (page.width, page.height) == A4_POINTS
    assert page.lines and all(line.box.valid() for line in page.lines)


def test_a_300_dpi_scan_keeps_the_configured_resolution(rendered) -> None:
    image = scan_image(LINES, size=SCAN_300, text_px=68, origin=(220, 280))
    result, engine = extract(pdf([PdfPage(images=[full_page(image)])]))
    assert rendered == [300] and engine.calls == [RENDER_300]
    assert native_steps(result.pages[0]) == []


def test_small_text_on_a_150_dpi_scan_falls_back_to_the_configured_resolution(rendered) -> None:
    """At its own resolution the text is too small for Tesseract and the page too large for
    prepare_for_ocr to enlarge it, so the page is rendered at 300 dpi exactly as before."""
    result, engine = extract(scanned_pdf([LINES], text_px=16))
    page = result.pages[0]
    assert rendered == [150, 300]
    assert engine.calls == [RENDER_300]
    assert native_steps(page) == [] and page.classification is PageClass.SCANNED_IMAGE


def test_a_rotated_page_keeps_displayed_page_coordinates(rendered) -> None:
    document = pdf([PdfPage(images=[full_page(scan_image(LINES))], rotate=90)])
    result, _ = extract(document)
    page = result.pages[0]
    assert rendered == [150]
    assert (page.width, page.height) == (A4_POINTS[1], A4_POINTS[0])
    assert native_steps(page) == ["native_dpi:150"]
    assert page.lines and all(line.box.valid() for line in page.lines)


def test_small_sideways_text_is_measured_along_its_lines(rendered) -> None:
    """A rotated page shows its lines vertically; their height is measured turned, so small
    text still falls back instead of being mistaken for large text."""
    document = pdf([PdfPage(images=[full_page(scan_image(LINES, text_px=16))], rotate=90)])
    result, _ = extract(document)
    assert rendered == [150, 300]
    assert native_steps(result.pages[0]) == []


def test_a_scan_with_an_invisible_ocr_layer_qualifies_and_the_layer_stays_untrusted(
    rendered,
) -> None:
    document = pdf(
        [PdfPage(images=[full_page(scan_image(LINES))], invisible_lines=["Fremde OCR 9999"])]
    )
    result, _ = extract(document)
    page = result.pages[0]
    assert rendered == [150]
    assert page.classification is PageClass.SCANNED_IMAGE
    assert "invisible_text_layer" in page.quality.flags and "9999" not in page.text


def test_identical_pages_choose_identical_resolutions(rendered) -> None:
    first, _ = extract(scanned_pdf([LINES, LINES]))
    second, _ = extract(scanned_pdf([LINES, LINES]))
    assert rendered == [150, 150, 150, 150]
    assert [p.transformations for p in first.pages] == [p.transformations for p in second.pages]


# ----------------------------------------------------------------------------- fallbacks


@pytest.mark.parametrize(
    ("page", "reason"),
    [
        (PdfPage(images=[(scan_image(LINES), (0.0, 421.0, 595.0, 421.0))]), "partial"),
        (
            PdfPage(
                images=[
                    (scan_image(LINES, size=(1240, 877)), (0.0, 421.0, 595.0, 421.0)),
                    (scan_image(LINES, size=(1240, 877)), (0.0, 0.0, 595.0, 421.0)),
                ]
            ),
            "two images",
        ),
        (PdfPage(images=[full_page(scan_image(LINES, size=(1240, 877)))]), "conflicting dpi"),
        (PdfPage(images=[full_page(scan_image(LINES, size=(595, 842)))]), "below 100 dpi"),
        (PdfPage(images=[full_page(scan_image(LINES))], vector_drawing=True), "vector drawing"),
        (PdfPage(images=[full_page(scan_image(LINES))], text_lines=LINES), "visible text"),
    ],
)
def test_uncertain_pages_keep_the_configured_resolution(rendered, page, reason) -> None:
    document = pdf([page])
    assert pages_module._native_scan_dpi(layout_of(document)) is None, reason
    result, _ = extract(document)
    assert set(rendered) == {300}, reason
    assert native_steps(result.pages[0]) == [], reason


def _single(ctm, pixels=SCAN_150):
    layout = pages_module._Layout(
        width=595.0, height=842.0, rotation=0, text="", crop=(0.0, 0.0, 595.0, 842.0)
    )
    layout.placements.append(pages_module._Placement(tuple(float(v) for v in ctm), pixels))
    return layout


@pytest.mark.parametrize(
    ("ctm", "pixels", "reason"),
    [
        ((595, 0, 0, 842, 0, 0), None, "intrinsic size unknown (inline image)"),
        ((595, 0, 40, 842, 0, 0), SCAN_150, "sheared"),
        ((0, 842, -595, 0, 595, 0), SCAN_150, "rotated placement"),
        ((-595, 0, 0, 842, 595, 0), SCAN_150, "mirrored"),
        ((595, 0, 0, -842, 0, 842), SCAN_150, "flipped"),
        ((620, 0, 0, 870, -12, -14), SCAN_150, "cropped by the page box"),
    ],
)
def test_unsupported_image_geometry_is_not_eligible(ctm, pixels, reason) -> None:
    assert pages_module._native_scan_dpi(_single(ctm, pixels)) is None, reason


def test_the_reference_placement_is_eligible_at_its_exact_resolution() -> None:
    assert pages_module._native_scan_dpi(_single((595, 0, 0, 842, 0, 0))) == pytest.approx(
        150.0, abs=0.1
    )


def test_masked_or_invalid_images_have_no_trusted_intrinsic_size() -> None:
    assert pages_module._intrinsic_pixels({"/Width": 1240, "/Height": 1754}) == SCAN_150
    for image in (
        {"/Width": 1240, "/Height": 1754, "/SMask": object()},
        {"/Width": 1240, "/Height": 1754, "/Mask": object()},
        {"/Width": 0, "/Height": 1754},
        {"/Width": "1240", "/Height": 1754},
        {"/Height": 1754},
    ):
        assert pages_module._intrinsic_pixels(image) is None


# ----------------------------------------------------------------------------- unchanged paths


def test_native_text_pdfs_are_still_never_rendered(rendered) -> None:
    result, engine = extract(native_pdf([LINES]))
    assert rendered == [] and engine.calls == []
    assert result.pages[0].classification is PageClass.NATIVE_TEXT


def test_mixed_pages_keep_the_configured_resolution(rendered) -> None:
    stamp = scan_image(["Eingangsstempel 12.05.2026"], size=(1240, 300), origin=(110, 110))
    document = pdf(
        [
            PdfPage(
                text_lines=["Begleitschreiben mit nativem Text"], images=[(stamp, (0, 0, 595, 144))]
            )
        ]
    )
    result, _ = extract(document)
    assert rendered == [300]
    assert result.pages[0].classification is PageClass.MIXED_CONTENT
    assert native_steps(result.pages[0]) == []


# ----------------------------------------------------------------------------- limits


def test_the_render_pixel_cap_still_bounds_a_native_scan(rendered) -> None:
    """A cap below the scan's resolution wins; the page is not rendered above the cap."""
    result, _ = extract(scanned_pdf([LINES]), max_render_pixels=1_000_000)
    assert rendered == [101]  # floor(72 * sqrt(1e6 / (595 * 842)))
    assert native_steps(result.pages[0]) == []


def test_the_document_pixel_budget_counts_native_renders(rendered) -> None:
    """Six 150 dpi pages (2.17 MP each) against a 12 MP document budget: five are rendered,
    the sixth hits the limit. At 300 dpi (8.7 MP) only the first page would fit."""
    result, _ = extract(
        scanned_pdf([LINES] * 6), max_render_pixels=12_000_000, max_document_pixels=12_000_000
    )
    assert rendered == [150] * 5
    assert [page.classification for page in result.pages] == [PageClass.SCANNED_IMAGE] * 5 + [
        PageClass.UNREADABLE
    ]
    assert "render_work_limit" in result.pages[5].quality.flags


def test_a_small_text_fallback_render_is_charged_to_the_document_budget(rendered) -> None:
    """The native probe render and the fallback both count; the fallback is refused when the
    budget cannot cover it."""
    result, _ = extract(
        scanned_pdf([LINES], text_px=16),
        max_render_pixels=10_000_000,
        max_document_pixels=10_000_000,
    )
    assert rendered == [150]  # 2.17 MP + 8.7 MP (300 dpi) exceeds 10 MP
    assert "render_work_limit" in result.pages[0].quality.flags
