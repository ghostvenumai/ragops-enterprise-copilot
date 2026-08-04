# Wissensbasis

## Zweck und Umfang

Die lokale Wissensbasis enthält ausschließlich synthetische Portfolio-Daten. Sie
besteht aktuell aus 20 Dokumentdateien für drei Mandanten und wird beim Start der
API reproduzierbar eingelesen. Der deterministische Standardmodus benötigt dafür
weder einen externen LLM-Anbieter noch eine externe Datenbank.

Die neun am 4. August 2026 ergänzten Dokumente sind:

| Mandant | Dokument | Klassifikation | Zugriff | Abgedecktes Thema |
| --- | --- | --- | --- | --- |
| tenant-alpha | Alpha Plattformarchitektur v1 | architecture | internal | Dateibasiertes Repository, PostgreSQL- und Qdrant-Zieladapter |
| tenant-alpha | Alpha Support-Betriebshandbuch v2 | support | confidential | Eskalation und tägliche Statuspflege |
| tenant-alpha | Alpha CRM-Datenschutzleitfaden v1 | policy | confidential | Mandantenfilter, erlaubte Felder und PII-Maskierung |
| tenant-alpha | Alpha Vertragslebenszyklus-Richtlinie v1 | contract | confidential | Vertragsrisiko innerhalb von 60 Tagen |
| tenant-beta | Beta Kundenservice-Handbuch v1 | support | confidential | Regionale Vorfallkoordination |
| tenant-beta | Beta Preisliste v1 | pricing | confidential | Synthetische Listenpreise |
| tenant-beta | Beta Datenschutzrichtlinie v1 | policy | restricted | Rollenfreigabe und Mandantentrennung |
| tenant-gamma | Gamma Nexus Produktleitfaden v1 | product | internal | Produktfunktion und Verfügbarkeitsziel |
| tenant-gamma | Gamma Preisliste v1 | pricing | confidential | Synthetische Listenpreise |

Bestehende Produktleitfäden, Renewal-Leitfäden, Compliance-Dokumente,
Prompt-Injection-Testquellen und Preislisten bleiben erhalten. CRM-Kunden,
Verträge und Supportfälle liegen getrennt unter `data/synthetic/crm/`.

## Retrieval-Verhalten

`HybridRetriever` verarbeitet normalisierte Unicode-Tokens und trennt deutsche
Bindestrich-Komposita. Deutsche und englische Stoppwörter werden vor BM25- und
Vektorbewertung entfernt. Für die Suche werden Dokumenttitel, Klassifikation und
Chunk-Text gemeinsam indexiert.

Ein Ergebnis benötigt mindestens einen echten lexikalischen Treffer. Eine reine
Kollision im deterministischen Hash-Vektor reicht nicht mehr aus. Dadurch gilt:

- „Welche Datenbank wird genutzt?“ trifft die Plattformarchitektur.
- „Welche Compliance-Richtlinien gelten für Kundendaten?“ kombiniert CRM- und
  Governance-Evidenz.
- Ein nicht dokumentiertes Thema wie die Büro-Kaffeemaschine wird kontrolliert
  abgelehnt.
- Ein kritischer Supportfall aktiviert den CRM-Agenten nur mit zusätzlichem
  Vertrags- oder Verlängerungsbezug.

## Antwort- und Quellenbindung

Der deterministische Provider besitzt für jedes vertrauenswürdige synthetische
Dokument eine geprüfte deutsche Zusammenfassung. Die Antwort meldet die von ihr
tatsächlich verwendeten Quellen-IDs zurück. Der Workflow entfernt weitere
Retrieval-Kandidaten vor API-Ausgabe, Evidenzscore und Quellenanzeige.

Unbekannte Dokumenttitel werden nicht als Roh-Chunk ausgegeben. Ohne relevante
Evidenz liefert der Provider eine kontrollierte Ablehnung. Dokumente mit
Prompt-Injection-Markierung werden bereits vor der Komposition entfernt.

## Dokument hinzufügen

1. Datei unter `data/synthetic/documents/` in einem erlaubten Format anlegen.
2. Den Inhalt ausdrücklich als synthetisch kennzeichnen.
3. Eindeutige `document_id`, `tenant_id`, `title`, `classification`,
   `access_level`, `version`, `department`, `created_at` und `valid_from` setzen.
4. Eine quellengetreue deutsche Zusammenfassung in
   `DeterministicTestProvider._GERMAN_SUMMARIES` ergänzen.
5. Mindestens einen Gold-Fall oder fokussierten Retrieval-Test hinzufügen.
6. `make verify` ausführen und Retrieval-Metriken vergleichen.
7. Docker-Image neu bauen und den kontrollierten Index aktualisieren.

Echte Kunden-, Kontakt-, Vertrags- oder Zugangsdaten sind unzulässig.

## Verifikation

Der Gold-Datensatz umfasst 28 Fragen. Er enthält zusätzlich einen Architekturfall,
einen neuen Supportfall und einen No-Evidence-Fall. Die Regressionstests prüfen:

- Abdeckung aller vertrauenswürdigen Dokumenttitel durch deutsche Zusammenfassungen;
- Unicode- und Bindestrich-Tokenisierung;
- Datenbank-Retrieval und kontrollierte Ablehnung ohne Evidenz;
- Trennung von Support- und CRM-Vertragsintent;
- Ausgabe ausschließlich tatsächlich verwendeter Quellen.

Der aktuelle gemessene Lauf erreicht bei 28 Fällen 100 Prozent Retrieval Hit
Rate, Recall@5 und Citation Coverage, 77,38 Prozent Precision@5 sowie null
Mandantenlecks. Maschinenlesbare Ergebnisse stehen in
`evidence/rag-evaluation.json`. Die drei gegen den laufenden API-Container
geprüften Verhaltensfälle stehen in `evidence/knowledge-base-validation.json`.
Beide Nachweise beruhen auf tatsächlich ausgeführten Anfragen.
