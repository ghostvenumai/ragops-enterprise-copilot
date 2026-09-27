# Abschlussstand der begonnenen Produktisierung

Version: **0.2.0.dev0**. Kein Release Candidate, keine v1.0-Freigabe.
Das vollständige Master-Briefing ist noch nicht umgesetzt.

## Umgesetzt

- Phase 0: Audit des vorhandenen Systems und erweiterte Aufgabenplanung ENT-00 bis ENT-12.
- Sicherheitskorrekturen: Admins bleiben im ausgewählten Mandanten; unbekannte
  Provider/Umgebungen werden abgelehnt; Dateinamenprüfung wurde verschärft.
- Der Produktionsmodus startet die ungesicherte Demo-API nicht mehr.
- Persistenzgrundlage mit 19 SQLAlchemy-Modellen, zusammengesetzten
  Mandantenschlüsseln, Alembic-Revision, Repository und Transaktionsgrenzen.
  Die SQLite-Migrationstests und ein echter Alembic-Upgrade-/Drift-Lauf bestehen;
  PostgreSQL-Integration ist noch nicht ausgeführt und die API bleibt entkoppelt.
- Migrationstests für SQLite-Verträge und eine leere PostgreSQL-Testdatenbank sind
  vorbereitet; entsprechende CI-Aufgabe sowie make migrate und make migration-check.
- make product-verify mit fehlgeschlagenen Gates bei fehlenden Prüfungen,
  begrenzten Laufzeiten und aktuellen Ergebnissen. Der bisher pauschal erfolgreiche
  Reviewer wurde durch begrenzte ausführbare Prüfungen ersetzt.

## Prüfungen

- Breiter lokaler Lauf: 159 Tests, davon 156 bestanden und 3 fehlgeschlagen.
  Die drei Fehler entstehen bei Socket-Erzeugung (PermissionError).
- Abdeckung für Core, Loop, Automation und Video: 63,23 %. Die unveränderte
  Mindestgrenze von 80 % wird nicht erreicht. Historische 96,02 % hatten einen
  engeren Messumfang und sind nicht direkt vergleichbar.
- Abschließende gezielte Sicherheits-/Gate-Prüfung: 23 bestanden. Diese Tests
  überschneiden sich mit dem breiten Lauf; die Zahlen sind nicht zu addieren.
- Lint und statische Sicherheitsprüfungen bestanden. Der Typecheck der geänderten
  Persistenz- und Prüfscripte besteht nach Installation der Persistenzabhängigkeiten.
- make verify bleibt fehlgeschlagen: pytest-Zeitlimit nach 120 Sekunden,
  Socket-Fehler und fehlgeschlagene Audit-/SBOM-Auflösung.
- API-Diagnose läuft ebenfalls ins Zeitlimit. Der Stack zeigt den Thread-/Eventloop-
  Übergang von TestClient; die genaue Ursache bleibt in dieser Sandbox ungeklärt.
- Migrationen, PostgreSQL-Laufzeit und Neustartpersistenz: NOT_EXECUTED.
- Abhängigkeitsaudit konnte nicht abgeschlossen werden. Es gibt keinen aktuellen
  Befund „keine bekannten Schwachstellen“ für die neuen Abhängigkeiten.

## RAG-Evaluation

28 synthetische Fälle mit deterministischem Provider: bestanden.
Recall@5 1,0; Precision@5 0,7738; Retrieval-Hitrate 1,0;
Citation Coverage 1,0; Mandantenlecks 0. Keine Live-Provider- oder
Produktionslastmessung. Details: rag-evaluation.json.

## Offene Phasen und Blocker

Die zuvor fehlende Persistenzinstallation wurde in der Projektumgebung nachgeholt.
Der Zugriff auf den Docker-Daemon wird verweigert. Es wurde keine Sandbox-
Eskalation für die Installation war erforderlich; die zusätzliche Abdeckungslücke
bleibt offen.

Verifizierte Identität, produktive API-Persistenz, Worker/Redis, Qdrant-Integration,
Model-Router, FinOps, Backup/Restore, Retention, Produktionsoberfläche,
Produktions-Compose und vollständige Release-/Adversarial-Gates sind noch offen.
OpenAI und Azure haben weiterhin keine implementierte Generierung.
Phase ENT-01 darf erst nach realen Migrations- und Integrationsprüfungen abgeschlossen werden.

## Exakte Befehle

Im Repository /home/serverserver/tools/ragops-enterprise-copilot:

Demo Mode, ausgeführt und erfolgreich:

```bash
RAGOPS_ENV=demo RAGOPS_LLM_PROVIDER=deterministic make demo
```

Es gibt derzeit keinen freigegebenen Production-Mode-Startbefehl.
Der geplante API-Aufruf wird absichtlich durch die Anwendungsfabrik blockiert:

```bash
RAGOPS_ENV=production .venv/bin/python -m uvicorn apps.api.main:app --host 127.0.0.1 --port 8000
```

Die Startsperre wurde direkt an create_app geprüft und bestand. In der aktuellen
Sandbox kann bereits die Eventloop-Initialisierung des Serverbefehls scheitern.
Ein ausführbarer Produktionsstack wird damit ausdrücklich nicht behauptet.

Freigabeprüfung:

```bash
make product-verify
```

Ergebnis: BLOCKED. Es wurde nichts veröffentlicht, getaggt oder produktiv deployt.

## Phase 2 update (2026-09-13)

Die IdentityProvider-Abstraktion, deterministische Entwicklungsidentität,
OIDC/JWT-Signatur-/Issuer-/Audience-/Expiry-/Claim-Prüfung, unveränderlicher
AuthenticatedUserContext sowie FastAPI-Authentifizierungs- und Admin-Dependencies
sind implementiert. OIDC ignoriert `user_id`, `tenant_id` und `role` aus dem
Request-Body; Entwicklung/Demo behält die ausdrücklich begrenzte Kompatibilität.

Phase-2-Evidence: 17 Authentifizierungstests, 19 Autorisierungs-/Persistenztests,
Konfigurationsprüfung, Ruff und MyPy bestanden. Live-OIDC-Keyrotation, externer
OIDC-Provider und Produktions-PostgreSQL sind NOT_EXECUTED. Phase 2 ist daher für
den lokal testbaren Umfang bestanden; die Produktionsfreigabe bleibt blockiert.
