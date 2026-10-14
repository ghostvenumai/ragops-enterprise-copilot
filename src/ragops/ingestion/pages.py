"""Page-level document extraction: native text where it is trustworthy, OCR where it is needed.

Each PDF page is classified from evidence on that page — the text layer and its plausibility,
whether the text is drawn invisibly, and the page area covered by images (followed through the
content stream's transformation matrix) — never from the file name or metadata. Images are one
virtual page 1. Other formats keep their previous extraction unchanged. The result is a typed,
versioned contract (``ragops.extraction/v1``) with original page numbers, normalized boxes in
``page-normalized-v1`` and a page-level quality state; nothing is ever invented for text that
could not be read.
"""

from __future__ import annotations

import io
import math
import re
import time
import unicodedata
from collections.abc import Callable, Iterator
from contextlib import ExitStack
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import PurePath
from typing import Any

import numpy as np
from PIL import Image

from ragops.ingestion.extract import DocumentExtractionError, extract_segments
from ragops.ingestion.normalization import normalize_text, tokenize
from ragops.ingestion.ocr import (
    COORDINATE_SPACE,
    DEFAULT_LANGUAGES,
    EXTRACTION_SCHEMA_VERSION,
    OCR_CONFIG_VERSION,
    EngineInfo,
    OcrEngine,
    OcrEngineUnavailable,
    OcrError,
    OcrLanguageUnavailable,
    OcrLimits,
    OcrTimeout,
    OcrWord,
    TesseractEngine,
)
from ragops.ingestion.raster import (
    MIN_TEXT_LINE_PX,
    PdfRenderer,
    ink_ratio,
    load_image,
    prepare_for_ocr,
    text_line_height,
)

__all__ = [
    "COORDINATE_SPACE",
    "Box",
    "DocumentExtraction",
    "DocumentExtractor",
    "ExtractionMethod",
    "OcrLine",
    "PageClass",
    "PageExtraction",
    "PageQuality",
    "PageWord",
    "assess_ocr",
]

PAGINATED_SUFFIXES = frozenset({".pdf", ".jpg", ".jpeg", ".png"})
IMAGE_SUFFIXES = frozenset({".jpg", ".jpeg", ".png"})
MIXED_IMAGE_COVERAGE = 0.15
SCANNED_IMAGE_COVERAGE = 0.02
MIN_REGION_AREA = 0.005
MAX_OCR_REGIONS = 8
BLANK_INK = 0.0002
MAX_CONTENT_OPERATIONS = 500_000
MAX_FORM_DEPTH = 3
NATIVE_PLAUSIBLE_RATIO = 0.5
# Native-resolution rendering of a single full-page scan (see _native_scan_dpi).
NATIVE_SCAN_MIN_DPI = 100.0
NATIVE_SCAN_DPI_AGREEMENT = 0.01  # horizontal and vertical estimates may differ by 1 %
NATIVE_SCAN_EDGE_TOLERANCE = 0.005  # placement edge vs. crop box, share of the page side
DUPLICATE_LINE_SHARE = 0.8
# Quality gate thresholds (Tesseract word confidences are 0-100 tool scores).
REVIEW_MEAN_CONFIDENCE = 65.0
LOW_WORD_CONFIDENCE = 40.0
REVIEW_LOW_CONFIDENCE_SHARE = 0.35
REVIEW_IMPLAUSIBLE_SHARE = 0.35
REVIEW_INVALID_GEOMETRY_SHARE = 0.05
MIN_CHARS_ON_INKED_PAGE = 12
EDGE_MARGIN = 0.005
PLAUSIBLE_TOKEN = re.compile(r"[\w€$£%§&@.,:;!?'\"()\[\]/+\-–—°*#=<>]+")
REVIEW_FLAGS = frozenset(
    {
        "no_text_recognized",
        "low_confidence",
        "implausible_characters",
        "duplicated_output",
        "invalid_geometry",
        "low_text_density",
    }
)


class PageClass(StrEnum):
    NATIVE_TEXT = "native_text"
    SCANNED_IMAGE = "scanned_image"
    MIXED_CONTENT = "mixed_content"
    EMPTY_PAGE = "empty_page"
    UNREADABLE = "unreadable"
    OCR_REVIEW_REQUIRED = "ocr_review_required"


