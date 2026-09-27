# RAGOps Enterprise Copilot

> **Produktisierungsstand: 0.2.0.dev0 — kein Release Candidate.**
> Phase 0 ist dokumentiert; die Persistenzgrundlage ist vorbereitet, aber noch
> nicht ausführungsgeprüft oder an die API angeschlossen. Produktionsstart ist
> gesperrt. OpenAI/Azure-Generierung ist bisher nicht implementiert.
> Audit, Blocker und nächste Schritte: [Produktisierungsaudit](docs/PRODUCTIZATION_AUDIT.md),
> [Datenbank](docs/DATABASE.md), [Produktionsstatus](docs/DEPLOYMENT_PRODUCTION.md).
> `make product-verify` liefert einen fehlgeschlagenen Freigabestatus, solange
> Pflichtprüfungen fehlen oder nicht ausgeführt werden können.


Die ausführliche deutschsprachige Projektbeschreibung befindet sich direkt in
der GitHub-Startseite [README.md](README.md). Sie erklärt das fachliche Szenario,
die RAG- und Multi-Agent-Architektur, Sicherheitskontrollen, Docker-Betrieb,
Evaluation sowie den kontrollierten Codex-Entwicklungsloop.

Direkte Einstiege:

- [Schnellstart](README.md#schnellstart)
- [Entwicklung mit kontrolliertem Codex-Loop](README.md#entwicklung-mit-kontrolliertem-codex-loop)
- [Security und Governance](README.md#security-und-governance)
- [Quality Gates und gemessener Stand](README.md#quality-gates-und-gemessener-stand)
- [Narration Quality Gate vor TTS](docs/NARRATION_QA.md)
- [Sicherer TTS-Cache und Kostenkontrolle](docs/TTS_CACHE.md)
- [Zentrale Dokumentation](README.md#zentrale-dokumentation)
