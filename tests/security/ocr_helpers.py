"""A scripted OCR engine for routing tests; real recognition is tested against Tesseract.

The fake engine "reads" an image by returning words that the test registered for the image's
exact pixel content, with the boxes the test gave. It records every call so tests can prove
whether OCR ran, on which page and on which region. It is never used by a live gate.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

from PIL import Image

from ragops.ingestion.ocr import EngineInfo, OcrTimeout, OcrWord


def fingerprint(image: Image.Image) -> str:
    return hashlib.sha256(image.convert("L").tobytes()).hexdigest()


@dataclass
class ScriptedEngine:
    """Returns the same scripted words for every image unless a per-size script matches."""

    words: list[tuple[str, float]] = field(default_factory=list)
    timeout: bool = False
    calls: list[tuple[int, int]] = field(default_factory=list)
    info: EngineInfo = field(
        default_factory=lambda: EngineInfo("scripted", "0.0-test", ("deu", "eng"))
    )

    def probe(self) -> EngineInfo:
        return self.info

    def orientation(self, image: Image.Image, timeout_seconds: float) -> int | None:
        return None

    def recognize(self, image: Image.Image, timeout_seconds: float) -> list[OcrWord]:
        self.calls.append(image.size)
        if self.timeout:
            raise OcrTimeout()
        width, height = image.size
        out = []
        line = column = 0
        for text, confidence in self.words:
            if text == "\n":
                line, column = line + 1, 0
                continue
            if column == 4:
                line, column = line + 1, 0
            column += 1
            out.append(
                OcrWord(
                    text=text,
                    left=int(width * (0.2 * column - 0.1)),
                    top=int(height * (0.1 + 0.05 * line)),
                    width=int(width * 0.15),
                    height=max(1, int(height * 0.03)),
                    confidence=confidence,
                    line=(1, 1, line + 1),
                )
            )
        return out


def words(text: str, confidence: float = 95.0) -> list[tuple[str, float]]:
    """Scripted words; a newline in the text starts a new OCR line."""
    out: list[tuple[str, float]] = []
    for number, line in enumerate(text.split("\n")):
        if number:
            out.append(("\n", 0.0))
        out.extend((word, confidence) for word in line.split())
    return out
