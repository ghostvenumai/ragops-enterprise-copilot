"""Bounded rasterization and recorded preprocessing for OCR.

PDF pages are rendered one at a time with poppler's ``pdftoppm`` under the same resource
limits as the OCR engine. Images are decoded with Pillow inside the intake pixel limits. Every
preprocessing step is recorded, and the geometric ones are affine transforms whose inverse maps
OCR boxes back to the page as displayed, so stored coordinates refer to the original upload.
"""

from __future__ import annotations

import io
import math
import os
import shutil
import tempfile
import warnings
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType

import numpy as np
from PIL import Image, ImageFilter, ImageOps

from ragops.ingestion.ocr import (
    OcrEngineUnavailable,
    OcrError,
    OcrFailed,
    minimal_environment,
    run_bounded,
)
from ragops.security.documents import IMAGE_LIMITS

TARGET_LONG_SIDE = 1600
MAX_UPSCALE = 2.0
# Median ink-run height (pixels) from which text is large enough for Tesseract as it is.
# Resampling such text to a fractional size made the LSTM misread glyphs (8 -> 3, lost
# umlauts) at high confidence, so only smaller text is upscaled.
MIN_TEXT_LINE_PX = 16
TEXT_STRIP_PX = 64
DESKEW_MAX_DEGREES = 5.0
DESKEW_STEP_DEGREES = 0.25
NOISE_ISOLATED_RATIO = 0.002


@dataclass(frozen=True)
class Affine:
    """``(x, y) -> (a·x + b·y + c, d·x + e·y + f)``; ``A @ B`` applies B first."""

    a: float = 1.0
    b: float = 0.0
    c: float = 0.0
    d: float = 0.0
    e: float = 1.0
    f: float = 0.0

    def __matmul__(self, other: Affine) -> Affine:
        return Affine(
            self.a * other.a + self.b * other.d,
            self.a * other.b + self.b * other.e,
            self.a * other.c + self.b * other.f + self.c,
            self.d * other.a + self.e * other.d,
            self.d * other.b + self.e * other.e,
            self.d * other.c + self.e * other.f + self.f,
        )

    def apply(self, point: tuple[float, float]) -> tuple[float, float]:
        x, y = point
        return self.a * x + self.b * y + self.c, self.d * x + self.e * y + self.f

    def inverse(self) -> Affine:
        determinant = self.a * self.e - self.b * self.d
        if abs(determinant) < 1e-12:
            raise ValueError("transform is not invertible")
        a, b = self.e / determinant, -self.b / determinant
        d, e = -self.d / determinant, self.a / determinant
        return Affine(a, b, -(a * self.c + b * self.f), d, e, -(d * self.c + e * self.f))

    def box(self, left: float, top: float, right: float, bottom: float) -> tuple[float, ...]:
        """Axis-aligned bounds of a transformed rectangle."""
        corners = [
            self.apply(point)
            for point in ((left, top), (right, top), (left, bottom), (right, bottom))
        ]
        xs = [x for x, _ in corners]
        ys = [y for _, y in corners]
        return min(xs), min(ys), max(xs), max(ys)

    @staticmethod
    def scale(sx: float, sy: float) -> Affine:
        return Affine(a=sx, e=sy)

    @staticmethod
    def translation(dx: float, dy: float) -> Affine:
        return Affine(c=dx, f=dy)

    @staticmethod
    def rotation(degrees: float, old_size: tuple[int, int], new_size: tuple[int, int]) -> Affine:
        """Old -> new pixel coordinates of Pillow's counter-clockwise ``rotate(expand=True)``."""
        theta = math.radians(degrees)
        cos, sin = math.cos(theta), math.sin(theta)
        center_old = Affine.translation(-old_size[0] / 2, -old_size[1] / 2)
        turn = Affine(a=cos, b=sin, d=-sin, e=cos)
        center_new = Affine.translation(new_size[0] / 2, new_size[1] / 2)
        return center_new @ turn @ center_old