INDEXABLE = frozenset({PageClass.NATIVE_TEXT, PageClass.SCANNED_IMAGE, PageClass.MIXED_CONTENT})
NEEDS_REVIEW = frozenset({PageClass.UNREADABLE, PageClass.OCR_REVIEW_REQUIRED})


class ExtractionMethod(StrEnum):
    NATIVE_TEXT = "native_text"
    OCR = "ocr"
    HYBRID = "native_text+ocr"
    NONE = "none"


@dataclass(frozen=True)
class Box:
    """``[x0, y0, x1, y1]`` in ``page-normalized-v1`` (top-left origin, y downwards)."""

    x0: float
    y0: float
    x1: float
    y1: float

    def valid(self) -> bool:
        values = (self.x0, self.y0, self.x1, self.y1)
        return (
            all(math.isfinite(value) and 0.0 <= value <= 1.0 for value in values)
            and self.x0 < self.x1
            and self.y0 < self.y1
        )

    def as_list(self) -> list[float]:
        return [round(self.x0, 6), round(self.y0, 6), round(self.x1, 6), round(self.y1, 6)]

    @property
    def area(self) -> float:
        return max(0.0, self.x1 - self.x0) * max(0.0, self.y1 - self.y0)


@dataclass(frozen=True)
class PageWord:
    text: str
    line: int | None  # index into PageExtraction.lines; None for text-layer words


@dataclass(frozen=True)
class OcrLine:
    index: int
    box: Box
    confidence: float | None
    word_count: int


@dataclass(frozen=True)
class PageQuality:
    state: str  # "accepted" | "review_required" | "not_applicable"
    mean_confidence: float | None = None
    low_confidence_ratio: float | None = None
    implausible_ratio: float | None = None
    char_count: int = 0
    word_count: int = 0
    flags: tuple[str, ...] = ()


@dataclass(frozen=True)
class PageExtraction:
    page_number: int | None
    classification: PageClass
    method: ExtractionMethod
    text: str
    words: tuple[PageWord, ...]
    lines: tuple[OcrLine, ...]
    width: float | None
    height: float | None
    unit: str | None
    quality: PageQuality
    coordinate_space: str | None = COORDINATE_SPACE
    engine: str | None = None
    engine_version: str | None = None
    config_version: str | None = None
    language: str | None = None
    transformations: tuple[str, ...] = ()
    ocr_regions: tuple[Box, ...] = ()
    ocr_seconds: float = 0.0

    @property
    def indexable(self) -> bool:
        return self.classification in INDEXABLE


@dataclass(frozen=True)
class DocumentExtraction:
    pages: tuple[PageExtraction, ...]
    paginated: bool
    schema_version: str = EXTRACTION_SCHEMA_VERSION

    @property
    def review_required(self) -> bool:
        return any(page.classification in NEEDS_REVIEW for page in self.pages)

    @property
    def pages_needing_review(self) -> tuple[int, ...]:
        return tuple(
            page.page_number
            for page in self.pages
            if page.classification in NEEDS_REVIEW and page.page_number is not None
        )


def _plausible(token: str) -> bool:
    return PLAUSIBLE_TOKEN.fullmatch(token) is not None and any(ch.isalnum() for ch in token)


