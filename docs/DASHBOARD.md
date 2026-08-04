# Copilot-Arbeitsoberfläche

## Zweck

**apps/dashboard/dashboard.py** implementiert den nutzerseitigen Streamlit-Arbeitsbereich.
Die erste Ansicht ist der Copilot-Chat. Monitoring und Governance unterstützen den
Betrieb, stehen aber nicht vor der eigentlichen Arbeitsoberfläche.

Der Arbeitsbereich enthält vier Ansichten:

| Ansicht | Zweck | Primäre API-Daten |
| --- | --- | --- |
| Copilot | Kontrollierte Fragen stellen und belegte Antworten prüfen | POST /v1/query |
| Wissensbasis | Für den Mandanten sichtbare Dokumentversionen prüfen und den Index aktualisieren | /v1/documents |
| Monitoring | Live-Telemetrie und gemessene Evaluationsergebnisse prüfen | /metrics, /v1/evaluations, /v1/costs/summary |
| Governance & Audit | Aktive Kontrollen und revisionsfähige Ereignisse prüfen | /metrics, /v1/audit-events |

## Oberflächensprache

Alle sichtbaren Navigationspunkte, Statusmeldungen, Metriken, Aktionen und
Chat-Hinweise sind deutschsprachig. Interne Rollen- und Mandantenwerte wie
`sales` oder `tenant-alpha` werden nicht in der Oberfläche angezeigt. Etablierte
Fachbegriffe wie RAG, LLM, Retrieval, Recall und Precision bleiben erhalten, um
ihre technische Bedeutung nicht zu verfälschen.

## Antwortsprache und Quellenbindung

Der lokale `DeterministicTestProvider` formuliert seine Antworten auf Deutsch.
Er wählt für die Anfrage relevante Zitate aus und verwendet kontrollierte, an
die synthetischen Quellentitel gebundene Zusammenfassungen. Englische Roh-Chunks
werden nicht in die Antwort übernommen. CRM-Treffer werden bereits im Workflow
als deutsche, strukturierte Evidenz erzeugt.

Jede Faktenzeile endet weiterhin mit der unveränderten Quellen-ID. Damit bleiben
Auditierbarkeit, Zitatvalidierung und die Gold-Datensatz-Auswertung stabil. Wird
der synthetische Dokumentbestand fachlich geändert, müssen die deterministischen
Zusammenfassungen und ihre Regressionstests gemeinsam aktualisiert werden.

## Chat-Ablauf

1. Der Benutzer wählt einen synthetischen Mandanten und eine Rolle.
2. Das Dashboard sendet Frage, Mandant, Rolle, Benutzer-ID und Top-K an
   `POST /v1/query`.
3. Die API führt Mandantenschutz, Routing, Retrieval, CRM-Abfrage,
   Compliance-Prüfungen, Antwortkomposition, Zitatvalidierung, Metrikerfassung
   und Audit-Protokollierung aus.
4. Das Dashboard zeigt die Antwort oder eine kontrollierte Ablehnung an.
5. Quellen werden unter der Antwort mit Quellen-ID und gemessener Relevanz
   zusammengefasst.
6. Evidenzwert, Retrieval-Latenz, LLM-Latenz, Token-Anzahl und Injection-Status
   bleiben der jeweiligen Antwort zugeordnet.

Der Chatverlauf gilt nur für die aktuelle Sitzung und kann mit **Neue
Unterhaltung** gelöscht werden. Anwendungscode persistiert ihn weder
serverseitig noch im Browser-Speicher.

## Identitätsgrenze

Die Auswahl von Mandant und Rolle macht das Autorisierungsverhalten ohne
externen Identity Provider demonstrierbar. Sie ersetzt keine produktive
Authentifizierung. Produktive Bereitstellungen müssen:

- Mandanten- und Rollen-Claims von einem vertrauenswürdigen OIDC/OAuth2-Anbieter beziehen;
- Aussteller, Zielgruppe, Signatur, Ablaufzeit und Autorisierungsrichtlinie validieren;
- vom Client übermittelte Änderungen an Mandant oder Rolle ignorieren;
- den bestehenden Mandantenfilter im Retrieval als zusätzliche Schutzschicht beibehalten.

## Sicherheitseigenschaften

- Das Dashboard verbindet sich ausschließlich mit der konfigurierten `RAGOPS_API_URL`.
- Docker Compose setzt die interne URL auf `http://api:8000`.
- Provider-Schlüssel und Dokumentinhalte gelangen nicht in den Browser.
- Dynamische Quellentitel und Quellen-IDs werden vor benutzerdefiniertem HTML maskiert.
- API-Fehler werden in begrenzte, benutzersichere Meldungen umgewandelt.
- Faktenantworten zeigen ihre Quellen; kontrollierte Ablehnungen bleiben sichtbar.
- Audit-Daten stammen aus strukturierten, bereits redigierten API-Ereignissen.
- Chatrollen werden für die Darstellung strikt auf `user` und `assistant` begrenzt.

## Konfiguration

Dashboard direkt auf dem Host starten:

~~~bash
RAGOPS_API_URL=http://localhost:8000 streamlit run apps/dashboard/dashboard.py
~~~

Docker Compose setzt `RAGOPS_API_URL=http://api:8000` für den Dashboard-Dienst.

## Verifikation

Automatisierte Abdeckung:

- **tests/unit/test_dashboard.py** prüft Metrik-Parsing, Formatierung,
  API-Serialisierung, sichere HTTP-Fehler, lokalisierte Anzeigenamen und
  begrenzte Chatrollen.
- **tests/integration/test_api.py** prüft Query-, OpenAPI-, Evaluations-, Audit-,
  Kosten- und Dokumentverträge.
- **make verify** führt Linting, Tests, Security-, Dependency-, Evaluations- und
  Container-Konfigurationsprüfungen aus.

Visuelle Nachweise:

- **evidence/dashboard-desktop.png**: Headless-Chrome-Rendering mit 1440 x 1000 Pixeln.
- **evidence/dashboard-mobile.png**: Headless-Chrome-Rendering mit 390 x 844 Pixeln.
- **evidence/dashboard-validation.md**: ausgeführte Befehle und beobachtete Ergebnisse.
- **evidence/german-response-validation.json**: reale Antwort des laufenden API-Containers auf die CRM- und Vertragsfrage.
- **evidence/knowledge-base-validation.json**: Datenbank-, Compliance- und No-Evidence-Prüfung gegen den laufenden API-Container.

## Bekannter Umfang

Der Arbeitsbereich ist eine Einzelbenutzer-Portfolio-Demo. Sitzungsübergreifende
Verläufe, gespeicherte Suchen, SSO, verteilte Metriken und Multi-Replica-Zustand
benötigen produktive Adapter. Der lokale deterministische Provider weist bewusst
Modellkosten von null aus.
