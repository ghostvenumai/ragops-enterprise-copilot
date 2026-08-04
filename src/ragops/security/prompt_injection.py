"""Prompt-injection detection for untrusted user and document text."""

from __future__ import annotations

import re
import unicodedata

INJECTION_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"ignore (all )?(previous|prior|system) instructions", re.I),
    re.compile(r"ignoriere (alle )?(vorherigen|bisherigen|system) anweisungen", re.I),
    re.compile(r"disregard (all )?(previous|prior|system) instructions", re.I),
    re.compile(
        r"reveal (the )?(system prompt|developer message|hidden prompt)|"
        r"zeige (den )?(systemprompt|system prompt)",
        re.I,
    ),
    re.compile(r"show (the )?(system prompt|developer message|hidden prompt)", re.I),
    re.compile(r"exfiltrat(e|ion)|steal|leak", re.I),
    re.compile(r"other tenant|all tenants|tenant-.+data", re.I),
    re.compile(r"administrator override|admin override|fake administrator", re.I),
    re.compile(r"prioriti[sz]e this source|ignore citations|fabricate citations", re.I),
    re.compile(r"base64|rot13|hidden instruction", re.I),
)


def normalize_for_detection(text: str) -> str:
    normalized = unicodedata.normalize("NFKC", text)
    return " ".join(normalized.replace("\u200b", "").split())


def detect_prompt_injection(text: str) -> list[str]:
    normalized = normalize_for_detection(text)
    return [pattern.pattern for pattern in INJECTION_PATTERNS if pattern.search(normalized)]


def has_prompt_injection(text: str) -> bool:
    return bool(detect_prompt_injection(text))
