"""Prompt-injection detection for untrusted user and document text.

Regex/keyword detection cannot guarantee zero false negatives against an
adaptive adversary; this module is one layer of defense, not a guarantee.
See docs/SECURITY_NOTES.md for the full threat-model discussion and the
remaining structural mitigation (no shipped provider passes document text to
a real generative model today).
"""

from __future__ import annotations

import base64
import binascii
import re
import unicodedata

_PHRASE_PATTERNS: tuple[str, ...] = (
    r"ignore (all |any )?(previous|prior|earlier|above|system) instructions",
    r"disregard (all |any )?(previous|prior|earlier|above|system) instructions",
    r"forget (all |any )?(previous|prior|earlier|above|your) instructions",
    r"override (all |any )?(previous|prior|system|safety) instructions",
    r"ignoriere (alle |jegliche )?(vorherigen|bisherigen|fr(ue|ü)heren|system) anweisungen",
    r"vergiss (alle |deine )?(vorherigen|bisherigen|system) anweisungen",
    r"ignore (toutes )?(les )?(instructions|consignes) (pr(e|é)c(e|é)dentes|du syst(e|è)me)",
    r"ignora (todas )?(las )?(instrucciones) (anteriores|del sistema)",
    r"you are now (in )?(developer|debug|admin|god|jailbreak|dan) mode",
    r"act as (if )?(there are no|you have no) (rules|restrictions|limits|guardrails)",
    r"you (are|have become) (an? )?(unrestricted|unfiltered|jailbroken) (ai|assistant|model)",
    r"pretend (you are|to be) (an? )?(ai|assistant) (with no|without) (restrictions|rules)",
    r"new instructions?:",
    r"system:\s*you (must|will|shall)",
    r"\[system\]|\[/?system\]|###\s*system",
    r"reveal (the )?(system prompt|developer message|hidden prompt|initial prompt)",
    r"zeige (den |mir den )?(systemprompt|system prompt|versteckten prompt)",
    r"show (me )?(the )?(system prompt|developer message|hidden prompt|initial prompt)",
    r"print (your |the )?(system prompt|instructions|rules) verbatim",
    r"repeat (everything|the text|the prompt) (above|before this)",
    r"what (were|are) you told (before|prior to) this",
    r"output your (initial |full )?instructions",
    r"what is your system prompt",
    r"exfiltrat(e|ion)|steal (credentials|secrets|data)|leak (the |all )?(data|secrets)",
    r"other tenant|all tenants|tenant-.+data|cross-tenant",
    r"(ignore|bypass|disable|circumvent|remove) "
    r"(the |all |any )?(tenant|access|security|permission|role) "
    r"(restrictions?|controls?|filters?|checks?|boundar(y|ies)|isolation)",
    r"(return|show|list|give|dump) (me )?(all |every |the )?"
    r"(hidden|secret|confidential|restricted|internal|classified) "
    r"(documents?|files?|data|records?|sources?)",
    r"(zeige|liste|gib) (mir )?(alle |s(ae|ä)mtliche )?"
    r"(versteckten|geheimen|vertraulichen|internen) "
    r"(dokumente|dateien|daten|quellen)",
    r"administrator override|admin override|fake administrator|impersonat(e|ing) (an? )?admin",
    r"prioriti[sz]e this source|ignore citations|fabricate citations|invent (a |the )?citation",
    r"execute (the )?following (command|code|script)",
    r"run this (code|script|command) (for me|now)",
    r"call the following function",
    r"send (this|the) (data|information|contents?) to https?://",
    r"do not (tell|inform|warn) the user",
    r"this is (a |an )?(hidden|secret) instruction (for|to) the (ai|model|assistant)",
)

INJECTION_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(pattern, re.I) for pattern in _PHRASE_PATTERNS
)

_COMPACT_PHRASES: tuple[str, ...] = (
    "ignorepreviousinstructions",
    "ignorepriorinstructions",
    "ignoresysteminstructions",
    "disregardpreviousinstructions",
    "ignoriereallevorherigenanweisungen",
    "ignorierediesystemanweisungen",
    "revealthesystemprompt",
    "showmethesystemprompt",
    "printyourinstructions",
    "jailbreakmode",
    "developermodeenabled",
    "youarenowdan",
)

_LEET_TABLE = str.maketrans({"0": "o", "1": "i", "3": "e", "4": "a", "5": "s", "7": "t", "@": "a"})

_FILLER_WORDS = frozenset(
    {
        "all",
        "any",
        "the",
        "der",
        "die",
        "das",
        "alle",
        "jegliche",
        "les",
        "toutes",
        "todas",
        "las",
    }
)

_TOKEN = re.compile(r"[a-z0-9]+")
_BASE64_TOKEN = re.compile(r"(?:[A-Za-z0-9+/]{24,}={0,2})")
_HTML_COMMENT = re.compile(r"<!--(.*?)-->", re.S)
_INVISIBLE_CATEGORIES = {"Cf", "Cc"}


def normalize_for_detection(text: str) -> str:
    normalized = unicodedata.normalize("NFKC", text)
    visible = "".join(
        char
        for char in normalized
        if unicodedata.category(char) not in _INVISIBLE_CATEGORIES or char in "\n\t "
    )
    return " ".join(visible.split())


def _compact(text: str) -> str:
    lowered = normalize_for_detection(text).lower().translate(_LEET_TABLE)
    tokens = [tok for tok in _TOKEN.findall(lowered) if tok not in _FILLER_WORDS]
    return "".join(tokens)


def _decode_base64_candidates(text: str) -> list[str]:
    decoded: list[str] = []
    for token in _BASE64_TOKEN.findall(text):
        try:
            raw = base64.b64decode(token, validate=True)
        except (binascii.Error, ValueError):
            continue
        try:
            decoded.append(raw.decode("utf-8"))
        except UnicodeDecodeError:
            continue
    return decoded


def detect_prompt_injection(text: str) -> list[str]:
    normalized = normalize_for_detection(text)
    findings = [pattern.pattern for pattern in INJECTION_PATTERNS if pattern.search(normalized)]

    compact = _compact(text)
    findings.extend(
        f"compact-evasion:{phrase}" for phrase in _COMPACT_PHRASES if phrase in compact
    )

    for match in _HTML_COMMENT.finditer(normalized):
        inner = match.group(1)
        if any(pattern.search(inner) for pattern in INJECTION_PATTERNS) or any(
            phrase in _compact(inner) for phrase in _COMPACT_PHRASES
        ):
            findings.append("hidden-instruction-in-comment")

    for decoded in _decode_base64_candidates(text):
        if detect_prompt_injection(decoded):
            findings.append("base64-encoded-instruction")

    return findings


def has_prompt_injection(text: str) -> bool:
    return bool(detect_prompt_injection(text))
