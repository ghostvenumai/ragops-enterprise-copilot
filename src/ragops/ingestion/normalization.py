"""Text normalization and chunking."""

from __future__ import annotations

import re
import unicodedata

TOKEN_RE = re.compile(r"[^\W_]\w*", re.UNICODE)


def normalize_text(text: str) -> str:
    normalized = unicodedata.normalize("NFKC", text)
    normalized = normalized.replace("\x00", " ")
    normalized = re.sub(r"\s+", " ", normalized)
    return normalized.strip()


def tokenize(text: str) -> list[str]:
    return [match.group(0).lower() for match in TOKEN_RE.finditer(normalize_text(text))]


def chunk_text(text: str, chunk_size: int = 120, overlap: int = 20) -> list[str]:
    if chunk_size <= 0:
        raise ValueError("chunk_size must be positive")
    if overlap < 0 or overlap >= chunk_size:
        raise ValueError("overlap must be non-negative and smaller than chunk_size")
    tokens = tokenize(text)
    if not tokens:
        return []
    chunks: list[str] = []
    start = 0
    while start < len(tokens):
        end = min(start + chunk_size, len(tokens))
        chunks.append(" ".join(tokens[start:end]))
        if end == len(tokens):
            break
        start = end - overlap
    return chunks


def chunk_spans(count: int, chunk_size: int = 120, overlap: int = 20) -> list[tuple[int, int]]:
    """``[start, end)`` word windows of ``chunk_words``; lets callers keep word provenance."""
    if chunk_size <= 0:
        raise ValueError("chunk_size must be positive")
    if overlap < 0 or overlap >= chunk_size:
        raise ValueError("overlap must be non-negative and smaller than chunk_size")
    spans: list[tuple[int, int]] = []
    start = 0
    while start < count:
        end = min(start + chunk_size, count)
        spans.append((start, end))
        if end == count:
            break
        start = end - overlap
    return spans


def chunk_words(text: str, chunk_size: int = 120, overlap: int = 20) -> list[str]:
    """Overlapping word windows that keep the original wording, case and punctuation."""
    words = normalize_text(text).split()
    spans = chunk_spans(len(words), chunk_size, overlap)
    return [" ".join(words[start:end]) for start, end in spans]
