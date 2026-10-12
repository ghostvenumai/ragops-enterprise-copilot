"""Local OCR with the Tesseract engine, run as a bounded subprocess.

Availability is proven by executing the engine (`--version`, `--list-langs`), never inferred
from an import. Each recognition runs in a private 0700 temporary directory with a minimal
environment, a wall-clock timeout and resource limits applied through util-linux `prlimit`
(address space, CPU time, file size, open files, no core dumps). That is resource isolation,
not a sandbox: the engine runs as the worker's user. No document leaves the host.
"""

from __future__ import annotations

import csv
import io
import math
import os
import re
import shutil
import subprocess  # nosec B404 - fixed argument vectors, no shell, see _run
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from PIL import Image

COORDINATE_SPACE = "page-normalized-v1"
EXTRACTION_SCHEMA_VERSION = "ragops.extraction/v1"
OCR_CONFIG_VERSION = "tesseract-lstm-psm3-v1"
DEFAULT_LANGUAGES = "deu+eng"
ENGINE_MEMORY_BYTES = 1_610_612_736  # 1.5 GiB address space per engine process
ENGINE_FILE_BYTES = 64 * 1024 * 1024
ENGINE_OPEN_FILES = 64
VERSION_RE = re.compile(r"tesseract\s+v?(\d+\.\d+(?:\.\d+)?)", re.IGNORECASE)
LANGUAGE_RE = re.compile(r"^[a-z_]{3,16}$")


class OcrError(RuntimeError):
    """OCR could not produce a result; ``code`` is safe to persist and display."""

    code = "ocr_failed"

    def __init__(self, message: str = "") -> None:
        super().__init__(message or self.code)


class OcrEngineUnavailable(OcrError):
    code = "ocr_engine_unavailable"


class OcrLanguageUnavailable(OcrError):
    code = "ocr_language_unavailable"


class OcrTimeout(OcrError):
    code = "ocr_timeout"


class OcrFailed(OcrError):
    code = "ocr_failed"


@dataclass(frozen=True)
class EngineInfo:
    name: str
    version: str
    languages: tuple[str, ...]


@dataclass(frozen=True)
class OcrWord:
    """One recognized word in pixel coordinates of the image that was recognized."""

    text: str
    left: int
    top: int
    width: int
    height: int
    confidence: float | None
    line: tuple[int, int, int]  # (block, paragraph, line) in the engine's reading order


@dataclass(frozen=True)
class OcrLimits:
    """Work bounds of one document; every value is validated."""

    max_ocr_pages: int = 40
    page_timeout_seconds: float = 60.0
    document_timeout_seconds: float = 600.0
    render_dpi: int = 300
    min_render_dpi: int = 72
    max_render_pixels: int = 12_000_000
    max_document_pixels: int = 400_000_000
    render_timeout_seconds: float = 30.0

    def __post_init__(self) -> None:
        if not 1 <= self.max_ocr_pages <= 1000:
            raise ValueError("max_ocr_pages must be between 1 and 1000")
        if not 0 < self.page_timeout_seconds <= 600:
            raise ValueError("page_timeout_seconds must be in (0, 600]")
        if not 0 < self.document_timeout_seconds <= 7200:
            raise ValueError("document_timeout_seconds must be in (0, 7200]")
        if not 72 <= self.min_render_dpi <= self.render_dpi <= 600:
            raise ValueError("render dpi must satisfy 72 <= min <= dpi <= 600")
        if not 1_000_000 <= self.max_render_pixels <= 50_000_000:
            raise ValueError("max_render_pixels must be between 1 and 50 megapixels")
        if not self.max_render_pixels <= self.max_document_pixels <= 2_000_000_000:
            raise ValueError("max_document_pixels must cover at least one page")
        if not 0 < self.render_timeout_seconds <= 300:
            raise ValueError("render_timeout_seconds must be in (0, 300]")

    @classmethod
    def from_env(cls) -> OcrLimits:
        def number(name: str, default: float) -> float:
            return float(os.getenv(name, str(default)))

        return cls(
            max_ocr_pages=int(number("RAGOPS_OCR_MAX_PAGES", 40)),
            page_timeout_seconds=number("RAGOPS_OCR_PAGE_TIMEOUT_SECONDS", 60),
            document_timeout_seconds=number("RAGOPS_OCR_DOCUMENT_TIMEOUT_SECONDS", 600),
            render_dpi=int(number("RAGOPS_OCR_RENDER_DPI", 300)),
        )


class OcrEngine(Protocol):
    def probe(self) -> EngineInfo: ...
    def recognize(self, image: Image.Image, timeout_seconds: float) -> list[OcrWord]: ...
    def orientation(self, image: Image.Image, timeout_seconds: float) -> int | None: ...


def bounded_command(
    argv: Sequence[str], *, cpu_seconds: float, memory_bytes: int = ENGINE_MEMORY_BYTES
) -> list[str]:
    """Prefix a command with util-linux prlimit; refuse to run without it."""
    prlimit = shutil.which("prlimit")
    if prlimit is None:
        raise OcrEngineUnavailable("prlimit is required to bound OCR subprocesses")
    return [
        prlimit,
        f"--as={memory_bytes}",
        f"--cpu={max(1, math.ceil(cpu_seconds)) + 1}",
        f"--fsize={ENGINE_FILE_BYTES}",
        f"--nofile={ENGINE_OPEN_FILES}",
        "--core=0",
        "--",
        *argv,
    ]


def minimal_environment(**extra: str) -> dict[str, str]:
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LANG": "C.UTF-8", **extra}
    if os.environ.get("TESSDATA_PREFIX"):
        env["TESSDATA_PREFIX"] = os.environ["TESSDATA_PREFIX"]
    return env


