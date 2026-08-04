# Dashboard-Verifikation

Datum: 2026-08-04

## Umfang

- deutschsprachiger, Chat-zentrierter Streamlit-Arbeitsbereich
- interne FastAPI-Verbindung
- Desktop- und Mobil-Darstellung
- lokalisierte Mandanten- und Rollenanzeigen ohne interne IDs
- Darstellung von Evaluations- und Audit-Daten
- Container-Healthchecks und reproduzierbarer Image-Inhalt

## Ausgeführte Prüfungen

- `make verify`
  - Ergebnis: bestanden
  - Tests: 46 bestanden
  - Kerncode-Abdeckung: 96,39 Prozent (`evidence/coverage.xml`)
  - Ruff, MyPy, Bandit, pip-audit, SBOM, Secret Scan, Evaluation und
    Container-Konfigurationsprüfungen: bestanden
- `make build`
  - Ergebnis: mit dem isolierten klassischen Docker-Builder bestanden
  - Image: `ragops-enterprise-copilot:local`
- `docker-compose --profile demo up -d --no-build --force-recreate`
  - Ergebnis: API, Dashboard, PostgreSQL und Qdrant gestartet
- Headless-Chrome-Desktop-Rendering mit 1440 x 1000 Pixeln
  - Artefakt: `evidence/dashboard-desktop.png`
- Headless-Chrome-Mobil-Rendering mit 390 x 844 Pixeln
  - Artefakt: `evidence/dashboard-mobile.png`
- `POST /v1/query` gegen den neu gebauten API-Container
  - Ergebnis: deutsche CRM- und Vertragsantwort mit drei belegten Aussagen
  - Artefakt: `evidence/german-response-validation.json`

## Funktionaler Nachweis

Die Standardansicht zeigt deutsche Navigation und Statusmeldungen, lesbare
Mandanten- und Rollenwerte, aktive Mandanten- und Quellenkontrollen,
Fragevorschläge, Assistentenbegrüßung, Chat-Eingabe und API-Status. Technische
Begriffe wie RAG, LLM und Retrieval bleiben absichtlich unverändert. Die
Desktop- und Mobilbilder wurden auf Überlagerungen, Umbruch und Kontrast der
Auswahlfelder geprüft.

Das Query-Verhalten ist durch API-Integrationstests und Unit-Tests des
Dashboard-HTTP-Clients abgedeckt. Antworten gelangen ausschließlich über den
FastAPI-Vertrag in den Browser. Ein Regressionstest stellt sicher, dass
Benutzernachrichten als `user` und unbekannte Rollen sicher als `assistant`
dargestellt werden.

## Einschränkung

Die visuelle Prüfung verwendet das lokal installierte Headless Chrome anstelle
einer zusätzlichen Playwright-Abhängigkeit. Für die Screenshot-Erzeugung wurde
keine neue Produktionsabhängigkeit eingeführt.
