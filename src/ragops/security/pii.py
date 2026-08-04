"""PII detection and masking helpers for synthetic enterprise text."""

from __future__ import annotations

import re

EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
PHONE_RE = re.compile(r"\b(?:\+?\d[\d .-]{7,}\d)\b")
IBAN_RE = re.compile(r"\b[A-Z]{2}\d{2}[A-Z0-9]{11,30}\b")


def detect_pii(text: str) -> list[str]:
    findings: list[str] = []
    if EMAIL_RE.search(text):
        findings.append("email")
    if PHONE_RE.search(text):
        findings.append("phone")
    if IBAN_RE.search(text):
        findings.append("iban")
    return findings


def mask_pii(text: str) -> str:
    masked = EMAIL_RE.sub("[REDACTED_EMAIL]", text)
    masked = PHONE_RE.sub("[REDACTED_PHONE]", masked)
    masked = IBAN_RE.sub("[REDACTED_IBAN]", masked)
    return masked
