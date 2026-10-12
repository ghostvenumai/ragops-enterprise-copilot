"""Held-out synthetic letters for ``rules-de-v1`` (ENT-11.8), written after the rules.

The design corpus (``intelligence_fixtures``) was written together with the rules, so its
metrics are in-sample. These letters use phrasings the rules were not written against
(inline labels, amounts before the currency, missing colons, IBAN lines, salutations with
titles). Their annotations state what a careful human reader would extract. The rules are NOT
tuned on this set: its metrics are reported as measured, including every miss.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict

from scripts.intelligence_fixtures import Letter, _f

HOLDOUT: tuple[Letter, ...] = (
    Letter(
        "h_invoice_inline",
        (
            (
                "Elektro Beispiel GmbH",
                "Am Markt 7, 34117 Beispielburg",
                "Herrn Bernd Probe",
                "Beispielburg, den 04. Februar 2026",
                "Rechnung Nr. EB-1209",
                "Für die Reparatur berechnen wir Ihnen:",
                "Arbeitszeit 2 Stunden: 140,00 EUR",
                "Material: 35,50 EUR",
                "Gesamtbetrag: EUR 175,50",
                "Bitte überweisen Sie den Betrag innerhalb von 14 Tagen.",
                "IBAN: DE00 1234 5678 9012 3456 78",
            ),
        ),
        "invoice",
        _f(
            ("creditor", "elektro beispiel gmbh"),
            ("creditor_address", "am markt 7, 34117 beispielburg"),
            ("addressee", "bernd probe"),
            ("issue_date", "2026-02-04"),
            ("invoice_number", "EB-1209"),
            ("unclassified_amount", "140.00 EUR"),
            ("unclassified_amount", "35.50 EUR"),
            ("stated_total_claim", "175.50 EUR"),
            ("payment_requested", "true"),
        ),
    ),
    Letter(
        "h_second_reminder_fee",
        (
            (
                "Versandhaus Lindenhof KG",
                "Lindenallee 12, 50667 Musterstadt",
                "Frau Dr. Anna Beispiel",
                "Datum: 01.03.2026",
                "2. Mahnung",
                "Ihre Kundennummer: K-48213",
                "Rechnung vom 12.01.2026, Rechnungsnummer VL-2026-0147",
                "Rechnungsbetrag 400,00 EUR",
                "Mahngebühr 5,00 EUR",
                "Zu zahlender Betrag 405,00 EUR",
                "Sollte die Zahlung bereits erfolgt sein, betrachten Sie dieses Schreiben als "
                "gegenstandslos.",
            ),
        ),
        "payment_reminder",
        _f(
            ("creditor", "versandhaus lindenhof kg"),
            ("creditor_address", "lindenallee 12, 50667 musterstadt"),
            ("addressee", "anna beispiel"),
            ("issue_date", "2026-03-01"),
            ("reference_number", "K-48213"),
            ("invoice_number", "VL-2026-0147"),
            ("principal_amount", "400.00 EUR"),
            ("fee_amount", "5.00 EUR"),
            ("stated_total_claim", "405.00 EUR"),
        ),
        frozenset(),
        "unknown",
        "C1",
        forbidden=_f(("payment_reported", "true"), ("settled", "true")),
    ),
    Letter(
        "h_inkasso_assignment",
        (
            (
                "Nord Inkasso GmbH & Co. KG",
                "Deichweg 2, 26122 Beispielhaven",
                "Herrn Bernd Probe",
                "Datum: 18.03.2026",
                "Letzte Mahnung vor gerichtlichen Schritten",
                "Unser Zeichen: NI-55-0912",
                "Ursprünglicher Gläubiger: Fernwärme Beispieltal AG",
                "Forderungsinhaberin: Nordfinanz Portfolio AG",
                "Schuldner: Bernd Probe",
                "Hauptforderung: 400,00 EUR",
                "Verzugszinsen: 9,10 EUR",
                "Inkassovergütung: 58,50 EUR",
                "Gesamtforderung: 467,60 EUR",
                "Wir bestreiten nicht, dass Teilzahlungen möglich sind.",
            ),
        ),
        "debt_collection_letter",
        _f(
            ("collection_agency", "nord inkasso gmbh & co. kg"),
            ("addressee", "bernd probe"),
            ("issue_date", "2026-03-18"),
            ("reference_number", "NI-55-0912"),
            ("original_creditor", "fernwärme beispieltal ag"),
            ("current_claimant", "nordfinanz portfolio ag"),
            ("debtor", "bernd probe"),
            ("principal_amount", "400.00 EUR"),
            ("interest_amount", "9.10 EUR"),
            ("collection_cost_amount", "58.50 EUR"),
            ("stated_total_claim", "467.60 EUR"),
        ),
        frozenset({"Person B"}),
        "identified",
        "",
        forbidden=_f(("disputed", "true")),
    ),
    Letter(
        "h_enforcement_order",
        (
            (
                "Amtsgericht Beispieltal",
                "Vollstreckungsbescheid",
                "Geschäftszeichen: 26-4471123-0-5",
                "Antragstellerin: Fernwärme Beispieltal AG",
                "Antragsgegner: Bernd Probe",
                "Hauptforderung: 400,00 EUR",
                "Kosten des Verfahrens: 36,00 EUR",
                "Zinsen ab 06.03.2026",
            ),
        ),
        "payment_order_or_court_notice",
        _f(
            ("court_or_authority", "amtsgericht beispieltal"),
            ("court_case_number", "26-4471123-0-5"),
            ("creditor", "fernwärme beispieltal ag"),
            ("debtor", "bernd probe"),
            ("principal_amount", "400.00 EUR"),
            ("court_cost_amount", "36.00 EUR"),
            ("interest_calculation_date", "2026-03-06"),
            ("enforcement_mentioned", "true"),
        ),
        frozenset({"Person B"}),
        "identified",
    ),
    Letter(
        "h_bank_receipt_partial",
        (
            (
                "Musterbank AG",
                "Bankplatz 2, 60311 Beispielfurt",
                "Herrn Bernd Probe",
                "Beispielfurt, 02.04.2026",
                "Zahlungsbestätigung",
                "Darlehensnummer: DN-301-552",
                "Wir bestätigen den Zahlungseingang über 100,00 EUR am 01.04.2026.",
                "Verbleibender Betrag: 1.927,35 EUR",
            ),
        ),
        "payment_receipt",
        _f(
            ("creditor", "musterbank ag"),
            ("creditor_address", "bankplatz 2, 60311 beispielfurt"),
            ("addressee", "bernd probe"),
            ("issue_date", "2026-04-02"),
            ("contract_reference", "DN-301-552"),
            ("payment_made_amount", "100.00 EUR"),
            ("stated_remaining_balance", "1927.35 EUR"),
            ("payment_reported", "true"),
        ),
        frozenset(),
        "unknown",
        "C6",
        forbidden=_f(("settled", "true")),
    ),
    Letter(
        "h_newsletter",
        (
            (
                "Stadtbibliothek Musterstadt",
                "Neue Öffnungszeiten ab 01.05.2026",
                "Montag bis Freitag 9 bis 18 Uhr.",
                "Ihre Bibliothek",
            ),
        ),
        "unrelated_document",
        _f(("other_date", "2026-05-01")),
    ),
    Letter(
        "h_dispute_letter",
        (
            (
                "Versandhaus Lindenhof KG",
                "Lindenallee 12, 50667 Musterstadt",
                "Frau Anna Beispiel",
                "Musterstadt, 20.03.2026",
                "Ihr Schreiben vom 15.03.2026",
                "Sehr geehrte Frau Beispiel,",
                "Sie haben die Forderung aus Rechnungsnummer VL-2026-0311 bestritten.",
                "Wir prüfen Ihren Einwand und melden uns.",
                "Mit freundlichen Grüßen",
            ),
        ),
        "correspondence",
        _f(
            ("addressee", "anna beispiel"),
            ("issue_date", "2026-03-20"),
            ("other_date", "2026-03-15"),
            ("invoice_number", "VL-2026-0311"),
            ("disputed", "true"),
        ),
    ),
)


def holdout_digest() -> str:
    payload = [
        {
            **asdict(letter),
            "facts": sorted(letter.facts),
            "debtors": sorted(letter.debtors),
            "forbidden": sorted(letter.forbidden),
        }
        for letter in HOLDOUT
    ]
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()