def assess_ocr(words: list[OcrWord], size: tuple[int, int], ink: float) -> PageQuality:
    """Page-level quality of OCR output in the pixel space of ``size``."""
    if not words:
        return PageQuality("review_required", flags=("no_text_recognized",))
    width, height = size
    flags: list[str] = []
    valid = [
        word
        for word in words
        if word.width > 0
        and word.height > 0
        and word.left >= 0
        and word.top >= 0
        and word.left + word.width <= width
        and word.top + word.height <= height
    ]
    invalid_share = 1 - len(valid) / len(words)
    confidences = [word.confidence for word in words if word.confidence is not None]
    mean = sum(confidences) / len(confidences) if confidences else None
    low_share = (
        sum(1 for value in confidences if value < LOW_WORD_CONFIDENCE) / len(confidences)
        if confidences
        else None
    )
    implausible = sum(1 for word in words if not _plausible(word.text)) / len(words)
    chars = sum(len(word.text) for word in words)
    lines: dict[tuple[int, int, int], list[str]] = {}
    for word in words:
        lines.setdefault(word.line, []).append(word.text)
    texts = [" ".join(items) for items in lines.values()]
    if (
        mean is None
        or mean < REVIEW_MEAN_CONFIDENCE
        or (low_share is not None and low_share > REVIEW_LOW_CONFIDENCE_SHARE)
    ):
        flags.append("low_confidence")
    if implausible > REVIEW_IMPLAUSIBLE_SHARE:
        flags.append("implausible_characters")
    if len(texts) >= 3 and len(set(texts)) / len(texts) < 0.5:
        flags.append("duplicated_output")
    if invalid_share > 0:
        flags.append("invalid_geometry")
    if chars < MIN_CHARS_ON_INKED_PAGE and ink > 0.01:
        flags.append("low_text_density")
    edge = [
        word
        for word in valid
        if word.left <= width * EDGE_MARGIN
        or word.top <= height * EDGE_MARGIN
        or word.left + word.width >= width * (1 - EDGE_MARGIN)
        or word.top + word.height >= height * (1 - EDGE_MARGIN)
    ]
    if valid and len(edge) / len(valid) > 0.2:
        flags.append("edge_truncation")
    review = any(
        flag in REVIEW_FLAGS
        and (flag != "invalid_geometry" or invalid_share > REVIEW_INVALID_GEOMETRY_SHARE)
        for flag in flags
    )
    return PageQuality(
        state="review_required" if review else "accepted",
        mean_confidence=round(mean, 2) if mean is not None else None,
        low_confidence_ratio=round(low_share, 4) if low_share is not None else None,
        implausible_ratio=round(implausible, 4),
        char_count=chars,
        word_count=len(words),
        flags=tuple(flags),
    )


# --------------------------------------------------------------------------- PDF layout


@dataclass(frozen=True)
class _Placement:
    """One drawn image: the matrix in effect and its intrinsic size (None if unknown)."""

    ctm: tuple[float, ...]
    pixels: tuple[int, int] | None


@dataclass
class _Layout:
    width: float
    height: float
    rotation: int
    text: str
    visible_text_ops: int = 0
    invisible_text_ops: int = 0
    vector_ops: int = 0
    image_boxes: list[Box] = field(default_factory=list)
    placements: list[_Placement] = field(default_factory=list)
    crop: tuple[float, float, float, float] = (0.0, 0.0, 0.0, 0.0)
    operations: int = 0

    @property
    def coverage(self) -> float:
        """Union area of the image boxes on a 100x100 grid (bounded and deterministic)."""
        cells: set[tuple[int, int]] = set()
        for box in self.image_boxes:
            for gx in range(int(box.x0 * 100), min(100, math.ceil(box.x1 * 100))):
                for gy in range(int(box.y0 * 100), min(100, math.ceil(box.y1 * 100))):
                    cells.add((gx, gy))
        return len(cells) / 10_000


def _matrix(values: list[Any]) -> tuple[float, ...]:
    return tuple(float(value) for value in values[:6])


def _multiply(m: tuple[float, ...], n: tuple[float, ...]) -> tuple[float, ...]:
    """PDF matrix product m × n (row-vector convention)."""
    a, b, c, d, e, f = m
    a2, b2, c2, d2, e2, f2 = n
    return (
        a * a2 + b * c2,
        a * b2 + b * d2,
        c * a2 + d * c2,
        c * b2 + d * d2,
        e * a2 + f * c2 + e2,
        e * b2 + f * d2 + f2,
    )


