"""Bounded diagnostics for operator evidence, never raw SDK object serialization."""

from __future__ import annotations

import re

_SK_KEY = re.compile(r"\bsk-[A-Za-z0-9_.+/-]+", re.IGNORECASE)
_BEARER = re.compile(r"\bBearer\s+[^\s,;\"'<>]+", re.IGNORECASE)
_LABELLED_SECRET = re.compile(
    r"(?P<label>\b(?:[a-z0-9]+[_-])*(?:api[_-]?key|key|token|secret|authorization)"
    r"\b[\"']?\s*(?:[:=]\s*|\s+(?:is\s+)?))"
    r"(?:(?:Basic|Bearer)\s+[^\s,;\"'<>]+|"
    r"\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*'|[^\s,;<>]+)",
    re.IGNORECASE,
)


def sanitize_provider_diagnostic(
    value: object, *, sensitive_values: tuple[str, ...] = ()
) -> str | None:
    """Accept text only; redact before truncation to avoid exposing partial secrets."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value
    for secret in sensitive_values:
        if secret:
            text = text.replace(secret, "[REDACTED]")
    text = _SK_KEY.sub("[REDACTED]", text)
    text = _BEARER.sub("Bearer [REDACTED]", text)
    text = _LABELLED_SECRET.sub(r"\g<label>[REDACTED]", text)
    # Drop control characters; whitespace (including line breaks) collapses to one space.
    text = "".join(char if char.isprintable() else " " for char in text)
    return " ".join(text.split())[:400] or None
