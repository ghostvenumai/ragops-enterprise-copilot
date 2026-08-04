# RAGOps Enterprise Copilot

RAGOps Enterprise Copilot ist ein lokal ausfuehrbares Portfolio-Projekt fuer
Enterprise-RAG, typisierte Multi-Agent-Workflows, Mandantentrennung,
AI-Governance, Evaluation und einen kontrollierten autonomen Codex-Loop.

Der Standardmodus verwendet ausschliesslich synthetische Daten und einen
deterministischen Testprovider. Es sind keine kostenpflichtigen LLM-Zugaenge
oder API-Schluessel erforderlich.

## Schnellstart

~~~bash
make setup
make verify
make build
make up
~~~

Danach sind erreichbar:

- Copilot-Arbeitsoberflaeche: <http://localhost:8501>
- FastAPI-Dokumentation: <http://localhost:8000/docs>
- Prometheus-kompatible Metriken: <http://localhost:8000/metrics>

**make demo** fuehrt das deterministische Kommandozeilen-Szenario ohne Docker aus.

## Moderne Copilot-Oberflaeche

Die vollständig deutschsprachige Streamlit-Anwendung startet als nutzbarer Chat und
nicht als statische Metrikseite. Sie enthält:

- deutsch formulierte, belegte Antworten mit Quellen, Evidenzscore, Laufzeit und Tokenverbrauch;
- Auswahl eines synthetischen Mandanten- und Rollenkontexts;
- Beispielanfragen fuer CRM-, Preis- und Compliance-Szenarien;
- mandantengefilterte Wissensbasis und kontrollierte Index-Aktualisierung;
- 20 synthetische Wissensdokumente mit kontrollierter No-Evidence-Ablehnung;
- Live-Telemetrie und gemessene Retrieval-Qualitaet;
- Sicherheitskontrollen und strukturierte Audit-Ereignisse.

Die Mandanten- und Rollenwahl ist eine explizite Demo-Funktion. In einer
Produktivumgebung muessen beide Werte aus verifizierten Identity-Provider-Claims
stammen und duerfen nicht vom Client frei gesetzt werden.

Bedienung, Zustandsmodell und technische Anbindung stehen in
[docs/DASHBOARD.md](docs/DASHBOARD.md). Aufbau und Erweiterung der synthetischen Wissensbasis sind in [docs/KNOWLEDGE_BASE.md](docs/KNOWLEDGE_BASE.md) beschrieben.

## Nachweisbarer Stand

**make verify** fuehrt alle verpflichtenden Quality Gates fail-closed aus. Der
aktuelle Messstand umfasst 46 bestandene Tests, 96,39 Prozent Kerncode-Coverage,
100 Prozent Retrieval Hit Rate, Recall@5 und Citation Coverage sowie 77,38 Prozent Precision@5 auf dem synthetischen
Gold-Datensatz sowie null Tenant Leakage.

## Zweck

Das Projekt demonstriert praktische Faehigkeiten fuer eine Rolle als Applied AI
Engineer: sauberes Python-Design, RAG-Architektur, Sicherheitsgrenzen,
Retrieval-Evaluation, Monitoring, Docker-Hardening, CI/CD und nachvollziehbare
automatisierte Entwicklungsablaeufe.
