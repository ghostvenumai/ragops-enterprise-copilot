# Copilot-Arbeitsoberfläche

## Zweck

**apps/dashboard/dashboard.py** implementiert den nutzerseitigen Streamlit-Arbeitsbereich.
Die erste Ansicht ist der Copilot-Chat. Monitoring und Governance unterstützen den
Betrieb, stehen aber nicht vor der eigentlichen Arbeitsoberfläche.

Der Arbeitsbereich enthält sechs Ansichten (Copilot, Wissensbasis, Monitoring,
FinOps, Governance & Audit, System / Operations); die wichtigsten:

| Ansicht | Zweck | Primäre API-Daten |
| --- | --- | --- |
| Copilot | Kontrollierte Fragen stellen und belegte Antworten prüfen | POST /v1/query |
| Wissensbasis | Dokumente hochladen, Verarbeitungsaufträge verfolgen und wiederholen, Demo-Korpus prüfen | /v1/documents/upload, /v1/ingestion/jobs, /v1/documents |
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

1. Mit OIDC ergeben sich Mandant und Rolle aus dem Token; im Entwicklungsmodus
   wählt der Benutzer einen synthetischen Mandanten und eine Rolle.
2. Das Dashboard sendet Frage und Top-K (im Entwicklungsmodus zusätzlich Mandant,
   Rolle und Benutzer-ID) an `POST /v1/query`, mit OIDC samt Bearer-Token.
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

## Anmeldung und Identität

Mit `RAGOPS_IDENTITY_PROVIDER=oidc` meldet das Dashboard Benutzer über den
OIDC-Anbieter an (Streamlit `st.login`, Authorization Code mit PKCE). Vor der
Anmeldung zeigt es nur die Seite **Anmeldung erforderlich**; ist der Anbieter nicht
erreichbar, erscheint ein verständlicher Hinweis statt einer Weiterleitung.

- Das Access-Token bleibt serverseitig im signierten, HttpOnly-Sitzungscookie und
  wird nur als Bearer-Token an die interne `RAGOPS_API_URL` gesendet.
- Mandant und Rollen stammen ausschließlich aus dem lokal verifizierten Token
  (dieselben `RAGOPS_OIDC_*`-Einstellungen wie die API); die Auswahlfelder für
  Mandant und Rolle existieren nur im Entwicklungsmodus.
- Ein abgelaufenes oder abgelehntes Token führt zur Seite **Sitzung abgelaufen**
  ohne Daten; **Erneut anmelden** führt über den Anbieter zurück. Ein stiller
  Refresh-Token-Ablauf ist nicht implementiert.
- **Abmelden** löscht die Sitzungscookies und beendet die Anbietersitzung über den
  `end_session_endpoint`.

Die OIDC-Konfiguration liegt außerhalb des Repositorys in einer Streamlit-Secrets-Datei
(`--secrets.files=<pfad>`):

~~~toml
[auth]
redirect_uri = "https://copilot.example/oauth2callback"
cookie_secret = "<zufälliger geheimer Wert>"
client_id = "<vertraulicher Dashboard-Client>"
client_secret = "<Client-Secret>"
server_metadata_url = "https://idp.example/realms/<realm>/.well-known/openid-configuration"
expose_tokens = ["access"]
~~~

Der Dashboard-Client braucht einen Audience-Mapper auf `ragops-api` und einen
`tenant_id`-Claim; Rollen kommen aus `resource_access.ragops-api.roles`.

Im Entwicklungsmodus bleibt die Auswahl von Mandant und Rolle für Demos erhalten;
sie ersetzt keine Authentifizierung und die API ignoriert sie außerhalb von
Entwicklungsumgebungen.

## Wissensbasis: Upload und Verarbeitung

Die Ansicht **Wissensbasis** lädt Dateien (PDF, DOCX, Markdown, Text, CSV, maximal
2 MB) über `POST /v1/documents/upload` hoch und zeigt die Verarbeitungsaufträge des
eigenen Mandanten mit Titel, Datei und Status als Text (**In Warteschlange**,
**In Verarbeitung**, **Bereit**, **Fehlgeschlagen**). Solange Aufträge laufen,
aktualisiert sich die Liste alle drei Sekunden; **Erneut versuchen** startet einen
fehlgeschlagenen Auftrag neu, sofern die Rolle es erlaubt. Doppelte Übertragungen
derselben Datei erzeugen keinen zweiten Auftrag. Abgelehnte Dateien (Typ, leer,
beschädigt, Duplikat) werden mit einer konkreten Meldung erklärt.

Der Copilot beantwortet Fragen weiterhin aus dem kontrollierten Demo-Korpus
(**Abfragebasis (Demo-Korpus)**). Hochgeladene Dokumente werden vom Worker
indexiert, fließen aber nicht in Antworten ein.

## Fehlerdarstellung und Barrierefreiheit

API-Fehler werden in handlungsleitende Meldungen ohne technische Details übersetzt:
fehlende Berechtigung (403), zu viele Anfragen mit Wartezeit aus `Retry-After`
(429), vorübergehend nicht verfügbarer Dienst (503) und eingeschränkte Readiness in
der Seitenleiste. Seitentitel sind Überschriften, der Tastaturfokus ist sichtbar
markiert, und ein kleines Skript behebt zwei Markup-Mängel von Streamlit 1.60
(`aria-expanded` an der Seitenleiste, unbenanntes Datei-Eingabefeld).

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

Dashboard direkt auf dem Host starten (Entwicklungsmodus):

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
- **scripts/browser_e2e_gate.py** (`RAGOPS_RC_GATES=browser_e2e make rc-live-gate`)
  steuert ein echtes Headless-Chromium durch Keycloak-Anmeldung, Rollen, Mandanten,
  Upload, Ausfall und Wiederherstellung, Tastatur, Mobilansicht und axe-Prüfung.
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
