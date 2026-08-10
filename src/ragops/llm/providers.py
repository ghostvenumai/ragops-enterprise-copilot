"""LLM provider abstraction with deterministic default provider."""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Protocol

from ragops.storage.models import Citation


@dataclass(frozen=True)
class LLMUsage:
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    estimated_cost_eur: float
    latency_ms: float
    model: str


@dataclass(frozen=True)
class LLMResponse:
    text: str
    usage: LLMUsage
    abstained: bool = False
    used_source_ids: tuple[str, ...] = ()


class LLMProvider(Protocol):
    model: str

    def generate(self, question: str, citations: list[Citation], context: str) -> LLMResponse:
        """Generate a grounded answer from already selected evidence."""


class DeterministicTestProvider:
    model = "deterministic-ragops-v1"

    _GERMAN_SUMMARIES = {
        "Alpha Enterprise Renewal Sales Guide": (
            "Bei Verlängerungsrisiken sind eine bereichsübergreifende "
            "Verlängerungsrunde, ein Sponsor aus der Geschäftsleitung, eine "
            "abgestimmte Support-Eskalation, ein kommerzieller Bindungsplan und "
            "ein Termin zur Nutzenbewertung vorgesehen."
        ),
        "Atlas Control Plane Product Guide v2": (
            "Für Atlas Control Plane gelten 99,9 Prozent monatliche Verfügbarkeit. "
            "Kritische Vorfälle müssen innerhalb von 30 Minuten an den Kundenservice "
            "eskaliert werden; eine Zusammenfassung für die Geschäftsleitung folgt "
            "innerhalb eines Arbeitstags. Version 2 ersetzt Version 1."
        ),
        "Atlas Control Plane Product Guide v1 outdated": (
            "Die frühere Angabe von 99,5 Prozent monatlicher Verfügbarkeit ist "
            "veraltet und darf die aktuelle Version 2 nicht ersetzen."
        ),
        "Alpha Synthetic Price List v2": (
            "Das Enterprise-Paket von Atlas Control Plane beginnt bei 180.000 EUR "
            "pro Jahr, die Beacon Support Suite bei 95.000 EUR und das "
            "Compliance-Zusatzmodul bei 60.000 EUR."
        ),
        "Alpha AI Governance and Compliance Policy v2": (
            "KI-Antworten müssen freigegebene Quellen nennen und bei unzureichender "
            "Evidenz abgelehnt werden. Dokumentanweisungen gelten als nicht "
            "vertrauenswürdig; personenbezogene Daten müssen maskiert und Uploads "
            "mit Path Traversal vor der Verarbeitung abgelehnt werden."
        ),
        "Beta Atlas Product Guide v2": (
            "Für die Beta-Umgebung sind 99,8 Prozent monatliche Verfügbarkeit, "
            "regionale Vorfallkoordination und eine Kundenservice-Eskalation bei "
            "kritischen offenen Tickets vorgesehen."
        ),
        "Beta Enterprise Renewal Sales Guide": (
            "Bei Verlängerungsrisiken sind die Abstimmung mit einem Sponsor aus der "
            "Geschäftsleitung, ein Support-Wiederherstellungsplan, ein "
            "Beschaffungszeitplan und eine Risikonotiz erforderlich."
        ),
        "Gamma Enterprise Renewal Playbook": (
            "Für Verlängerungen werden Vertragslaufzeit, Support-Schweregrad, "
            "Produktnutzung und Sponsorabdeckung gemeinsam bewertet. Vorgesehen sind "
            "technische Stabilisierung, eine kommerzielle Überbrückung und ein "
            "Termin auf Geschäftsleitungsebene."
        ),
        "Gamma Operations Continuity Policy": (
            "Kritische Supportfälle benötigen eine Einsatzleitung, tägliche "
            "Wiederherstellungsnotizen und Statusmeldungen an die Geschäftsleitung, "
            "wenn eine Enterprise-Verlängerung gefährdet ist."
        ),
        "Alpha Plattformarchitektur v1": (
            "Der deterministische Standardmodus nutzt ein lokales dateibasiertes "
            "Repository. PostgreSQL 16 und Qdrant 1.15 sind als Zieladapter im "
            "Docker-Profil bereitgestellt, aber nicht der Persistenzkern der lokalen Tests."
        ),
        "Alpha Support-Betriebshandbuch v2": (
            "Kritische Supportfälle erhalten innerhalb von 30 Minuten eine "
            "Einsatzleitung und Kundenservice-Eskalation. Bis zur Stabilisierung "
            "wird mindestens täglich ein Status dokumentiert."
        ),
        "Alpha CRM-Datenschutzleitfaden v1": (
            "CRM-Abfragen sind auf den aktiven Mandanten begrenzt. Freigegebene "
            "synthetische Geschäftsattribute dürfen verarbeitet werden; persönliche "
            "Kontakt- und Bankdaten werden maskiert oder abgelehnt."
        ),
        "Alpha Vertragslebenszyklus-Richtlinie v1": (
            "Aktive Enterprise-Verträge innerhalb von 60 Tagen werden bei einem "
            "gleichzeitig kritischen offenen Supportfall als Verlängerungsrisiko markiert."
        ),
        "Beta Kundenservice-Handbuch v1": (
            "Kritische offene Tickets benötigen regionale Koordination, eine benannte "
            "Einsatzleitung, einen Wiederherstellungsplan und tägliche Statuspflege."
        ),
        "Beta Preisliste v1": (
            "Das Beta Atlas Enterprise-Paket beginnt bei 175.000 EUR pro Jahr; "
            "regionales Incident-Coaching beginnt bei 40.000 EUR pro Jahr."
        ),
        "Beta Datenschutzrichtlinie v1": (
            "Tenant-beta-Daten benötigen eine verifizierte Mandanten- und "
            "Rollenfreigabe. Personenbezogene Daten werden maskiert und "
            "mandantenübergreifende Abfragen blockiert."
        ),
        "Gamma Nexus Produktleitfaden v1": (
            "Gamma Nexus koordiniert Wartungs- und Wiederherstellungsabläufe. Das "
            "synthetische Verfügbarkeitsziel beträgt 99,7 Prozent pro Monat."
        ),
        "Gamma Preisliste v1": (
            "Gamma Nexus beginnt bei 140.000 EUR pro Jahr; das Kontinuitätsmodul "
            "beginnt bei 35.000 EUR pro Jahr."
        ),
        "Marketing Campaign Guide": (
            "Kampagnen werden quartalsweise über E-Mail, Social, Search, Events und "
            "Partner geplant. Vor jeder Ansprache sind Zielsegment, UTM-Tracking und "
            "eine Double-Opt-in-Einwilligung dokumentiert; qualifizierte Leads gehen "
            "mit Kampagnenquelle an den Vertrieb."
        ),
        "Synthetic Campaign Overview 2026": (
            "Für 2026 sind synthetische Kampagnen für Industriekunden, "
            "Midmarket-Webinare und Partner-Referrals geplant, mit 40, 25 und 30 "
            "qualifizierten Leads als Zielwerten."
        ),
        "Marketing Playbook EMEA": (
            "Account-basierte Kampagnen für Healthcare- und Energie-Segmente folgen "
            "festen Schritten: Accountauswahl mit dem Vertrieb, segmentspezifische "
            "Botschaften, koordinierte E-Mail- und Event-Kontakte und wöchentliche "
            "Auswertung. Kontakte ohne gültiges Opt-in werden automatisch ausgeschlossen."
        ),
        "Marketing Events and Webinar Guide": (
            "Quartalsweise Webinare und zwei Konferenzen pro Jahr liefern "
            "Event-Leads, die mit expliziter Einwilligung erfasst und innerhalb von "
            "zwei Arbeitstagen ins CRM importiert werden. Follow-ups sind auf fünf "
            "Kontakte begrenzt."
        ),
        "Copilot Security Overview": (
            "Jede Anfrage durchläuft den Tenant Guard, Rollenstufen steuern die "
            "Dokumentsensitivität, erkannte Injection-Muster werden ausgeschlossen, "
            "personenbezogene Daten maskiert, Antworten benötigen Quellen und "
            "Uploads unterliegen einer Allowlist mit Größenlimits."
        ),
        "Order and Fulfillment Guide": (
            "Bestellungen durchlaufen Angebotsbestätigung, kommerzielle Freigabe, "
            "Provisionierung und Aktivierungsübergabe. Die Standard-Provisionierung "
            "dauert fünf Arbeitstage, beschleunigt zwei Arbeitstage mit "
            "Operations-Freigabe."
        ),
        "Hardware Logistics and Delivery Policy": (
            "Hardware wird nur mit Sendungsverfolgung über zwei freigegebene "
            "Carrier geliefert; zehn Arbeitstage in Nordamerika, fünfzehn "
            "international. Transportschäden werden innerhalb von drei Arbeitstagen "
            "kostenfrei ersetzt."
        ),
    }

    def generate(self, question: str, citations: list[Citation], context: str) -> LLMResponse:
        started = time.perf_counter()
        prompt_tokens = len(question.split()) + len(context.split())
        if not citations:
            text = (
                "Ich verweigere eine fachliche Antwort, weil keine ausreichende "
                "Evidenz im zulässigen Mandantenkontext gefunden wurde."
            )
            completion_tokens = len(text.split())
            return LLMResponse(
                text=text,
                usage=LLMUsage(
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    total_tokens=prompt_tokens + completion_tokens,
                    estimated_cost_eur=0.0,
                    latency_ms=round((time.perf_counter() - started) * 1000, 3),
                    model=self.model,
                ),
                abstained=True,
            )
        selected = self._select_citations(question, citations)
        lines = ["**Ergebnis auf Basis der verfügbaren synthetischen Evidenz**", ""]
        for citation in selected:
            summary = self._german_summary(citation)
            lines.append(f"- {summary} [{citation.source_id}]")
        lines.extend(
            (
                "",
                "**Unsicherheit:** niedrig bis mittel, abhängig von Aktualität und "
                "Vollständigkeit der Quellen.",
            )
        )
        text = "\n".join(lines)
        completion_tokens = len(text.split())
        return LLMResponse(
            text=text,
            usage=LLMUsage(
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=prompt_tokens + completion_tokens,
                estimated_cost_eur=0.0,
                latency_ms=round((time.perf_counter() - started) * 1000, 3),
                model=self.model,
            ),
            used_source_ids=tuple(citation.source_id for citation in selected),
        )

    def _german_summary(self, citation: Citation) -> str:
        if citation.source_id.startswith("CRM:"):
            return citation.snippet.rstrip(".") + "."
        return self._GERMAN_SUMMARIES.get(
            citation.title,
            f'Die Quelle "{citation.title}" wurde als relevante Evidenz eingestuft.',
        )

    def _select_citations(self, question: str, citations: list[Citation]) -> list[Citation]:
        lowered = question.lower()
        crm = [citation for citation in citations if citation.source_id.startswith("CRM:")]
        if crm:
            guides = [citation for citation in citations if "Renewal" in citation.title]
            return crm + guides[:1]

        rules = (
            (
                ("datenbank", "datenspeicher", "persistenz", "postgresql", "qdrant"),
                ("Plattformarchitektur",),
            ),
            (("preis", "pricing", "beacon"), ("Price List", "Preisliste")),
            (
                ("compliance", "datenschutz", "richtlinie", "evidenz", "path traversal"),
                ("Policy", "Datenschutz", "Richtlinie", "Compliance"),
            ),
            (
                ("support", "ticket", "eskalation", "vorfall"),
                ("Support-Betriebshandbuch", "Kundenservice-Handbuch", "Operations Continuity"),
            ),
            (
                ("renewal", "verlänger", "vertrag", "massnahme", "maßnahme", "actions"),
                ("Renewal", "Playbook", "Vertragslebenszyklus"),
            ),
            (
                ("produkt", "atlas", "nexus", "verfügbarkeit", "verfuegbarkeit", "sla"),
                ("Product Guide", "Produktleitfaden"),
            ),
        )
        if any(term in lowered for term in ("alte", "alt ", "99.5", "99,5")):
            outdated = [citation for citation in citations if "outdated" in citation.title]
            current = [citation for citation in citations if citation.title.endswith("v2")]
            return (outdated + current)[:2] or citations[:2]
        for terms, title_terms in rules:
            if any(term in lowered for term in terms):
                matches = [
                    citation
                    for citation in citations
                    if any(term in citation.title for term in title_terms)
                ]
                if matches:
                    return matches[:2]
        if any(term in lowered for term in ("sla", "verfügbarkeit", "verfuegbarkeit", "atlas")):
            current = [
                citation
                for citation in citations
                if "Product Guide" in citation.title and "outdated" not in citation.title
            ]
            if current:
                return current[:1]
        return citations[:2]


