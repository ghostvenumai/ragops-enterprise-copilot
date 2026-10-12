"""Person attribution from document evidence and explicitly configured aliases.

A person is linked as debtor only when an explicitly labelled debtor party equals one of the
person's configured name aliases after normalization — exact, never fuzzy. Everything weaker
(addressee only, identifier only, several candidates, low-quality OCR) is a review proposal.
An address never links anyone and no relationship between people is inferred.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from ragops.intelligence.rules import Fact

LINK_STATES = ("evidenced", "review_required", "confirmed", "rejected", "withdrawn")
DEBTOR_STATUSES = ("identified", "ambiguous", "unmatched", "unknown")
IDENTIFIER_TYPES = frozenset({"reference_number", "contract_reference", "invoice_number"})


@dataclass(frozen=True)
class LinkProposal:
    person_id: str
    role: str  # "debtor" | "addressee"
    state: str  # "evidenced" | "review_required"
    reasons: tuple[str, ...]


def propose_links(
    facts: Iterable[Fact],
    name_aliases: Mapping[str, str],
    identifier_aliases: Mapping[str, str] | None = None,
) -> list[LinkProposal]:
    """Link proposals of one document; aliases map a normalized value to a person id."""
    facts = list(facts)
    identifier_aliases = identifier_aliases or {}
    debtors = [fact for fact in facts if fact.fact_type == "debtor"]
    links: dict[tuple[str, str], LinkProposal] = {}

    def put(person: str, role: str, state: str, reason: str) -> None:
        current = links.get((person, role))
        if current is None:
            links[(person, role)] = LinkProposal(person, role, state, (reason,))
            return
        # Evidence never downgrades: a single review-required source keeps the link in review.
        merged = "review_required" if "review_required" in {current.state, state} else state
        reasons = tuple(dict.fromkeys((*current.reasons, reason)))
        links[(person, role)] = LinkProposal(person, role, merged, reasons)

    for fact in debtors:
        person = name_aliases.get(fact.normalized or "")
        if person is None:
            continue
        verified = fact.quality_state != "review_required"
        put(
            person,
            "debtor",
            "evidenced" if verified else "review_required",
            "debtor_label_name_match" if verified else "debtor_match_low_quality_source",
        )
    debtor_people = {link.person_id for link in links.values()}
    claim_content = any(
        fact.category == "amount" or fact.fact_type in IDENTIFIER_TYPES for fact in facts
    )
    for fact in facts:
        if fact.fact_type != "addressee":
            continue
        person = name_aliases.get(fact.normalized or "")
        if person is None:
            continue
        # Being the addressee is a document fact of its own, never proof of being the debtor.
        put(
            person,
            "addressee",
            "evidenced" if fact.quality_state != "review_required" else "review_required",
            "addressee_name_match",
        )
        # Without any named debtor, an addressee of a claim letter is only a review proposal.
        if not debtors and claim_content:
            put(person, "debtor", "review_required", "addressee_only")
    for fact in facts:
        if fact.fact_type not in IDENTIFIER_TYPES:
            continue
        person = identifier_aliases.get(fact.normalized or "")
        if person is not None and person not in debtor_people:
            put(person, "debtor", "review_required", "identifier_only")
    return sorted(links.values(), key=lambda link: (link.person_id, link.role))


def debtor_status(has_debtor_label: bool, states: Iterable[str], *, decided: bool = False) -> str:
    """Document-level debtor status from the effective link states of its debtor links."""
    effective = [state for state in states if state not in {"rejected", "withdrawn"}]
    if any(state == "review_required" for state in effective) and not decided:
        return "ambiguous"
    if any(state in {"evidenced", "confirmed"} for state in effective):
        return "identified"
    return "unmatched" if has_debtor_label else "unknown"