@dataclass(frozen=True)
class Prepared:
    image: Image.Image
    to_original: Affine  # processed pixel -> pixel of the image as handed in
    transformations: tuple[str, ...]


def ink_ratio(image: Image.Image) -> float:
    """Share of dark pixels at full resolution (a thumbnail would erase a single text line)."""
    data = np.asarray(image.convert("L"))
    return float((data < 128).mean()) if data.size else 0.0


def _isolated_dark_ratio(image: Image.Image) -> float:
    data = np.asarray(image) < 128
    if data.size == 0:
        return 0.0
    padded = np.pad(data, 1)
    neighbours = sum(
        padded[1 + dy : padded.shape[0] - 1 + dy, 1 + dx : padded.shape[1] - 1 + dx].astype(
            np.uint8
        )
        for dy in (-1, 0, 1)
        for dx in (-1, 0, 1)
        if dy or dx
    )
    return float((data & (neighbours == 0)).mean())


def text_line_height(image: Image.Image) -> float | None:
    """Median height of the inked row runs in narrow vertical strips, or None without ink.

    Strips keep lines of slightly skewed pages (up to the deskew limit) from merging.
    """
    ink = np.asarray(image.convert("L")) < 128
    runs: list[int] = []
    for left in range(0, ink.shape[1], TEXT_STRIP_PX):
        rows = ink[:, left : left + TEXT_STRIP_PX].any(axis=1)
        if not rows.any():
            continue
        edges = np.diff(np.concatenate(([0], rows.astype(np.int8), [0])))
        heights = np.flatnonzero(edges == -1) - np.flatnonzero(edges == 1)
        runs.extend(int(value) for value in heights if value >= 3)
    return float(np.median(runs)) if runs else None


def _skew_angle(image: Image.Image) -> float:
    small = image.copy()
    small.thumbnail((1000, 1000))
    inverted = ImageOps.invert(small)

    def score(angle: float) -> float:
        rotated = inverted.rotate(angle, resample=Image.Resampling.NEAREST, fillcolor=0)
        rows = (np.asarray(rotated) > 127).sum(axis=1).astype(np.float64)
        return float(rows.var())

    steps = int(DESKEW_MAX_DEGREES / DESKEW_STEP_DEGREES)
    candidates = [step * DESKEW_STEP_DEGREES for step in range(-steps, steps + 1)]
    baseline = score(0.0)
    best = max(candidates, key=score)
    return best if abs(best) >= 0.5 and score(best) > baseline * 1.05 else 0.0


def prepare_for_ocr(
    image: Image.Image,
    orientation: Callable[[Image.Image], int | None] | None,
    *,
    max_pixels: int = 12_000_000,
) -> Prepared:
    """Grayscale, resolution, contrast, noise, orientation and skew — each only when needed."""
    steps: list[str] = []
    forward = Affine()
    if image.mode != "L":
        if image.mode in {"RGBA", "LA", "P"}:
            background = Image.new("RGBA", image.size, (255, 255, 255, 255))
            image = Image.alpha_composite(background, image.convert("RGBA"))
        image = image.convert("L")
        steps.append("grayscale")
    else:
        image = image.copy()
        steps.append("grayscale")
    width, height = image.size
    factor = 1.0
    if max(width, height) < TARGET_LONG_SIDE:
        line_height = text_line_height(image)
        if line_height is None or line_height < MIN_TEXT_LINE_PX:
            factor = min(MAX_UPSCALE, TARGET_LONG_SIDE / max(width, height))
            factor = min(factor, math.sqrt(max_pixels / (width * height)))
            if factor < 1.25:
                factor = 1.0
    elif width * height > max_pixels:
        factor = math.sqrt(max_pixels / (width * height))
    if factor != 1.0:
        size = (max(1, round(width * factor)), max(1, round(height * factor)))
        sx, sy = size[0] / width, size[1] / height
        image = image.resize(size, Image.Resampling.LANCZOS)
        forward = Affine.scale(sx, sy) @ forward
        steps.append(f"{'upscale' if factor > 1 else 'downscale'}:{factor:.2f}")
    histogram = np.asarray(image.histogram(), dtype=np.float64)
    cumulative = np.cumsum(histogram) / max(histogram.sum(), 1)
    low, high = int(np.searchsorted(cumulative, 0.02)), int(np.searchsorted(cumulative, 0.98))
    if high > low and high - low < 128:
        image = ImageOps.autocontrast(image, cutoff=1)
        steps.append("autocontrast")
    if _isolated_dark_ratio(image) > NOISE_ISOLATED_RATIO:
        image = image.filter(ImageFilter.MedianFilter(3))
        steps.append("median3")
    if orientation is not None:
        angle = orientation(image)
        if angle in {90, 180, 270}:
            old = image.size
            image = image.rotate(angle, expand=True)
            forward = Affine.rotation(angle, old, image.size) @ forward
            steps.append(f"rotate:{angle}")
    skew = _skew_angle(image)
    if skew:
        old = image.size
        image = image.rotate(skew, resample=Image.Resampling.BICUBIC, expand=True, fillcolor=255)
        forward = Affine.rotation(skew, old, image.size) @ forward
        steps.append(f"deskew:{skew:+.2f}")
    return Prepared(image, forward.inverse(), tuple(steps))