class _ContentWalker:
    """Follows q/Q/cm through a page and its forms; counts text, images and drawing."""

    def __init__(self, layout: _Layout, crop: tuple[float, float, float, float]) -> None:
        self.layout, self.crop = layout, crop

    def walk(self, content: Any, resources: Any, ctm: tuple[float, ...], depth: int) -> None:
        from pypdf.generic import ContentStream

        if content is None:
            return
        if not isinstance(content, ContentStream):
            content = ContentStream(content, None)
        stack: list[tuple[float, ...]] = []
        mode = 0
        xobjects = resources.get("/XObject") if resources is not None else None
        xobjects = xobjects.get_object() if xobjects is not None else {}
        for operands, operator in content.operations:
            self.layout.operations += 1
            if self.layout.operations > MAX_CONTENT_OPERATIONS:
                raise DocumentExtractionError("page content exceeds the operation limit")
            if operator == b"q":
                stack.append(ctm)
            elif operator == b"Q":
                ctm = stack.pop() if stack else ctm
            elif operator == b"cm" and len(operands) >= 6:
                ctm = _multiply(_matrix(operands), ctm)
            elif operator == b"Tr" and operands:
                mode = int(operands[0])
            elif operator in {b"Tj", b"TJ", b"'", b'"'}:
                if mode == 3:
                    self.layout.invisible_text_ops += 1
                else:
                    self.layout.visible_text_ops += 1
            elif operator in {b"f", b"F", b"f*", b"S", b"s", b"B", b"B*", b"b", b"b*"}:
                self.layout.vector_ops += 1
            elif operator == b"INLINE IMAGE":
                self._image(ctm, None)
            elif operator == b"Do" and operands:
                target = xobjects.get(operands[0]) if xobjects else None
                if target is None:
                    continue
                target = target.get_object()
                subtype = target.get("/Subtype")
                if subtype == "/Image":
                    self._image(ctm, _intrinsic_pixels(target))
                elif subtype == "/Form" and depth < MAX_FORM_DEPTH:
                    matrix = _matrix(list(target.get("/Matrix", [1, 0, 0, 1, 0, 0])))
                    self.walk(
                        ContentStream(target, None),
                        target.get("/Resources", resources),
                        _multiply(matrix, ctm),
                        depth + 1,
                    )

    def _image(self, ctm: tuple[float, ...], pixels: tuple[int, int] | None) -> None:
        self.layout.placements.append(_Placement(ctm, pixels))
        corners = [
            (ctm[0] * x + ctm[2] * y + ctm[4], ctm[1] * x + ctm[3] * y + ctm[5])
            for x, y in ((0, 0), (1, 0), (0, 1), (1, 1))
        ]
        left, bottom, right, top = self.crop
        width, height = right - left, top - bottom
        xs = [min(max((x - left) / width, 0.0), 1.0) for x, _ in corners]
        ys = [min(max((top - y) / height, 0.0), 1.0) for _, y in corners]
        box = _rotate_box(Box(min(xs), min(ys), max(xs), max(ys)), self.layout.rotation)
        if box.area >= MIN_REGION_AREA:
            self.layout.image_boxes.append(box)


def _intrinsic_pixels(image: Any) -> tuple[int, int] | None:
    """Declared width and height of an image XObject; None when invalid or soft-masked."""
    if "/SMask" in image or "/Mask" in image:
        return None  # a mask has its own resolution
    width, height = image.get("/Width"), image.get("/Height")
    if not isinstance(width, int) or not isinstance(height, int) or width <= 0 or height <= 0:
        return None
    return int(width), int(height)


def _native_scan_dpi(layout: _Layout) -> float | None:
    """Resolution of the one image that makes up a scanned page, when it is reliable.

    Rendering such a page above the scan's own resolution only resamples it; that made
    Tesseract misread digits ("7" as "/" or "]") at high confidence (ENT-13.1 closure). Only a
    page that is exactly one upright, unsheared, unmasked image filling the crop box, with no
    visible text or vector drawing and agreeing horizontal and vertical resolution, qualifies;
    everything else keeps the configured render resolution. An invisible text layer does not
    disqualify a page: it is not rendered.
    """
    if layout.visible_text_ops or layout.vector_ops or len(layout.placements) != 1:
        return None
    placement = layout.placements[0]
    if placement.pixels is None:
        return None
    a, b, c, d, e, f = placement.ctm
    if a <= 0 or d <= 0 or abs(b) > 1e-6 * a or abs(c) > 1e-6 * d:
        return None  # rotated, sheared or mirrored placement
    left, bottom, right, top = layout.crop
    page_width, page_height = right - left, top - bottom
    edges = (
        (e, left, page_width),
        (e + a, right, page_width),
        (f, bottom, page_height),
        (f + d, top, page_height),
    )
    if any(abs(edge - target) > NATIVE_SCAN_EDGE_TOLERANCE * side for edge, target, side in edges):
        return None  # the image does not fill the page exactly (cropped or partial)
    dpi_x, dpi_y = placement.pixels[0] * 72 / a, placement.pixels[1] * 72 / d
    if abs(dpi_x - dpi_y) > NATIVE_SCAN_DPI_AGREEMENT * max(dpi_x, dpi_y):
        return None
    native = (dpi_x + dpi_y) / 2
    return native if math.isfinite(native) and native >= NATIVE_SCAN_MIN_DPI else None


def _horizontal_text_line_height(image: Image.Image) -> float | None:
    """Text line height measured with the lines horizontal (a sideways page is turned first).

    Horizontal lines give the row ink profile a much larger variance than the column profile.
    """
    ink = np.asarray(image.convert("L")) < 128
    upright = ink.sum(axis=1).var() >= ink.sum(axis=0).var()
    return text_line_height(image if upright else image.transpose(Image.Transpose.ROTATE_90))


