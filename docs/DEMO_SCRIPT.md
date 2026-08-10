# Drei-Minuten-Demo

Dieses Skript entspricht der automatisierten Timeline in
`video/script/timeline.json`. Alle gezeigten Daten sind synthetisch.

## 0:00-0:20 - Produkt und Arbeitsbereich

1. RAGOps Enterprise Copilot als abgesicherte Enterprise-RAG-Referenz einordnen.
2. Deutsche Bereiche Copilot, Wissensbasis, Monitoring und Governance zeigen.
3. Sichtbaren Demo-Mandanten, Rolle, Tenant Guard und Quellenpflicht benennen.

## 0:20-0:40 - Wissensbasis

1. Mandantengefilterte Dokumente und Versionen öffnen.
2. Unterstützte Formate PDF, DOCX, Markdown, TXT und CSV nennen.
3. Metadaten, Hashing, Gültigkeit und inkrementelle Indexierung hervorheben.

## 0:40-1:10 - Belegte Antwort

1. Vertragsrisiko-Frage für kritische Supportfälle und bald endende Verträge stellen.
2. Reale CRM- und Dokument-Evidenz in der deutschen Antwort zeigen.
3. Quellen, Evidenzwert, Retrieval- und LLM-Latenz sowie Token öffnen.
4. Erklären, dass der Citation Validator unbelegte Tatsachen ablehnt.

## 1:10-1:30 - Security

1. Eine Systemprompt-Exfiltrationsanweisung stellen.
2. Kontrollierte Verweigerung und Prompt-Injection-Erkennung zeigen.
3. Klarstellen, dass Dokumentanweisungen nicht als Systembefehle gelten.

## 1:30-2:05 - Monitoring und Governance

1. Gold-Evaluation, Retrieval Hit Rate, Recall, Precision und Citation Coverage zeigen.
2. Tenant Leakage, Abstention und deterministische Modellkosten einordnen.
3. Governance & Audit mit Rollenprüfung, PII-Maskierung und Korrelations-ID öffnen.

## 2:05-2:30 - Engineering-Nachweis

1. Persistente Master-Loop-Phasen, Retry-Limits und Wiederaufnahme zeigen.
2. Statische Analyse, Tests, Security, Aufnahme, Rendering und Video-QA nennen.
3. Mit Applied AI Engineering, Governance und reproduzierbarer Automation schließen.

## Manuelle Live-Demo

```bash
make build
make up
```

Danach `http://localhost:8501` öffnen. Die automatisierte Aufnahme verwendet
stattdessen lokale Ports 8765 und 8766 und beendet beide Prozesse nach der
letzten Szene.

## Automatisierte Videoproduktion

```bash
./run_loop.sh --dry-run
./run_loop.sh
```

Ohne `OPENAI_API_KEY` entsteht nur die klar benannte stumme Vorschau.
Der finale Build wird in diesem Zustand nicht als vollständig ausgegeben.
