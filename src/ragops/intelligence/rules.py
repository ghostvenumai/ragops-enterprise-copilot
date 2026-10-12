"""Deterministic German document classification and entity extraction (``rules-de-v1``).

A transparent rule system, not a model: it reads labelled lines ("Rechnungsnummer: …",
"Gesamtforderung: … EUR"), short headings that name a document type, sender and authority
lines, and a few fixed sentence forms. Everything it does not recognize is left out or marked
for review; it never guesses a role for an unlabelled amount, a person behind an address or a
legal status from a mentioned term. Lines that look like instructions to a machine are skipped
entirely and counted.

Input is a sequence of :class:`SourceLine` (page, line, text, extraction method, quality and,
for OCR, the line box). Output is an :class:`Analysis` with competing classification candidates
and typed facts whose character spans point into their source line.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
from datetime import date
from decimal import Decimal, InvalidOperation

from ragops.security.prompt_injection import has_prompt_injection

TAXONOMY_VERSION = "doc-taxonomy-v1"
EXTRACTOR_VERSION = "rules-de-v1"

DOCUMENT_CLASSES = (
    "invoice",
    "payment_reminder",
    "formal_demand",
    "debt_collection_letter",
    "payment_order_or_court_notice",
    "enforcement_document",
    "creditor_statement",
    "loan_agreement",
    "payment_receipt",
    "settlement_agreement",
    "correspondence",
    "unrelated_document",
    "unknown",
)

FACT_TYPES: dict[str, str] = {
    # parties
    "creditor": "party",
    "creditor_address": "party",
    "debtor": "party",
    "addressee": "party",
    "collection_agency": "party",
    "original_creditor": "party",
    "current_claimant": "party",
    "court_or_authority": "party",
    "guarantor": "party",
    "representative": "party",
    "other_party": "party",
    # amounts
    "principal_amount": "amount",
    "interest_amount": "amount",
    "fee_amount": "amount",
    "collection_cost_amount": "amount",
    "court_cost_amount": "amount",
    "payment_made_amount": "amount",
    "stated_remaining_balance": "amount",
    "stated_total_claim": "amount",
    "unclassified_amount": "amount",
    # identifiers
    "invoice_number": "identifier",
    "reference_number": "identifier",
    "collection_case_number": "identifier",
    "court_case_number": "identifier",
    "contract_reference": "identifier",
    "payment_reference": "identifier",
    # dates
    "issue_date": "date",
    "due_date": "date",
    "payment_deadline": "date",
    "interest_calculation_date": "date",
    "other_date": "date",
    # status indicators (mentions, never legal determinations)
    "payment_requested": "status",
    "payment_reported": "status",
    "disputed": "status",
    "settled": "status",
    "payment_arrangement_mentioned": "status",
    "enforcement_mentioned": "status",
}
STRENGTH_SCORE = {"strong": 3, "moderate": 2, "weak": 1}
MAX_RAW = 200
CURRENCIES = {"EUR": "EUR", "€": "EUR", "USD": "USD", "$": "USD", "CHF": "CHF", "GBP": "GBP"}

# --------------------------------------------------------------------------- data types


@dataclass(frozen=True)
class SourceLine:
    """One line of extracted text with where it came from."""

    page_number: int | None
    line_index: int  # position of the line on its page (native lines first, then OCR lines)
    text: str
    method: str  # "native_text" | "ocr"
    quality_state: str  # "accepted" | "review_required" | "not_applicable"
    ocr_line: int | None = None  # index into the page's OCR lines (page report)
    box: tuple[float, float, float, float] | None = None  # page-normalized-v1, OCR only
    chunk_index: int | None = None


@dataclass(frozen=True)
class Candidate:
    document_class: str
    strength: str
    evidence: tuple[tuple[int | None, int, str], ...]  # (page, line, rule)

    @property
    def score(self) -> int:
        return STRENGTH_SCORE[self.strength]


@dataclass(frozen=True)
class Fact:
    fact_type: str
    assertion: str  # "document_stated" | "rule_inferred"
    raw_value: str
    line: SourceLine
    start: int
    end: int
    normalized: str | None = None
    amount: Decimal | None = None
    currency: str | None = None
    date_value: date | None = None
    as_of: date | None = None

    @property
    def category(self) -> str:
        return FACT_TYPES[self.fact_type]

    @property
    def quality_state(self) -> str:
        # An amount without a stated currency is never complete.
        if self.category == "amount" and self.currency is None:
            return "review_required"
        return self.line.quality_state

    def value_repr(self) -> str:
        if self.category == "amount":
            return f"{self.amount:.2f} {self.currency}" if self.currency else f"{self.amount:.2f}"
        if self.category == "date" and self.date_value is not None:
            return self.date_value.isoformat()
        if self.category == "status":
            return "true"
        return self.normalized or ""

    def key(self, extractor_version: str = EXTRACTOR_VERSION) -> str:
        """Stable identity: the same version and source give the same key on every run."""
        parts = (
            extractor_version,
            self.fact_type,
            str(self.line.page_number),
            str(self.line.line_index),
            str(self.start),
            str(self.end),
            self.value_repr(),
        )
        return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Analysis:
    classifications: tuple[Candidate, ...]
    primary_class: str | None
    classification_status: str  # "classified" | "ambiguous" | "unknown"
    facts: tuple[Fact, ...]
    ignored_instruction_lines: int
    review_required: bool


# --------------------------------------------------------------------------- normalization

_SALUTATIONS = re.compile(
    r"^(?:(?:herrn?|frau|firma|familie|dr\.?|prof\.?|dipl\.?-?\w*\.?)\s+)+", re.IGNORECASE
)
_PUNCT = re.compile(r"[^\w&\s.,\-/]")


def _clean(text: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", text).split())


def normalize_name(value: str) -> str:
    """Case, whitespace and salutation are irrelevant; every other character must match."""
    text = _clean(value).strip(" ,.;:")
    text = _SALUTATIONS.sub("", text)
    text = _PUNCT.sub("", text)
    return " ".join(text.lower().split()).strip(" ,.")


def normalize_identifier(value: str) -> str:
    return " ".join(_clean(value).upper().split()).strip(" ,.;:")


# --------------------------------------------------------------------------- grammars

_AMOUNT = re.compile(
    r"(?P<cur1>EUR|€|USD|CHF|GBP|\$)\s*(?P<num1>\d{1,3}(?:\.\d{3})+,\d{2}|\d+,\d{2})"
    r"|(?P<num2>\d{1,3}(?:\.\d{3})+,\d{2}|\d+,\d{2})(?:\s*(?P<cur2>EUR|€|USD|CHF|GBP|\$))?"
)
_MONTHS = {
    "januar": 1,
    "februar": 2,
    "märz": 3,
    "maerz": 3,
    "april": 4,
    "mai": 5,
    "juni": 6,
    "juli": 7,
    "august": 8,
    "september": 9,
    "oktober": 10,
    "november": 11,
    "dezember": 12,
}
_DATE = re.compile(
    r"\b(?P<d>\d{1,2})\.\s?(?:(?P<m>\d{1,2})\.|(?P<mn>"
    + "|".join(_MONTHS)
    + r")\s)\s?(?P<y>\d{4})\b",
    re.IGNORECASE,
)
_LABEL = re.compile(r"^\s*(?P<label>[^\W\d_][\w .()/\-]{0,60}?)\s*:\s*(?P<value>\S.*)$")
_CASE_VALUE = re.compile(r"^[A-Z0-9][A-Z0-9 \-/.]{0,38}$")
_ID_TOKEN = re.compile(r"(?=[A-Za-z0-9\-/.]*\d)[A-Za-z0-9][A-Za-z0-9\-/.]*")
_ADDRESS = re.compile(
    r"^[^\W\d_][\w .\-]*?(?:straße|strasse|str\.|weg|platz|allee|gasse|ring|damm|ufer|markt)"
    r"\s+\d+\s?[a-z]?,?\s+\d{5}\s+[^\W\d_][\w .\-]*$",
    re.IGNORECASE,
)
_PLACE_DATE = re.compile(r"^[^\W\d_][\w .\-]{1,40},\s*(?:den\s+)?(?P<date>.+)$")
_LEGAL_FORM = re.compile(
    r"(?<!\w)(?:GmbH|AG|KG|mbH|e\.\s?V\.|OHG|GbR|SE|e\.\s?K\.|KGaA|UG|Stadtwerke|Bank|Sparkasse|"
    r"Versicherung|Inkasso)(?!\w)"
)
_AUTHORITY = re.compile(
    r"^(?:Amtsgericht|Landgericht|Oberlandesgericht|Zentrales Mahngericht|Mahngericht|"
    r"Gerichtsvollzieher(?:in)?|Finanzamt|Hauptzollamt)\b[\w .\-]*$"
)
_NEGATION = re.compile(r"\b(?:kein|keine|keinen|keiner|keines|nicht|weder|ohne|niemals)\b")
_LETTER_MARKERS = re.compile(
    r"sehr geehrte|mit freundlichen grüßen|ihre anfrage|ihr schreiben|unser schreiben"
)
_DEBT_VOCABULARY = re.compile(
    r"rechnung|mahn|inkasso|forderung|zahlung|schuld|gläubig|vollstreck|pfänd|darlehen|kredit|"
    r"betrag|kundennummer|vertragskonto|überweis|tilgung|\braten?\b"
)
# German instructions aimed at whoever processes the text; such lines are never data.
_INSTRUCTION = re.compile(
    r"\b(?:neue anweisung|anweisung an|hinweis an das system|ignoriere|vergiss|"
    r"markiere\b.*\bals\b|setze\b.*\bauf\b|ordne\b.*\bzu\b|behandle\b.*\bals\b|"
    r"systemprompt|system prompt)",
    re.IGNORECASE,
)

# Heading patterns, most specific first. A heading is a short line without a label colon.
_HEADINGS: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (cls, re.compile(pattern, re.IGNORECASE))
    for cls, pattern in (
        (
            "settlement_agreement",
            r"^(?:ratenzahlungs|vergleichs|tilgungs|abfindungs)vereinbarung$|^vergleich$",
        ),
        (
            "enforcement_document",
            r"^(?:pfändungs- und überweisungsbeschluss|pfändungsbeschluss|vollstreckungsauftrag"
            r"|vorpfändung|ladung zur abgabe der vermögensauskunft)$",
        ),
        (
            "payment_order_or_court_notice",
            r"^(?:mahnbescheid|vollstreckungsbescheid|urteil|ladung|gerichtlicher mahnbescheid)$",
        ),
        (
            "formal_demand",
            r"^(?:letzte mahnung|letzte zahlungsaufforderung|zahlungsaufforderung"
            r"|aufforderung zur zahlung|mahnung vor klage)$",
        ),
        (
            "payment_reminder",
            r"^(?:zahlungserinnerung|erinnerung|(?:\d\.\s*|erste |zweite |dritte )?mahnung)$",
        ),
        ("payment_receipt", r"^(?:zahlungsbestätigung|zahlungseingangsbestätigung|quittung)$"),
        (
            "creditor_statement",
            r"^(?:forderungsaufstellung|forderungsübersicht|kontoauszug|saldenbestätigung|saldenmitteilung)$",
        ),
        ("loan_agreement", r"^(?:darlehensvertrag|kreditvertrag|ratenkreditvertrag)$"),
        (
            "invoice",
            r"^(?:rechnung|schlussrechnung|jahresabrechnung|abrechnung)(?:\s+nr\.?\s*\S+)?$",
        ),
    )
)
# Running-text mentions (weak evidence only).
_MENTIONS: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (cls, re.compile(pattern, re.IGNORECASE))
    for cls, pattern in (
        ("payment_order_or_court_notice", r"mahnbescheid|vollstreckungsbescheid"),
        ("enforcement_document", r"pfändungs|gerichtsvollzieher"),
        ("debt_collection_letter", r"inkasso"),
        ("payment_reminder", r"\bmahnung\b|zahlungserinnerung"),
    )
)


def _labels(*entries: tuple[str, tuple[str, ...]]) -> list[tuple[str, str]]:
    """``(keyword, fact type)`` pairs, longest keyword first so specific labels win."""
    pairs = [(keyword, kind) for kind, keywords in entries for keyword in keywords]
    return sorted(pairs, key=lambda pair: -len(pair[0]))


PARTY_LABELS = _labels(
    (
        "original_creditor",
        ("ursprünglicher gläubiger", "ursprüngliche gläubigerin", "ursprungsgläubiger"),
    ),
    (
        "current_claimant",
        (
            "forderungsinhaber",
            "forderungsinhaberin",
            "neuer gläubiger",
            "neue gläubigerin",
            "zessionar",
        ),
    ),
    (
        "creditor",
        (
            "gläubiger",
            "gläubigerin",
            "antragsteller",
            "antragstellerin",
            "auftraggeber",
            "auftraggeberin",
            "mandantin",
            "darlehensgeber",
            "darlehensgeberin",
            "kreditgeber",
            "rechnungssteller",
        ),
    ),
    (
        "debtor",
        (
            "schuldner",
            "schuldnerin",
            "hauptschuldner",
            "antragsgegner",
            "antragsgegnerin",
            "darlehensnehmer",
            "darlehensnehmerin",
            "kreditnehmer",
            "kreditnehmerin",
            "rechnungsempfänger",
            "rechnungsempfängerin",
            "zahlungspflichtiger",
            "zahlungspflichtige",
        ),
    ),
    ("guarantor", ("bürge", "bürgin")),
    (
        "representative",
        (
            "vertreten durch",
            "prozessbevollmächtigter",
            "prozessbevollmächtigte",
            "bevollmächtigter",
            "bevollmächtigte",
        ),
    ),
    ("collection_agency", ("inkassounternehmen", "inkassobüro")),
    ("other_party", ("sachbearbeiter", "sachbearbeiterin", "ansprechpartner", "ansprechpartnerin")),
)
AMOUNT_LABELS = _labels(
    (
        "stated_total_claim",
        (
            "gesamtforderung",
            "gesamtbetrag",
            "gesamtsumme",
            "zu zahlender betrag",
            "zahlbetrag",
            "forderung gesamt",
            "summe",
            "gesamt",
        ),
    ),
    (
        "stated_remaining_balance",
        (
            "restforderung",
            "restbetrag",
            "offener betrag",
            "offener saldo",
            "noch offen",
            "verbleibender betrag",
            "ausstehender betrag",
            "saldo",
        ),
    ),
    (
        "payment_made_amount",
        (
            "bereits gezahlt",
            "geleistete zahlungen",
            "erhaltene zahlung",
            "abzüglich zahlungen",
            "zahlungseingang",
            "gezahlt",
            "zahlungen",
        ),
    ),
    (
        "principal_amount",
        (
            "hauptforderung",
            "rechnungsbetrag",
            "ursprünglicher betrag",
            "ursprungsforderung",
            "nettodarlehensbetrag",
            "darlehensbetrag",
            "kreditbetrag",
        ),
    ),
    ("interest_amount", ("verzugszinsen", "vertragszinsen", "sollzinsen", "zinsbetrag", "zinsen")),
    (
        "collection_cost_amount",
        ("inkassokosten", "inkassovergütung", "inkassogebühren", "kosten des inkassos"),
    ),
    (
        "court_cost_amount",
        (
            "gerichtskosten",
            "verfahrenskosten",
            "gerichtsgebühren",
            "gerichtsgebühr",
            "kosten des verfahrens",
        ),
    ),
    (
        "fee_amount",
        (
            "mahngebühren",
            "mahngebühr",
            "mahnkosten",
            "bearbeitungsgebühr",
            "rücklastschriftgebühr",
            "gebühren",
            "gebühr",
        ),
    ),
)
IDENTIFIER_LABELS = _labels(
    (
        "invoice_number",
        ("rechnungsnummer", "rechnungs-nr", "rechnung nr", "re-nr", "ursprüngliche rechnung"),
    ),
    (
        "case_number",
        ("aktenzeichen", "geschäftszeichen", "gerichtsaktenzeichen", "geschäfts-nr", "az"),
    ),
    (
        "contract_reference",
        ("vertragsnummer", "vertragskonto", "darlehensnummer", "kreditnummer", "vertrags-nr"),
    ),
    ("payment_reference", ("verwendungszweck", "zahlungsreferenz", "buchungsreferenz")),
    (
        "reference_number",
        ("kundennummer", "kunden-nr", "vorgangsnummer", "referenz", "unser zeichen", "ihr zeichen"),
    ),
)
DATE_LABELS = _labels(
    ("issue_date", ("rechnungsdatum", "ausstellungsdatum", "datum", "erstellt am")),
    ("due_date", ("fälligkeitsdatum", "fälligkeit", "fällig am", "zahlbar bis", "fällig")),
    ("payment_deadline", ("zahlungsfrist", "zahlungsziel", "spätestens bis", "frist")),
    (
        "interest_calculation_date",
        (
            "zinsen berechnet bis",
            "zinsberechnung bis",
            "zinsen seit",
            "verzinsung ab",
            "zinsen bis",
        ),
    ),
    ("other_date", ("stand",)),
)
_STATUS: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (kind, re.compile(pattern, re.IGNORECASE))
    for kind, pattern in (
        (
            "payment_requested",
            r"bitte (?:überweisen|zahlen|begleichen)|zahlen sie|fordern (?:wir )?sie auf"
            r"|^zahlungsaufforderung$|^letzte mahnung$",
        ),
        (
            "payment_reported",
            r"zahlungseingang|zahlung erhalten|bereits gezahlt|geleistete zahlungen"
            r"|erhaltene zahlung",
        ),
        ("settled", r"vollständig (?:beglichen|bezahlt|ausgeglichen)|ist ausgeglichen"),
        ("payment_arrangement_mentioned", r"ratenzahlung|stundung|tilgungsvereinbarung"),
        (
            "enforcement_mentioned",
            r"zwangsvollstreckung|gerichtsvollzieher|pfändung|vollstreckungsbescheid|vollstreckungsauftrag",
        ),
        (
            "disputed",
            r"\bbestreite\w*|\bbestritten\b|widerspruch\b.*\b(?:eingelegt|erhoben)|einspruch\b.*\beingelegt",
        ),
    )
)
_MODAL = re.compile(r"\b(?:kann|können|könnte|binnen|innerhalb|möglich|dürfen)\b", re.IGNORECASE)
_PAYMENT_SENTENCE = re.compile(
    r"(?:zahlungseingang|zahlung|gutschrift)\s+(?:von|über|in höhe von)\s+", re.IGNORECASE
)
_DEADLINE_SENTENCE = re.compile(
    r"\b(?:bis zum|bis spätestens|spätestens am|bis)\s+$", re.IGNORECASE
)


def _match_label(label: str, table: list[tuple[str, str]]) -> str | None:
    folded = " ".join(label.casefold().split())
    for keyword, kind in table:
        if re.search(rf"(?<!\w){re.escape(keyword)}(?!\w)", folded):
            return kind
    return None


def _parse_amount(match: re.Match[str]) -> tuple[Decimal, str | None] | None:
    number = match.group("num1") or match.group("num2")
    currency = match.group("cur1") or match.group("cur2")
    try:
        value = Decimal(number.replace(".", "").replace(",", "."))
    except (InvalidOperation, AttributeError):
        return None
    return value, CURRENCIES.get(currency) if currency else None


def _parse_date(match: re.Match[str]) -> date | None:
    try:
        month = int(match.group("m")) if match.group("m") else _MONTHS[match.group("mn").lower()]
        return date(int(match.group("y")), month, int(match.group("d")))
    except (ValueError, KeyError, TypeError):
        return None


# --------------------------------------------------------------------------- extraction


class _Extractor:
    def __init__(self, lines: Sequence[SourceLine]) -> None:
        self.lines = list(lines)
        self.facts: list[Fact] = []
        self.ignored = 0
        self.stand: date | None = None
        self.usable: list[SourceLine] = []
        for line in self.lines:
            text = line.text
            if not text.strip():
                continue
            if has_prompt_injection(text) or _INSTRUCTION.search(text):
                self.ignored += 1
                continue
            self.usable.append(line)

    # -- helpers
    def add(
        self, kind: str, line: SourceLine, start: int, end: int, assertion: str, **values: object
    ) -> None:
        raw = line.text[start:end]
        if not raw.strip() or len(raw) > MAX_RAW:
            return
        self.facts.append(Fact(kind, assertion, raw, line, start, end, **values))  # type: ignore[arg-type]

    def first_page(self) -> list[SourceLine]:
        page = self.usable[0].page_number if self.usable else None
        return [line for line in self.usable if line.page_number == page]

    # -- passes
    def run(self) -> tuple[list[Candidate], list[Fact]]:
        candidates = self.classify()
        primary = _primary(candidates)[0]
        sender = self.sender()
        court_context = any(
            _AUTHORITY.match(_clean(line.text)) for line in self.usable
        ) or primary in {
            "payment_order_or_court_notice",
            "enforcement_document",
        }
        inkasso_context = sender is not None and "inkasso" in sender.text.casefold()
        for line in self.usable:
            self.labelled(line, court_context, inkasso_context)
            self.sentences(line)
            self.statuses(line)
        self.header(sender, primary)
        self.addressees()
        self.authorities()
        self.settlement_from_zero()
        self.as_of_dates()
        return candidates, _dedupe(self.facts)

    def sender(self) -> SourceLine | None:
        page = self.first_page()
        if page and _LEGAL_FORM.search(page[0].text) and ":" not in page[0].text:
            return page[0]
        return None

    def classify(self) -> list[Candidate]:
        found: dict[str, Candidate] = {}

        def put(cls: str, strength: str, line: SourceLine, rule: str) -> None:
            evidence = ((line.page_number, line.line_index, rule),)
            current = found.get(cls)
            if current is None or STRENGTH_SCORE[strength] > current.score:
                found[cls] = Candidate(cls, strength, evidence)
            elif STRENGTH_SCORE[strength] == current.score and len(current.evidence) < 5:
                found[cls] = Candidate(cls, strength, current.evidence + evidence)

        if not self.usable:
            return [Candidate("unknown", "weak", ())]
        sender = self.sender()
        inkasso = sender is not None and "inkasso" in sender.text.casefold()
        authority = next(
            (line for line in self.usable if _AUTHORITY.match(_clean(line.text))), None
        )
        headed = False
        for line in self.usable:
            text = _clean(line.text)
            if ":" in text or len(text.split()) > 6:
                continue
            for cls, pattern in _HEADINGS:
                if pattern.match(text.casefold()):
                    headed = True
                    if inkasso and cls in {
                        "formal_demand",
                        "payment_reminder",
                        "creditor_statement",
                    }:
                        put("debt_collection_letter", "strong", line, "heading+collection_sender")
                        put(cls, "weak", line, "heading_from_collection_sender")
                    elif authority is not None and cls in {
                        "payment_order_or_court_notice",
                        "enforcement_document",
                    }:
                        put(cls, "strong", line, "heading+authority")
                    else:
                        put(cls, "moderate", line, "heading")
                    break
        for line in self.usable:
            text = _clean(line.text).casefold()
            if _NEGATION.search(text):
                continue
            for cls, pattern in _MENTIONS:
                if pattern.search(text):
                    put(cls, "weak", line, "mention")
        if not headed:
            whole = " ".join(_clean(line.text).casefold() for line in self.usable)
            financial = bool(_DEBT_VOCABULARY.search(whole) or _AMOUNT.search(whole))
            marker = next(
                (line for line in self.usable if _LETTER_MARKERS.search(line.text.casefold())), None
            )
            if financial and marker is not None:
                put("correspondence", "moderate", marker, "letter_with_financial_topic")
            elif not financial:
                put("unrelated_document", "moderate", self.usable[0], "no_financial_signal")
        if not found:
            put("unknown", "weak", self.usable[0], "no_evidence")
        return sorted(
            found.values(), key=lambda c: (-c.score, DOCUMENT_CLASSES.index(c.document_class))
        )

    def labelled(self, line: SourceLine, court: bool, inkasso: bool) -> None:
        match = _LABEL.match(line.text)
        if match is None:
            return
        label, value_start = match.group("label"), match.start("value")
        value = match.group("value")
        amount = _AMOUNT.search(value)
        dated = _DATE.search(value)
        if amount is not None and not dated:
            kind = _match_label(label, AMOUNT_LABELS) or "unclassified_amount"
            parsed = _parse_amount(amount)
            if parsed is not None:
                self.add(
                    kind,
                    line,
                    value_start + amount.start(),
                    value_start + amount.end(),
                    "document_stated",
                    amount=parsed[0],
                    currency=parsed[1],
                )
            return
        if dated is not None and amount is None:
            kind = _match_label(label, DATE_LABELS) or "other_date"
            parsed_date = _parse_date(dated)
            if parsed_date is not None and "stand" in label.casefold().split():
                self.stand = self.stand or parsed_date
            if parsed_date is not None:
                self.add(
                    kind,
                    line,
                    value_start + dated.start(),
                    value_start + dated.end(),
                    "document_stated",
                    date_value=parsed_date,
                )
            return
        if amount is not None or dated is not None:
            return  # a value mixing amount and date is not interpreted
        identifier = _match_label(label, IDENTIFIER_LABELS)
        if identifier is not None:
            self.identifier(line, identifier, value, value_start, court, inkasso)
            return
        party = _match_label(label, PARTY_LABELS)
        if party is not None:
            self.party(line, party, value, value_start)

    def identifier(
        self, line: SourceLine, kind: str, value: str, offset: int, court: bool, inkasso: bool
    ) -> None:
        if kind == "case_number":
            kind = (
                "court_case_number"
                if court
                else "collection_case_number"
                if inkasso
                else "reference_number"
            )
            candidate = value.split(",")[0].strip()
            if _CASE_VALUE.match(candidate) and any(ch.isdigit() for ch in candidate):
                start = offset + value.index(candidate)
                self.add(
                    kind,
                    line,
                    start,
                    start + len(candidate),
                    "document_stated",
                    normalized=normalize_identifier(candidate),
                )
            return
        token = _ID_TOKEN.search(value)
        if token is None:
            return
        raw = token.group(0).rstrip(".,/")
        start = offset + token.start()
        self.add(
            kind,
            line,
            start,
            start + len(raw),
            "document_stated",
            normalized=normalize_identifier(raw),
        )

    def party(self, line: SourceLine, kind: str, value: str, offset: int) -> None:
        names = value.split(",")[0]
        position = 0
        for part in re.split(r"\s+(?:und|&)\s+|;", names):
            name = part.strip().rstrip(".")
            if not name or not any(ch.isalpha() for ch in name):
                continue
            start = offset + value.index(name, position)
            position = start - offset + len(name)
            self.add(
                kind,
                line,
                start,
                start + len(name),
                "document_stated",
                normalized=normalize_name(name),
            )

    def sentences(self, line: SourceLine) -> None:
        text = line.text
        if _LABEL.match(text):
            return
        payment = _PAYMENT_SENTENCE.search(text)
        if payment is not None:
            amount = _AMOUNT.match(text, payment.end())
            parsed = _parse_amount(amount) if amount else None
            if amount is not None and parsed is not None:
                rest = text[amount.end() :]
                when = re.match(r"\s*(?:am|vom)\s+", rest)
                dated = _DATE.match(rest, when.end()) if when else None
                self.add(
                    "payment_made_amount",
                    line,
                    amount.start(),
                    amount.end(),
                    "document_stated",
                    amount=parsed[0],
                    currency=parsed[1],
                    as_of=_parse_date(dated) if dated else None,
                )
            return
        if re.search(r"überweis|zahl", text, re.IGNORECASE):
            for dated in _DATE.finditer(text):
                if _DEADLINE_SENTENCE.search(text[: dated.start()]):
                    parsed_date = _parse_date(dated)
                    if parsed_date is not None:
                        self.add(
                            "payment_deadline",
                            line,
                            dated.start(),
                            dated.end(),
                            "document_stated",
                            date_value=parsed_date,
                        )
                    return
        if line.page_number in {None, self.usable[0].page_number} and line in self.first_page()[:8]:
            place = _PLACE_DATE.match(_clean(text))
            if place is not None:
                dated = _DATE.fullmatch(place.group("date").strip())
                if dated is not None:
                    start = text.rfind(dated.group(0))
                    parsed_date = _parse_date(dated)
                    if start >= 0 and parsed_date is not None:
                        self.add(
                            "issue_date",
                            line,
                            start,
                            start + len(dated.group(0)),
                            "document_stated",
                            date_value=parsed_date,
                        )
                    return
        if _AMOUNT.search(text) is None:
            for dated in _DATE.finditer(text):
                parsed_date = _parse_date(dated)
                if parsed_date is not None:
                    self.add(
                        "other_date",
                        line,
                        dated.start(),
                        dated.end(),
                        "document_stated",
                        date_value=parsed_date,
                    )

    def statuses(self, line: SourceLine) -> None:
        text = _clean(line.text)
        folded = text.casefold()
        for kind, pattern in _STATUS:
            match = pattern.search(folded)
            if match is None:
                continue
            if _NEGATION.search(folded):
                continue
            if kind == "disputed" and _MODAL.search(folded):
                continue
            start = line.text.casefold().find(match.group(0))
            if start < 0:
                continue
            self.add(kind, line, start, start + len(match.group(0)), "document_stated")

    def header(self, sender: SourceLine | None, primary: str | None) -> None:
        if sender is None:
            return
        text = sender.text.strip()
        start = sender.text.index(text)
        if "inkasso" in text.casefold():
            self.add(
                "collection_agency",
                sender,
                start,
                start + len(text),
                "rule_inferred",
                normalized=normalize_name(text),
            )
            return
        stated = any(fact.fact_type == "creditor" for fact in self.facts)
        if (
            primary
            in {
                "invoice",
                "payment_reminder",
                "formal_demand",
                "creditor_statement",
                "payment_receipt",
                "loan_agreement",
            }
            and not stated
        ):
            self.add(
                "creditor",
                sender,
                start,
                start + len(text),
                "rule_inferred",
                normalized=normalize_name(text),
            )
            page = self.first_page()
            index = page.index(sender)
            for following in page[index + 1 : index + 3]:
                clean = _clean(following.text)
                if _ADDRESS.match(clean):
                    begin = following.text.index(following.text.strip())
                    self.add(
                        "creditor_address",
                        following,
                        begin,
                        begin + len(following.text.strip()),
                        "document_stated",
                        normalized=normalize_name(clean),
                    )
                    break

    def addressees(self) -> None:
        page = self.first_page()[:12]
        for position, line in enumerate(page):
            text = _clean(line.text)
            if ":" in text:
                continue
            if re.fullmatch(r"(?:Herrn?|Frau)", text) and position + 1 < len(page):
                following = page[position + 1]
                name = following.text.strip()
                if 1 < len(name.split()) <= 4 and not any(ch.isdigit() for ch in name):
                    begin = following.text.index(name)
                    self.add(
                        "addressee",
                        following,
                        begin,
                        begin + len(name),
                        "document_stated",
                        normalized=normalize_name(name),
                    )
                continue
            if not re.match(r"^(?:Herrn?|Frau)\s+\S", text):
                continue
            offset = 0
            for part in re.split(r"\s+(?:und|&)\s+", line.text.strip()):
                if not re.match(r"^(?:Herrn?|Frau)\s+\S", part):
                    continue
                name = re.sub(r"^(?:Herrn?|Frau)\s+", "", part).strip()
                if not 1 < len(name.split()) <= 4 or any(ch.isdigit() for ch in name):
                    continue
                begin = line.text.index(name, offset)
                offset = begin + len(name)
                self.add(
                    "addressee",
                    line,
                    begin,
                    begin + len(name),
                    "document_stated",
                    normalized=normalize_name(name),
                )

    def authorities(self) -> None:
        for line in self.usable:
            text = line.text.strip()
            if _AUTHORITY.match(_clean(text)):
                begin = line.text.index(text)
                self.add(
                    "court_or_authority",
                    line,
                    begin,
                    begin + len(text),
                    "document_stated",
                    normalized=normalize_name(text),
                )

    def settlement_from_zero(self) -> None:
        for fact in list(self.facts):
            if fact.fact_type == "stated_remaining_balance" and fact.amount == 0 and fact.currency:
                self.add("settled", fact.line, fact.start, fact.end, "rule_inferred")

    def as_of_dates(self) -> None:
        """A stated "Stand" date applies to the balances and totals of that document."""
        if self.stand is None:
            return
        self.facts = [
            replace(fact, as_of=self.stand)
            if fact.fact_type in {"stated_remaining_balance", "stated_total_claim"}
            and fact.as_of is None
            else fact
            for fact in self.facts
        ]


def _dedupe(facts: Iterable[Fact]) -> list[Fact]:
    seen: dict[str, Fact] = {}
    for fact in facts:
        seen.setdefault(fact.key(), fact)
    return sorted(
        seen.values(),
        key=lambda f: (f.line.page_number or 0, f.line.line_index, f.start, f.fact_type),
    )


def _primary(candidates: Sequence[Candidate]) -> tuple[str | None, str]:
    if not candidates:
        return "unknown", "unknown"
    top = max(candidate.score for candidate in candidates)
    leaders = [candidate for candidate in candidates if candidate.score == top]
    if len(leaders) > 1:
        return None, "ambiguous"
    leader = leaders[0].document_class
    return leader, "unknown" if leader == "unknown" else "classified"


def analyze(lines: Sequence[SourceLine]) -> Analysis:
    extractor = _Extractor(lines)
    candidates, facts = extractor.run()
    primary, status = _primary(candidates)
    review = (
        status != "classified"
        or any(fact.quality_state == "review_required" for fact in facts)
        or any(line.quality_state == "review_required" for line in lines)
    )
    if primary == "unknown" and not lines:
        review = False
    return Analysis(tuple(candidates), primary, status, tuple(facts), extractor.ignored, review)


def lines_from_text(pages: Sequence[Sequence[str]]) -> list[SourceLine]:
    """Native-text source lines of plain pages (tests, evaluation)."""
    return [
        SourceLine(number, index, text, "native_text", "not_applicable", chunk_index=0)
        for number, page in enumerate(pages, start=1)
        for index, text in enumerate(page)
    ]