class OpenAIProvider:
    def __init__(self) -> None:
        api_key = os.getenv("OPENAI_API_KEY")
        model = os.getenv("OPENAI_MODEL")
        if not api_key or not model:
            raise RuntimeError("OPENAI_API_KEY and OPENAI_MODEL are required for OpenAIProvider")
        self.model = model
        self._api_key_present = bool(api_key)

    def generate(self, question: str, citations: list[Citation], context: str) -> LLMResponse:
        raise NotImplementedError(
            "OpenAIProvider is configured but external calls are disabled in tests"
        )


class AzureOpenAIProvider:
    def __init__(self) -> None:
        required = [
            "AZURE_OPENAI_API_KEY",
            "AZURE_OPENAI_ENDPOINT",
            "AZURE_OPENAI_DEPLOYMENT",
            "AZURE_OPENAI_API_VERSION",
        ]
        missing = [name for name in required if not os.getenv(name)]
        if missing:
            raise RuntimeError(f"AzureOpenAIProvider missing env vars: {', '.join(missing)}")
        self.model = os.environ["AZURE_OPENAI_DEPLOYMENT"]

    def generate(self, question: str, citations: list[Citation], context: str) -> LLMResponse:
        raise NotImplementedError(
            "AzureOpenAIProvider is configured but external calls are disabled in tests"
        )


def provider_from_env() -> LLMProvider:
    provider = os.getenv("RAGOPS_LLM_PROVIDER", "deterministic").lower()
    if provider == "openai":
        return OpenAIProvider()
    if provider == "azure_openai":
        return AzureOpenAIProvider()
    return DeterministicTestProvider()
