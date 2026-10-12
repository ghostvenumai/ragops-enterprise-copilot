"""Wholly synthetic documents for the OCR tests and the ocr_ingestion gate.

Every builder produces documents from scratch: native-text PDF pages, rasterized "scanned"
pages without a text layer, pages mixing both, and JPEG/PNG images. Nothing is derived from a
real document. The PDF writer is minimal on purpose so the tests control exactly what a page
contains (visible or invisible text, images and where they are placed).
"""

from __future__ import annotations

import io
import struct
import zlib
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

A4_POINTS = (595, 842)
FONT_CANDIDATES = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
)


def font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for candidate in FONT_CANDIDATES:
        if Path(candidate).is_file():
            return ImageFont.truetype(candidate, size)
    return ImageFont.load_default(size=size)


def scan_image(
    lines: Sequence[str],
    *,
    size: tuple[int, int] = (1240, 1754),
    text_px: int = 34,
    origin: tuple[int, int] = (110, 140),
    spacing: float = 1.9,
    skew_degrees: float = 0.0,
    noise: float = 0.0,
    seed: int = 7,
) -> Image.Image:
    """A grayscale "scan" (150 dpi A4 by default) of the given text lines."""
    image = Image.new("L", size, 255)
    draw = ImageDraw.Draw(image)
    face = font(text_px)
    x, y = origin
    for line in lines:
        draw.text((x, y), line, fill=0, font=face)
        y += round(text_px * spacing)
    if skew_degrees:
        image = image.rotate(skew_degrees, resample=Image.Resampling.BICUBIC, fillcolor=255)
    if noise:
        import random

        rng = random.Random(seed)  # noqa: S311 - deterministic synthetic speckle, not crypto
        pixels = image.load()
        assert pixels is not None
        width, height = image.size
        for _ in range(int(width * height * noise)):
            pixels[rng.randrange(width), rng.randrange(height)] = rng.choice((0, 255))
    return image


def blank_image(size: tuple[int, int] = (1240, 1754)) -> Image.Image:
    return Image.new("L", size, 255)


def image_bytes(image: Image.Image, fmt: str, **options: object) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, fmt, **options)
    return buffer.getvalue()


def jpeg_with_orientation(image: Image.Image, orientation: int) -> bytes:
    """A JPEG whose pixels are stored rotated and whose EXIF tag says how to display them."""
    stored = {
        1: image,
        3: image.rotate(180, expand=True),
        6: image.rotate(90, expand=True),
        8: image.rotate(-90, expand=True),
    }[orientation]
    exif = Image.Exif()
    exif[0x0112] = orientation
    return image_bytes(stored.convert("L"), "JPEG", quality=90, exif=exif.tobytes())


def _pdf_string(text: str) -> bytes:
    raw = text.encode("cp1252")
    return b"(" + raw.replace(b"\\", b"\\\\").replace(b"(", b"\\(").replace(b")", b"\\)") + b")"


@dataclass
class PdfPage:
    """One page: visible text lines, optional invisible text and optional placed images."""

    text_lines: Sequence[str] = ()
    invisible_lines: Sequence[str] = ()
    images: Sequence[tuple[Image.Image, tuple[float, float, float, float]]] = ()
    size: tuple[int, int] = A4_POINTS
    rotate: int = 0
    extra_page_entries: bytes = b""
    extra_content: bytes = b""
    vector_drawing: bool = False
    jpeg: bool = False
    _image_names: list[str] = field(default_factory=list)


def _text_block(lines: Sequence[str], mode: int, top: float) -> bytes:
    if not lines:
        return b""
    out = b"BT /F1 12 Tf " + str(mode).encode() + b" Tr 14 TL "
    out += f"1 0 0 1 72 {top:.2f} Tm ".encode()
    for line in lines:
        out += _pdf_string(line) + b" Tj T* "
    return out + b"ET\n"


def pdf(pages: Sequence[PdfPage]) -> bytes:
    """Serialize pages into a small, valid, unencrypted PDF without active content."""
    objects: list[bytes] = [b"", b""]  # catalog and page tree are filled in at the end
    font_ref = len(objects) + 1
    objects.append(
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>"
    )
    page_refs: list[int] = []
    for page in pages:
        resources = b"/Font << /F1 " + str(font_ref).encode() + b" 0 R >>"
        xobjects = b""
        content = b""
        top = page.size[1] - 72
        content += _text_block(page.text_lines, 0, top)
        content += _text_block(page.invisible_lines, 3, top)
        for number, (image, (x, y, width, height)) in enumerate(page.images, start=1):
            gray = image.convert("L")
            if page.jpeg:
                data = image_bytes(gray, "JPEG", quality=85)
                stream_filter = b"/DCTDecode"
            else:
                data = zlib.compress(gray.tobytes(), 6)
                stream_filter = b"/FlateDecode"
            objects.append(
                b"<< /Type /XObject /Subtype /Image /Width "
                + str(gray.width).encode()
                + b" /Height "
                + str(gray.height).encode()
                + b" /ColorSpace /DeviceGray /BitsPerComponent 8 /Filter "
                + stream_filter
                + b" /Length "
                + str(len(data)).encode()
                + b" >>\nstream\n"
                + data
                + b"\nendstream"
            )
            xobjects += f"/Im{number} {len(objects)} 0 R ".encode()
            content += (
                f"q {width:.2f} 0 0 {height:.2f} {x:.2f} {y:.2f} cm /Im{number} Do Q\n".encode()
            )
        if page.vector_drawing:
            content += b"q 0 g 100 400 m 300 400 l 300 420 l 100 420 l f Q\n"
        content += page.extra_content
        if xobjects:
            resources += b" /XObject << " + xobjects + b">>"
        objects.append(
            b"<< /Length "
            + str(len(content)).encode()
            + b" >>\nstream\n"
            + content
            + b"\nendstream"
        )
        content_ref = len(objects)
        width, height = page.size
        objects.append(
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 "
            + f"{width} {height}".encode()
            + b"] /Rotate "
            + str(page.rotate).encode()
            + b" /Contents "
            + str(content_ref).encode()
            + b" 0 R /Resources << "
            + resources
            + b" >> "
            + page.extra_page_entries
            + b">>"
        )
        page_refs.append(len(objects))
    objects[0] = b"<< /Type /Catalog /Pages 2 0 R >>"
    kids = b" ".join(f"{ref} 0 R".encode() for ref in page_refs)
    objects[1] = (
        b"<< /Type /Pages /Kids [" + kids + b"] /Count " + str(len(pages)).encode() + b" >>"
    )
    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    )
    return bytes(out)


def full_page(
    image: Image.Image, size: tuple[int, int] = A4_POINTS
) -> tuple[Image.Image, tuple[float, float, float, float]]:
    return image, (0.0, 0.0, float(size[0]), float(size[1]))


def scanned_pdf(pages: Sequence[Sequence[str]], **scan: object) -> bytes:
    """Image-only PDF: every page is one full-page raster, there is no text layer."""
    return pdf([PdfPage(images=[full_page(scan_image(lines, **scan))]) for lines in pages])  # type: ignore[arg-type]


def native_pdf(pages: Sequence[Sequence[str]]) -> bytes:
    return pdf([PdfPage(text_lines=lines) for lines in pages])


def png_bomb(width: int, height: int) -> bytes:
    """A PNG declaring huge dimensions with a tiny compressed body (a decompression bomb)."""

    def chunk(kind: bytes, data: bytes) -> bytes:
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(b"\x00" * 1024, 9))
        + chunk(b"IEND", b"")
    )
