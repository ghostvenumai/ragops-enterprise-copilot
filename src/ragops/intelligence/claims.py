"""Conservative claim grouping and financial aggregation.

Grouping uses strong evidence only: the same normalized claim creditor and the same invoice
number or contract reference. Aggregation never sums every amount: per claim it takes the
stated total or remaining balance of the latest member document, labels earlier amounts as
historical, and produces a consolidated total only when every amount-bearing document is in a
resolved claim. Money is ``Decimal``; currencies are never combined.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from ragops.intelligence.rules import Fact

GROUP_STATUSES = ("system_linked", "review_required", "contradictory", "confirmed", "dismissed")
MEMBER_STATES = ("linked", "proposed", "confirmed", "rejected")
CLAIM_CLASSES_EXCLUDED = frozenset({"unrelated_document", "unknown"})
BALANCE_TYPES = ("stated_remaining_balance", "stated_total_claim")
COMPONENT_TYPES = frozenset(
    {
        "principal_amount",
        "interest_amount",
        "fee_amount",
        "collection_cost_amount",
        "court_cost_amount",
    }
)


@dataclass(frozen=True)
class ClaimKey:
    creditor: str
    identifier_type: str  # "invoice_number" | "contract_reference"
    identifier: str

    @property
    def digest(self) -> str:
        raw = "\x1f".join(("claim-v1", self.creditor, self.identifier_type, self.identifier))
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def claim_creditor(facts: Iterable[Fact]) -> str:
    """The original creditor where stated, else a stated creditor, else an inferred one."""
    facts = list(facts)
    for kinds in (("original_creditor",), ("creditor",)):
        for assertion in ("document_stated", "rule_inferred"):
            for fact in facts:
                if fact.fact_type in kinds and fact.assertion == assertion and fact.normalized:
                    return fact.normalized
    return ""


def claim_keys(facts: Sequence[Fact], primary_class: str | None) -> list[ClaimKey]:
    if primary_class in CLAIM_CLASSES_EXCLUDED:
        return []
    creditor = claim_creditor(facts)
    for kind in ("invoice_number", "contract_reference"):
        values = sorted(
            {fact.normalized for fact in facts if fact.fact_type == kind and fact.normalized}
        )
        if values:
            return [ClaimKey(creditor, kind, value) for value in values]
    return []


def group_status(creditor: str, member_debtors: Sequence[frozenset[str]]) -> tuple[str, list[str]]:
    """System status of a group from its creditor and its members' identified debtors."""
    if not creditor:
        return "review_required", ["creditor_unknown"]
    identified = {debtors for debtors in member_debtors if debtors}
    if len(identified) > 1:
        return "contradictory", ["debtor_conflict"]
    return "system_linked", ["creditor_and_identifier_match"]


# --------------------------------------------------------------------------- aggregation


@dataclass(frozen=True)
class AmountView:
    fact_id: str
    fact_type: str
    amount: Decimal
    currency: str | None
    as_of: date | None
    review_state: str
    quality_state: str


@dataclass
class MemberView:
    member_id: str
    document_id: str
    version_id: str
    title: str
    membership: str
    review_version: int
    document_class: str | None
    issue_date: date | None
    settled: bool
    amounts: list[AmountView] = field(default_factory=list)


@dataclass
class GroupView:
    group_id: str
    status: str
    creditor: str
    identifier_type: str
    identifier: str
    review_version: int
    members: list[MemberView]


def _money(value: Decimal) -> str:
    return f"{value.quantize(Decimal('0.01'))}"


def _latest(members: list[MemberView]) -> tuple[MemberView, AmountView] | str:
    """The balance of the latest member by issue date, or why there is none."""
    for kinds in (BALANCE_TYPES, ("principal_amount",)):
        candidates = [
            (member, amount)
            for member in members
            for amount in member.amounts
            if amount.fact_type in kinds
        ]
        if not candidates:
            continue
        if any(member.issue_date is None for member, _ in candidates):
            return "issue_date_missing"
        newest = max(member.issue_date for member, _ in candidates if member.issue_date)
        latest = [(m, a) for m, a in candidates if m.issue_date == newest]
        values = {(a.amount, a.currency) for _, a in latest}
        if len(values) > 1:
            return "conflicting_latest_amounts"
        member, amount = latest[0]
        if amount.currency is None:
            return "currency_missing"
        if amount.quality_state == "review_required" or amount.review_state == "review_required":
            return "amount_review_required"
        return member, amount
    return "no_stated_amount"