def load_image(content: bytes) -> tuple[Image.Image, tuple[str, ...]]:
    """Decode an intake-validated image and apply its EXIF orientation (recorded)."""
    with warnings.catch_warnings():
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        image: Image.Image = Image.open(io.BytesIO(content))
        width, height = image.size
        if width * height > IMAGE_LIMITS.max_pixels or max(width, height) > IMAGE_LIMITS.max_side:
            raise OcrFailed("image exceeds safety limits")
        image.load()
    orientation = image.getexif().get(0x0112, 1)
    steps: tuple[str, ...] = ()
    if orientation in {2, 3, 4, 5, 6, 7, 8}:
        transposed = ImageOps.exif_transpose(image)
        if transposed is not None:
            image = transposed
        steps = (f"exif_orientation:{orientation}",)
    return image, steps


class PdfRenderer:
    """Renders single pages of one PDF from a private temporary copy; removes it on exit."""

    def __init__(self, content: bytes, timeout_seconds: float) -> None:
        self.content, self.timeout = content, timeout_seconds
        self._work: Path | None = None

    def __enter__(self) -> PdfRenderer:
        if shutil.which("pdftoppm") is None:
            raise OcrEngineUnavailable("poppler pdftoppm is not installed")
        self._work = Path(tempfile.mkdtemp(prefix="ragops-render-"))
        source = self._work / "document.pdf"
        descriptor = os.open(source, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(self.content)
        return self

    def __exit__(
        self,
        kind: type[BaseException] | None,
        error: BaseException | None,
        trace: TracebackType | None,
    ) -> None:
        if self._work is not None:
            shutil.rmtree(self._work, ignore_errors=True)
            self._work = None

    def render(self, page_number: int, dpi: int) -> Image.Image:
        if self._work is None:
            raise RuntimeError("renderer is not open")
        executable = shutil.which("pdftoppm")
        if executable is None:
            raise OcrEngineUnavailable("poppler pdftoppm is not installed")
        output = self._work / "page"
        result = run_bounded(
            [
                executable,
                "-f",
                str(page_number),
                "-l",
                str(page_number),
                "-r",
                str(dpi),
                "-gray",
                "-cropbox",
                "-singlefile",
                "-png",
                str(self._work / "document.pdf"),
                str(output),
            ],
            timeout=self.timeout,
            cwd=self._work,
            env=minimal_environment(),
        )
        target = output.with_suffix(".png")
        try:
            if result.returncode != 0 or not target.is_file():
                raise OcrFailed("page could not be rendered")
            with Image.open(target) as rendered:
                rendered.load()
                return rendered.convert("L")
        except OcrError:
            raise
        except (OSError, ValueError) as exc:
            raise OcrFailed("page could not be rendered") from exc
        finally:
            target.unlink(missing_ok=True)
