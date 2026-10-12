"""Scanned-document intake: PDF, JPEG and PNG are checked against their contents and limits."""

from __future__ import annotations

import io

import pytest
from PIL import Image
from scripts.ocr_fixtures import (
    PdfPage,
    image_bytes,
    jpeg_with_orientation,
    native_pdf,
    pdf,
    png_bomb,
    scan_image,
    scanned_pdf,
)

from ragops.knowledge.service import validate_upload
from ragops.security.documents import IMAGE_LIMITS
from ragops.security.upload import (
    ALLOWED_EXTENSIONS,
    MAX_FILE_SIZE_BYTES,
    MAX_SCAN_FILE_SIZE_BYTES,
    UnsafeUploadError,
    max_upload_bytes,
)

LINES = ["Bescheid über 1.234,56 €", "Datum: 03.02.2026"]


def png(image: Image.Image) -> bytes:
    return image_bytes(image, "PNG")


def test_pdf_jpeg_and_png_are_accepted_with_matching_types() -> None:
    page = scan_image(LINES, size=(600, 400), text_px=24)
    assert {".jpg", ".jpeg", ".png"} <= ALLOWED_EXTENSIONS
    assert validate_upload("scan.png", "image/png", png(page)) == "scan.png"
    assert validate_upload("scan.jpg", "image/jpeg", image_bytes(page, "JPEG")) == "scan.jpg"
    assert validate_upload("scan.jpeg", "image/jpeg", jpeg_with_orientation(page, 6))
    assert validate_upload("scan.pdf", "application/pdf", scanned_pdf([LINES]))


@pytest.mark.parametrize(
    ("name", "mime", "content"),
    [
        ("scan.png", "image/jpeg", png(Image.new("L", (40, 40), 255))),
        ("scan.jpg", "image/png", image_bytes(Image.new("L", (40, 40), 255), "JPEG")),
        ("scan.png", "image/png", image_bytes(Image.new("L", (40, 40), 255), "JPEG")),
        ("scan.jpg", "image/jpeg", png(Image.new("L", (40, 40), 255))),
        ("scan.png", "image/png", b"MZ\x90\x00 renamed executable"),
        ("scan.png", "image/png", native_pdf([["kein Bild"]])),
    ],
    ids=["mime-mismatch", "mime-mismatch-2", "jpeg-as-png", "png-as-jpeg", "exe", "pdf-as-png"],
)
def test_type_and_signature_mismatches_are_rejected(name, mime, content) -> None:
    with pytest.raises(UnsafeUploadError):
        validate_upload(name, mime, content)


def test_truncated_and_malformed_images_are_rejected() -> None:
    whole = png(scan_image(LINES, size=(600, 400), text_px=24))
    jpeg = image_bytes(scan_image(LINES, size=(600, 400), text_px=24), "JPEG")
    for name, mime, content in (
        ("a.png", "image/png", whole[: len(whole) // 2]),
        ("a.jpg", "image/jpeg", jpeg[: len(jpeg) // 2]),
        ("a.png", "image/png", whole[:8] + b"\x00" * 64),
        ("a.jpg", "image/jpeg", b"\xff\xd8\xff" + b"\x00" * 64),
    ):
        with pytest.raises(UnsafeUploadError, match="does not match its type"):
            validate_upload(name, mime, content)


def _png_header_only(width: int, height: int) -> bytes:
    return png_bomb(width, height)


def test_image_bombs_and_excessive_dimensions_are_rejected() -> None:
    with pytest.raises(UnsafeUploadError, match="image exceeds safety limits"):
        validate_upload("bomb.png", "image/png", _png_header_only(30_000, 30_000))
    side = IMAGE_LIMITS.max_side + 1
    with pytest.raises(UnsafeUploadError, match="image exceeds safety limits"):
        validate_upload("wide.png", "image/png", _png_header_only(side, 10))
    pixels = IMAGE_LIMITS.max_pixels
    with pytest.raises(UnsafeUploadError, match="image exceeds safety limits"):
        validate_upload("big.png", "image/png", _png_header_only(10_000, pixels // 10_000 + 1))


def test_multi_frame_and_unsupported_modes_are_rejected() -> None:
    frames = [Image.new("L", (50, 50), value) for value in (0, 255)]
    buffer = io.BytesIO()
    frames[0].save(buffer, "PNG", save_all=True, append_images=frames[1:])
    with pytest.raises(UnsafeUploadError):
        validate_upload("anim.png", "image/png", buffer.getvalue())
    sixteen = Image.new("I;16", (50, 50))
    with pytest.raises(UnsafeUploadError):
        validate_upload("deep.png", "image/png", png(sixteen))


def test_pdf_external_references_and_active_content_are_rejected() -> None:
    for extra in (
        b"/AA << /O << /S /Launch /F (cmd.exe) >> >> ",
        b"/Annots [<< /Type /Annot /Subtype /Link /Rect [0 0 1 1] /A << /S /GoToE >> >>] ",
        b"/Annots [<< /Type /Annot /Subtype /FileAttachment /FS << /FS /URL /F (http://x) >> >>] ",
    ):
        document = pdf([PdfPage(text_lines=["Text"], extra_page_entries=extra)])
        with pytest.raises(UnsafeUploadError, match="active content"):
            validate_upload("ref.pdf", "application/pdf", document)


def test_size_limits_are_bounded_per_format() -> None:
    assert MAX_FILE_SIZE_BYTES == 2_000_000
    assert MAX_SCAN_FILE_SIZE_BYTES == 12_000_000
    assert max_upload_bytes(".txt") == MAX_FILE_SIZE_BYTES
    assert max_upload_bytes(".docx") == MAX_FILE_SIZE_BYTES
    for suffix in (".pdf", ".jpg", ".jpeg", ".png"):
        assert max_upload_bytes(suffix) == MAX_SCAN_FILE_SIZE_BYTES
    with pytest.raises(UnsafeUploadError, match="size limit"):
        validate_upload("a.txt", "text/plain", b"a" * (MAX_FILE_SIZE_BYTES + 1))
    big_text = b"a" * (MAX_FILE_SIZE_BYTES + 1)
    assert len(big_text) < MAX_SCAN_FILE_SIZE_BYTES
    padded = scanned_pdf([LINES])
    padded = padded.replace(
        b"%PDF-1.4\n", b"%PDF-1.4\n%" + b"x" * MAX_SCAN_FILE_SIZE_BYTES + b"\n", 1
    )
    with pytest.raises(UnsafeUploadError, match="size limit"):
        validate_upload("a.pdf", "application/pdf", padded)