def run_bounded(
    argv: Sequence[str], *, timeout: float, cwd: Path, env: dict[str, str]
) -> subprocess.CompletedProcess[bytes]:
    """Run a fixed argument vector (never a shell) under resource limits and a timeout."""
    command = bounded_command(argv, cpu_seconds=timeout)
    try:
        return subprocess.run(  # noqa: S603 # nosec B603 - fixed argv, no shell
            command,
            cwd=cwd,
            env=env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise OcrTimeout() from exc
    except OSError as exc:
        raise OcrEngineUnavailable() from exc


def parse_tsv(output: str) -> list[OcrWord]:
    """Words of Tesseract's TSV output (level 5); empty text is dropped."""
    words: list[OcrWord] = []
    reader = csv.DictReader(io.StringIO(output), delimiter="\t", quoting=csv.QUOTE_NONE)
    for row in reader:
        if row.get("level") != "5":
            continue
        text = (row.get("text") or "").strip()
        if not text:
            continue
        try:
            confidence = float(row["conf"])
            words.append(
                OcrWord(
                    text=text,
                    left=int(row["left"]),
                    top=int(row["top"]),
                    width=int(row["width"]),
                    height=int(row["height"]),
                    confidence=confidence if confidence >= 0 else None,
                    line=(int(row["block_num"]), int(row["par_num"]), int(row["line_num"])),
                )
            )
        except (KeyError, TypeError, ValueError):
            continue  # a malformed row carries no usable geometry
    return words


class TesseractEngine:
    """The Tesseract CLI with LSTM recognition (``--oem 1``) and automatic layout (``--psm 3``)."""

    name = "tesseract"

    def __init__(self, languages: str = DEFAULT_LANGUAGES, binary: str = "tesseract") -> None:
        parts = languages.split("+")
        if not parts or any(LANGUAGE_RE.fullmatch(part) is None for part in parts):
            raise ValueError("OCR languages must look like 'deu+eng'")
        self.languages = languages
        self.binary = binary
        self._info: EngineInfo | None = None

    def _executable(self) -> str:
        found = shutil.which(self.binary)
        if found is None:
            raise OcrEngineUnavailable("the tesseract executable is not installed")
        return found

    def probe(self) -> EngineInfo:
        if self._info is not None:
            return self._info
        executable = self._executable()
        with tempfile.TemporaryDirectory(prefix="ragops-ocr-") as work:
            version = run_bounded(
                [executable, "--version"], timeout=10, cwd=Path(work), env=minimal_environment()
            )
            listing = run_bounded(
                [executable, "--list-langs"], timeout=10, cwd=Path(work), env=minimal_environment()
            )
        text = (version.stdout + version.stderr).decode("utf-8", "replace")
        match = VERSION_RE.search(text)
        if version.returncode != 0 or match is None:
            raise OcrEngineUnavailable("the tesseract executable did not report a version")
        languages = tuple(
            sorted(
                line.strip()
                for line in listing.stdout.decode("utf-8", "replace").splitlines()[1:]
                if LANGUAGE_RE.fullmatch(line.strip())
            )
        )
        missing = [part for part in self.languages.split("+") if part not in languages]
        if listing.returncode != 0 or missing:
            raise OcrLanguageUnavailable("tesseract language data is missing: " + ",".join(missing))
        self._info = EngineInfo(self.name, match.group(1), languages)
        return self._info

    def _image_file(self, image: Image.Image, work: Path) -> Path:
        target = work / "page.png"
        descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            image.save(handle, "PNG")
        return target

    def recognize(self, image: Image.Image, timeout_seconds: float) -> list[OcrWord]:
        executable = self._executable()
        self.probe()
        with tempfile.TemporaryDirectory(prefix="ragops-ocr-") as work:
            source = self._image_file(image, Path(work))
            result = run_bounded(
                [
                    executable,
                    str(source),
                    "stdout",
                    "-l",
                    self.languages,
                    "--oem",
                    "1",
                    "--psm",
                    "3",
                    "tsv",
                ],
                timeout=timeout_seconds,
                cwd=Path(work),
                env=minimal_environment(OMP_THREAD_LIMIT="1"),
            )
        if result.returncode != 0:
            raise OcrFailed()
        return parse_tsv(result.stdout.decode("utf-8", "replace"))

    def orientation(self, image: Image.Image, timeout_seconds: float) -> int | None:
        """Counter-clockwise rotation (90/180/270) that makes the page upright, if OSD data exists.

        Tesseract reports ``Rotate: N`` as the counter-clockwise angle that corrects the page,
        which is what Pillow's ``rotate`` expects.
        """
        info = self.probe()
        if "osd" not in info.languages:
            return None
        executable = self._executable()
        with tempfile.TemporaryDirectory(prefix="ragops-ocr-") as work:
            source = self._image_file(image, Path(work))
            result = run_bounded(
                [executable, str(source), "stdout", "--psm", "0", "-l", "osd"],
                timeout=timeout_seconds,
                cwd=Path(work),
                env=minimal_environment(OMP_THREAD_LIMIT="1"),
            )
        text = result.stdout.decode("utf-8", "replace")
        rotate = re.search(r"Rotate:\s*(\d+)", text)
        confidence = re.search(r"Orientation confidence:\s*([\d.]+)", text)
        if result.returncode != 0 or rotate is None or confidence is None:
            return None  # too little text to decide; the page is recognized as it is
        angle = int(rotate.group(1)) % 360
        return angle if angle in {90, 180, 270} and float(confidence.group(1)) >= 2.0 else None