def summarize_claim(group: GroupView) -> dict[str, object]:
    active = [m for m in group.members if m.membership in {"linked", "confirmed"}]
    reasons: list[str] = []
    if group.status not in {"system_linked", "confirmed"}:
        reasons.append(f"group_{group.status}")
    if any(m.membership == "proposed" for m in group.members):
        reasons.append("membership_proposed")
    latest = _latest(active)
    current: dict[str, object] | None = None
    latest_member: MemberView | None = None
    if isinstance(latest, str):
        reasons.append(latest)
    else:
        latest_member, amount = latest
        current = {
            "amount": _money(amount.amount),
            "currency": amount.currency,
            "as_of": (amount.as_of or latest_member.issue_date or date.min).isoformat(),
            "fact_id": amount.fact_id,
            "document_id": latest_member.document_id,
            "basis": "latest_stated_balance"
            if amount.fact_type in BALANCE_TYPES
            else "latest_principal_only",
        }
    documents = []
    for member in sorted(group.members, key=lambda m: (m.issue_date or date.min, m.title)):
        rows = []
        for amount in member.amounts:
            if amount.fact_type == "payment_made_amount":
                role = "payment"
            elif amount.fact_type == "unclassified_amount":
                role = "unclassified"
            elif member is latest_member and amount.fact_type in BALANCE_TYPES:
                role = "potentially_current"
            elif member is latest_member and amount.fact_type in COMPONENT_TYPES:
                role = "component"
            else:
                role = "historical"
            rows.append(
                {
                    "fact_id": amount.fact_id,
                    "fact_type": amount.fact_type,
                    "amount": _money(amount.amount),
                    "currency": amount.currency,
                    "as_of": amount.as_of.isoformat() if amount.as_of else None,
                    "role": role,
                    "review_state": amount.review_state,
                }
            )
        documents.append(
            {
                "member_id": member.member_id,
                "document_id": member.document_id,
                "version_id": member.version_id,
                "title": member.title,
                "membership": member.membership,
                "review_version": member.review_version,
                "document_class": member.document_class,
                "issue_date": member.issue_date.isoformat() if member.issue_date else None,
                "amounts": rows,
            }
        )
    settled = bool(
        current is not None
        and Decimal(str(current["amount"])) == 0
        and latest_member is not None
        and latest_member.settled
    )
    return {
        "claim_group_id": group.group_id,
        "status": group.status,
        "review_version": group.review_version,
        "creditor": group.creditor,
        "identifier_type": group.identifier_type,
        "identifier": group.identifier,
        "documents": documents,
        "potentially_current": current,
        "settled": settled,
        "resolved": not reasons,
        "unresolved_reasons": reasons,
    }


def summarize(
    groups: Sequence[GroupView], ungrouped: Sequence[tuple[str, str, str]]
) -> dict[str, object]:
    """Claims, unresolved items and a consolidated total only when it is safe.

    ``ungrouped`` lists amount-bearing documents outside any active claim group as
    ``(document_id, title, reason)``.
    """
    claims = [summarize_claim(group) for group in groups]
    unresolved: list[dict[str, object]] = [
        {"document_id": document_id, "title": title, "reason": reason}
        for document_id, title, reason in ungrouped
    ]
    for claim in claims:
        if not claim["resolved"]:
            unresolved.append(
                {
                    "claim_group_id": claim["claim_group_id"],
                    "reason": ",".join(claim["unresolved_reasons"]),  # type: ignore[arg-type]
                }
            )
    subtotal: dict[str, Decimal] = {}
    for claim in claims:
        current = claim["potentially_current"]
        if claim["resolved"] and isinstance(current, dict):
            currency = str(current["currency"])
            subtotal[currency] = subtotal.get(currency, Decimal("0")) + Decimal(
                str(current["amount"])
            )
    complete = not unresolved
    totals = {currency: _money(value) for currency, value in sorted(subtotal.items())}
    return {
        "claims": claims,
        "unresolved": unresolved,
        "complete": complete,
        "consolidated_total": totals if complete else None,
        "resolved_subtotal": totals,
        "aggregation_rule": "latest_stated_balance_per_resolved_claim_v1",
    }