def _rotate_box(box: Box, rotation: int) -> Box:
    """Unrotated normalized page box -> the page as displayed with /Rotate."""

    def point(x: float, y: float) -> tuple[float, float]:
        if rotation == 90:
            return 1 - y, x
        if rotation == 180:
            return 1 - x, 1 - y
        if rotation == 270:
            return y, 1 - x
        return x, y

    corners = [point(box.x0, box.y0), point(box.x1, box.y1)]
    xs, ys = [x for x, _ in corners], [y for _, y in corners]
    return Box(min(xs), min(ys), max(xs), max(ys))


def _analyze(page: Any) -> _Layout:
    crop = page.cropbox
    left, bottom, right, top = (
        float(value) for value in (crop.left, crop.bottom, crop.right, crop.top)
    )
    rotation = int(page.rotation or 0) % 360
    width, height = right - left, top - bottom
    if width <= 0 or height <= 0:
        raise DocumentExtractionError("page has no area")
    if rotation in {90, 270}:
        width, height = height, width
    layout = _Layout(
        width=width,
        height=height,
        rotation=rotation,
        text=page.extract_text() or "",
        crop=(left, bottom, right, top),
    )
    _ContentWalker(layout, (left, bottom, right, top)).walk(
        page.get_contents(), page.get("/Resources"), (1, 0, 0, 1, 0, 0), 0
    )
    return layout


def _native_usable(layout: _Layout) -> tuple[bool, list[str]]:
    flags: list[str] = []
    if layout.invisible_text_ops and not layout.visible_text_ops:
        flags.append("invisible_text_layer")
        return False, flags
    if layout.invisible_text_ops and layout.coverage >= MIXED_IMAGE_COVERAGE:
        flags.append("invisible_text_layer")  # an OCR layer over a scan is not trusted
        return False, flags
    tokens = layout.text.split()
    if not tokens:
        return False, flags
    garbage = sum(
        1
        for ch in layout.text
        if ch == "�" or unicodedata.category(ch) in {"Co", "Cc"} and ch not in "\n\r\t"
    )
    plausible = sum(1 for token in tokens if _plausible(token)) / len(tokens)
    if plausible < NATIVE_PLAUSIBLE_RATIO or garbage > 0.1 * len(layout.text):
        flags.append("native_text_unusable")
        return False, flags
    return True, flags


# --------------------------------------------------------------------------- extraction


@dataclass
class _Budget:
    limits: OcrLimits
    deadline: float
    ocr_pages: int = 0
    pixels: int = 0

    def remaining(self) -> float:
        return self.deadline - time.monotonic()


def _merged_regions(boxes: list[Box]) -> list[Box]:
    regions = sorted(boxes, key=lambda box: (box.y0, box.x0))
    if len(regions) > MAX_OCR_REGIONS:
        return [
            Box(
                min(box.x0 for box in regions),
                min(box.y0 for box in regions),
                max(box.x1 for box in regions),
                max(box.y1 for box in regions),
            )
        ]
    return regions


def _clip(value: float) -> float:
    return min(max(value, 0.0), 1.0)


class DocumentExtractor:
    """Extracts a stored document version into the page contract; OCR only where needed."""

    def __init__(self, engine: OcrEngine | None = None, limits: OcrLimits | None = None) -> None:
        self.engine: OcrEngine = engine or TesseractEngine()
        self.limits = limits or OcrLimits()
        self._info: EngineInfo | None = None

    def _engine_info(self) -> EngineInfo:
        if self._info is None:
            self._info = self.engine.probe()
        return self._info

    def extract(self, filename: str, content: bytes) -> DocumentExtraction:
        suffix = PurePath(filename).suffix.lower()
        budget = _Budget(self.limits, time.monotonic() + self.limits.document_timeout_seconds)
        if suffix == ".pdf":
            return DocumentExtraction(tuple(self._pdf(content, budget)), paginated=True)
        if suffix in IMAGE_SUFFIXES:
            return DocumentExtraction((self._image(content, budget),), paginated=True)
        segments = extract_segments(filename, content)
        return DocumentExtraction(
            tuple(
                PageExtraction(
                    page_number=number,
                    classification=PageClass.NATIVE_TEXT,
                    method=ExtractionMethod.NATIVE_TEXT,
                    text=text,
                    words=tuple(PageWord(word, None) for word in normalize_text(text).split()),
                    lines=(),
                    width=None,
                    height=None,
                    unit=None,
                    coordinate_space=None,
                    quality=PageQuality("not_applicable"),
                )
                for number, text in segments
            ),
            paginated=False,
        )

    # ------------------------------------------------------------------ PDF

    def _pdf(self, content: bytes, budget: _Budget) -> Iterator[PageExtraction]:
        from pypdf import PdfReader

        try:
            reader = PdfReader(io.BytesIO(content), strict=True)
            pages = list(reader.pages)
        except Exception as exc:  # noqa: BLE001 - parser errors vary by library and version
            raise DocumentExtractionError("document text could not be extracted") from exc
        with ExitStack() as stack:
            renderer: PdfRenderer | None = None

            def render(number: int, dpi: int) -> Image.Image:
                nonlocal renderer
                if renderer is None:
                    renderer = stack.enter_context(
                        PdfRenderer(content, self.limits.render_timeout_seconds)
                    )
                return renderer.render(number, dpi)

            for number, page in enumerate(pages, start=1):
                yield self._pdf_page(number, page, render, budget)

    def _pdf_page(self, number: int, page: Any, render: Any, budget: _Budget) -> PageExtraction:
        try:
            layout = _analyze(page)
        except Exception:  # noqa: BLE001 - a malformed page is reported, never guessed
            return self._unreadable(number, None, None, "pt", ("page_unparseable",))
        usable, flags = _native_usable(layout)
        coverage = layout.coverage
        if usable and coverage < MIXED_IMAGE_COVERAGE:
            return PageExtraction(
                page_number=number,
                classification=PageClass.NATIVE_TEXT,
                method=ExtractionMethod.NATIVE_TEXT,
                text=layout.text,
                words=tuple(PageWord(word, None) for word in normalize_text(layout.text).split()),
                lines=(),
                width=layout.width,
                height=layout.height,
                unit="pt",
                quality=PageQuality("not_applicable", flags=tuple(flags)),
            )
        if usable:
            regions = _merged_regions(layout.image_boxes)
            return self._ocr_page(number, layout, regions, render, budget, flags, mixed=True)
        needs_ocr = (
            coverage >= SCANNED_IMAGE_COVERAGE
            or layout.vector_ops > 0
            or bool(layout.text.strip())
            or layout.invisible_text_ops > 0
        )
        if not needs_ocr:
            return self._empty(number, layout.width, layout.height, "pt", flags)
        full = [Box(0.0, 0.0, 1.0, 1.0)]
        return self._ocr_page(number, layout, full, render, budget, flags, mixed=False)

    def _dpi(self, layout: _Layout) -> int:
        pixels_per_point_squared = self.limits.max_render_pixels / (layout.width * layout.height)
        return min(self.limits.render_dpi, math.floor(72 * math.sqrt(pixels_per_point_squared)))

    def _ocr_page(
        self,
        number: int,
        layout: _Layout,
        regions: list[Box],
        render: Any,
        budget: _Budget,
        flags: list[str],
        *,
        mixed: bool,
    ) -> PageExtraction:
        dims = (layout.width, layout.height, "pt")
        dpi = self._dpi(layout)
        if dpi < self.limits.min_render_dpi:
            return self._unreadable(number, *dims, (*flags, "page_too_large"))
        attempts = [dpi]
        native_dpi = None if mixed else _native_scan_dpi(layout)
        if native_dpi is not None:
            native_render = max(round(native_dpi), self.limits.min_render_dpi)
            if native_render < dpi:
                attempts.insert(0, native_render)
        steps: tuple[str, ...] = ()
        for attempt in attempts:
            pixels = round(layout.width * attempt / 72) * round(layout.height * attempt / 72)
            if budget.pixels + pixels > self.limits.max_document_pixels:
                return self._unreadable(number, *dims, (*flags, "render_work_limit"))
            if budget.remaining() <= 0:
                return self._unreadable(number, *dims, (*flags, "ocr_document_timeout"))
            try:
                image = render(number, attempt)
            except OcrTimeout:
                return self._unreadable(number, *dims, (*flags, "render_timeout"))
            except OcrEngineUnavailable:
                raise
            except OcrError:
                return self._unreadable(number, *dims, (*flags, "render_failed"))
            budget.pixels += pixels
            if attempt == dpi:
                break
            line_height = _horizontal_text_line_height(image)
            if line_height is None or line_height >= MIN_TEXT_LINE_PX:
                steps = (f"native_dpi:{attempt}",)
                break
            # Small text at the scan's own resolution: render at the configured resolution as
            # before, so it stays as large as it was (prepare_for_ocr cannot enlarge a page
            # whose long side already exceeds its target).
        page_ink = ink_ratio(image)
        if not mixed and page_ink < BLANK_INK:
            return self._empty(number, *dims, flags)
        native = layout.text if mixed else ""
        return self._recognize(
            number, image, regions, budget, flags, dims, native=native, ink=page_ink, steps=steps
        )

    # ------------------------------------------------------------------ images

    def _image(self, content: bytes, budget: _Budget) -> PageExtraction:
        try:
            image, steps = load_image(content)
        except Exception:  # noqa: BLE001 - intake validated it; a decoder failure is unreadable
            return self._unreadable(1, None, None, "px", ("image_unreadable",))
        dims = (image.width, image.height, "px")
        ink = ink_ratio(image)
        if ink < BLANK_INK:
            return self._empty(1, *dims, [])
        return self._recognize(
            1, image, [Box(0.0, 0.0, 1.0, 1.0)], budget, [], dims, native="", ink=ink, steps=steps
        )

    # ------------------------------------------------------------------ recognition

    def _recognize(
        self,
        number: int,
        image: Image.Image,
        regions: list[Box],
        budget: _Budget,
        flags: list[str],
        dims: tuple[Any, Any, str],
        *,
        native: str,
        ink: float,
        steps: tuple[str, ...] = (),
    ) -> PageExtraction:
        info = self._engine_info()  # missing engine or language fails the document clearly
        if budget.ocr_pages >= self.limits.max_ocr_pages:
            return self._unreadable(number, *dims, (*flags, "ocr_page_limit"))
        budget.ocr_pages += 1
        started = time.monotonic()
        width, height = image.size
        mapped: list[tuple[int, OcrWord]] = []
        transformations: list[str] = list(steps)
        for region_index, region in enumerate(regions):
            timeout = min(self.limits.page_timeout_seconds, budget.remaining())
            if timeout <= 0:
                return self._unreadable(number, *dims, (*flags, "ocr_document_timeout"))
            left, top = round(region.x0 * width), round(region.y0 * height)
            right, bottom = round(region.x1 * width), round(region.y1 * height)
            if right - left < 8 or bottom - top < 8:
                continue
            crop = image.crop((left, top, right, bottom))
            try:
                prepared = prepare_for_ocr(
                    crop,
                    self._orientation(min(10.0, timeout)),
                    max_pixels=self.limits.max_render_pixels,
                )
                words = self.engine.recognize(prepared.image, timeout)
            except OcrTimeout:
                return self._unreadable(number, *dims, (*flags, "ocr_timeout"))
            except (OcrEngineUnavailable, OcrLanguageUnavailable):
                raise
            except OcrError:
                return self._unreadable(number, *dims, (*flags, "ocr_failed"))
            transformations.extend(
                step for step in prepared.transformations if step not in transformations
            )
            for word in words:
                x0, y0, x1, y1 = prepared.to_original.box(
                    word.left, word.top, word.left + word.width, word.top + word.height
                )
                text = unicodedata.normalize("NFKC", word.text).strip()
                if not text:
                    continue
                mapped.append(
                    (
                        region_index,
                        OcrWord(
                            text=text,
                            left=round(x0 + left),
                            top=round(y0 + top),
                            width=round(x1 - x0),
                            height=round(y1 - y0),
                            confidence=word.confidence,
                            line=word.line,
                        ),
                    )
                )
        seconds = round(time.monotonic() - started, 3)
        quality = assess_ocr([word for _, word in mapped], (width, height), ink)
        lines, page_words, dropped = self._lines(mapped, (width, height), native)
        all_flags = [*flags, *quality.flags]
        if dropped:
            all_flags.append("ocr_duplicate_of_text_layer")
        mixed = bool(native)
        if mixed and "no_text_recognized" in quality.flags:
            # Image regions without text (logos, photos) are normal on a text page.
            all_flags = [flag for flag in all_flags if flag != "no_text_recognized"]
            all_flags.append("image_regions_without_text")
            quality = PageQuality("accepted", flags=())
        review = quality.state == "review_required"
        if mixed:
            native_words = [PageWord(word, None) for word in normalize_text(native).split()]
            words_out = (*native_words, *page_words)
            text = (
                native
                if not lines
                else native.rstrip()
                + "\n"
                + "\n".join(
                    " ".join(word.text for word in page_words if word.line == line.index)
                    for line in lines
                )
            )
            classification = PageClass.MIXED_CONTENT
            method = ExtractionMethod.HYBRID if lines else ExtractionMethod.NATIVE_TEXT
        else:
            words_out = tuple(page_words)
            text = "\n".join(
                " ".join(word.text for word in page_words if word.line == line.index)
                for line in lines
            )
            classification = PageClass.SCANNED_IMAGE
            method = ExtractionMethod.OCR
        if review:
            classification = PageClass.OCR_REVIEW_REQUIRED
        return PageExtraction(
            page_number=number,
            classification=classification,
            method=method,
            text=text,
            words=tuple(words_out),
            lines=tuple(lines),
            width=dims[0],
            height=dims[1],
            unit=dims[2],
            quality=PageQuality(
                state=quality.state,
                mean_confidence=quality.mean_confidence,
                low_confidence_ratio=quality.low_confidence_ratio,
                implausible_ratio=quality.implausible_ratio,
                char_count=quality.char_count,
                word_count=quality.word_count,
                flags=tuple(dict.fromkeys(all_flags)),
            ),
            engine=info.name,
            engine_version=info.version,
            config_version=OCR_CONFIG_VERSION,
            language=getattr(self.engine, "languages", DEFAULT_LANGUAGES),
            transformations=tuple(transformations),
            ocr_regions=tuple(regions),
            ocr_seconds=seconds,
        )

    def _orientation(self, timeout: float) -> Callable[[Image.Image], int | None]:
        def detect(candidate: Image.Image) -> int | None:
            return self.engine.orientation(candidate, timeout)

        return detect

    @staticmethod
    def _lines(
        mapped: list[tuple[int, OcrWord]], size: tuple[int, int], native: str
    ) -> tuple[list[OcrLine], list[PageWord], int]:
        width, height = size
        grouped: dict[tuple[int, tuple[int, int, int]], list[OcrWord]] = {}
        for region, word in mapped:
            grouped.setdefault((region, word.line), []).append(word)
        native_tokens = set(tokenize(native)) if native else set()
        lines: list[OcrLine] = []
        words: list[PageWord] = []
        dropped = 0
        for items in grouped.values():
            tokens = tokenize(" ".join(word.text for word in items))
            if (
                native_tokens
                and tokens
                and (
                    sum(1 for token in tokens if token in native_tokens) / len(tokens)
                    >= DUPLICATE_LINE_SHARE
                )
            ):
                dropped += 1
                continue
            box = Box(
                _clip(min(word.left for word in items) / width),
                _clip(min(word.top for word in items) / height),
                _clip(max(word.left + word.width for word in items) / width),
                _clip(max(word.top + word.height for word in items) / height),
            )
            if not box.valid():
                continue  # never attach a box that is not on the page
            confidences = [word.confidence for word in items if word.confidence is not None]
            index = len(lines)
            lines.append(
                OcrLine(
                    index=index,
                    box=box,
                    confidence=round(sum(confidences) / len(confidences), 2)
                    if confidences
                    else None,
                    word_count=len(items),
                )
            )
            words.extend(PageWord(word.text, index) for word in items)
        return lines, words, dropped

    @staticmethod
    def _empty(
        number: int, width: Any, height: Any, unit: str, flags: list[str] | tuple[str, ...]
    ) -> PageExtraction:
        return PageExtraction(
            page_number=number,
            classification=PageClass.EMPTY_PAGE,
            method=ExtractionMethod.NONE,
            text="",
            words=(),
            lines=(),
            width=width,
            height=height,
            unit=unit,
            quality=PageQuality("not_applicable", flags=tuple(flags)),
        )

    @staticmethod
    def _unreadable(
        number: int, width: Any, height: Any, unit: str, flags: tuple[str, ...]
    ) -> PageExtraction:
        return PageExtraction(
            page_number=number,
            classification=PageClass.UNREADABLE,
            method=ExtractionMethod.NONE,
            text="",
            words=(),
            lines=(),
            width=width,
            height=height,
            unit=unit,
            quality=PageQuality("review_required", flags=tuple(dict.fromkeys(flags))),
        )
