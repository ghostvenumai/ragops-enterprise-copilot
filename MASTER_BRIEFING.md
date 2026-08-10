# MASTER BRIEFING – AUTONOMOUS PYTHON DATA PROCESSOR + DEMO VIDEO PRODUCTION LOOP

## MISSION

Arbeite selbstständig am bestehenden Python-Data-Processor-Projekt.

Das Ziel besteht aus zwei miteinander verbundenen Systemen:

1. einem professionellen, stabilen Python Data Processor mit moderner GUI und reproduzierbarem Demo-Modus,
2. einem autonomen Entwicklungs-, Test- und Video-Production-Loop, der das Projekt prüft, verbessert und anschließend möglichst automatisch ein professionelles Demonstrationsvideo erzeugt.

Das Endergebnis soll für die Präsentation des Projekts gegenüber einem potenziellen Auftraggeber wie SOLCOM geeignet sein.

Der Benutzer soll möglichst wenig manuell durchführen müssen.

Das gewünschte Bedienprinzip lautet:

```text
ONE COMMAND
     ↓
PRECHECK
     ↓
BUILD / VERIFY APPLICATION
     ↓
TEST
     ↓
DEMO
     ↓
RECORD
     ↓
NARRATION
     ↓
AI VOICE
     ↓
SUBTITLES
     ↓
VIDEO RENDER
     ↓
VIDEO QA
     ↓
FINAL OUTPUT
```

Der gesamte Workflow soll reproduzierbar, nachvollziehbar und sicher sein.

---

# 1. OBERSTE ARBEITSANWEISUNG

Arbeite nicht nach dem Muster:

```text
Ich habe einige Dateien vorbereitet.
Bitte führe jetzt Schritt X selbst aus.
```

Stattdessen sollst du innerhalb der verfügbaren Sandbox und Berechtigungen selbst:

1. Repository analysieren.
2. bestehende Architektur verstehen.
3. vorhandene Funktionen identifizieren.
4. vorhandene Tests analysieren.
5. fehlende Komponenten planen.
6. Komponenten implementieren.
7. Tests schreiben.
8. Tests ausführen.
9. Fehler analysieren.
10. Fehler korrigieren.
11. erneut testen.
12. Demo-Pipeline implementieren.
13. Demo-Pipeline ausführen.
14. Video-Pipeline implementieren.
15. Video-Pipeline testen.
16. Quality Gates ausführen.
17. Fehler korrigieren.
18. Ergebnisse dokumentieren.

Frage den Benutzer nicht bei normalen technischen Entscheidungen.

Treffe vernünftige Entscheidungen selbstständig.

Nur echte externe Blocker dürfen einen Arbeitsschritt verhindern.

Beispiele echter externer Blocker:

- fehlender API-Key,
- ein nicht installiertes systemweites Programm, das innerhalb der Sandbox nicht installiert werden darf,
- fehlende OS-Berechtigung,
- nicht erreichbarer externer Service,
- nicht vorhandene Hardware-/Display-Funktion.

Ein externer Blocker darf NICHT dazu führen, dass alle anderen möglichen Arbeiten eingestellt werden.

Implementiere und teste immer alles, was ohne diesen Blocker möglich ist.

---

# 2. REPOSITORY DISCOVERY

Beginne mit einer gründlichen, aber zielgerichteten Analyse des vorhandenen Repositorys.

Identifiziere:

- Programmiersprache und Python-Version,
- Package-/Dependency-Management,
- GUI-Framework,
- Entry Points,
- Datenverarbeitung,
- Importformate,
- Exportformate,
- Validierung,
- Logging,
- Fehlerbehandlung,
- Tests,
- vorhandene Demo-Daten,
- Konfigurationssystem,
- Build-Skripte,
- bestehende AGENTS.md,
- bestehende README,
- Sicherheitsmechanismen.

Verändere keine Architektur blind.

Bestehende funktionierende Komponenten sollen bevorzugt weiterverwendet werden.

Keine unnötigen Framework-Wechsel.

Keine komplette Neuentwicklung, wenn eine bestehende Komponente sauber erweitert werden kann.

---

# 3. AUTONOMER MASTER LOOP

Implementiere eine zentrale, explizite Orchestrierung.

Bevorzuge beispielsweise:

```text
automation/
├── run_loop.py
├── state.py
├── gates.py
├── retry.py
├── diagnostics.py
└── reports.py
```

plus:

```text
run_loop.sh
```

Der Benutzer soll später den gesamten Workflow möglichst mit folgendem Befehl starten können:

```bash
./run_loop.sh
```

---

# 4. STATE MACHINE

Der Loop soll als nachvollziehbare State Machine implementiert werden.

Mindestens folgende States:

```text
DISCOVER
   ↓
PRECHECK
   ↓
PLAN
   ↓
IMPLEMENT
   ↓
STATIC_CHECK
   ↓
UNIT_TEST
   ↓
INTEGRATION_TEST
   ↓
SECURITY_CHECK
   ↓
APPLICATION_QA
   ↓
DEMO_PRECHECK
   ↓
DEMO_RUN
   ↓
RECORD
   ↓
GENERATE_NARRATION
   ↓
GENERATE_VOICE
   ↓
GENERATE_SUBTITLES
   ↓
RENDER
   ↓
VIDEO_QA
   ↓
FINAL_VERIFY
   ↓
COMPLETE
```

Bei Fehlern soll gezielt zur passenden Phase zurückgesprungen werden.

Beispiel:

```text
UNIT_TEST FAIL
      ↓
DIAGNOSE
      ↓
FIX
      ↓
UNIT_TEST
```

Nicht immer den gesamten Workflow erneut starten.

---

# 5. LOOP-SICHERHEIT

Implementiere zwingend Schutz gegen Endlosschleifen.

Definiere:

- maximale Retries pro State,
- maximale globale Iterationen,
- Timeout-Konzept,
- Fehlerkategorien,
- Exit Codes,
- Abbruchbedingungen,
- Success Criteria.

Empfohlener Ausgangspunkt:

```text
max_retries_per_state = 3
max_global_iterations = 30
```

Diese Werte sollen konfigurierbar sein.

Ein Fehler darf niemals eine unbegrenzte Rekursion oder Endlosschleife auslösen.

---

# 6. PERSISTENTER LOOP-STATE

Der Workflow muss seinen Zustand nachvollziehbar speichern.

Beispielsweise:

```text
automation/state/
└── loop_state.json
```

Speichere mindestens:

- aktuelle Phase,
- abgeschlossene Phasen,
- fehlgeschlagene Phasen,
- Retry-Zähler,
- Zeitstempel,
- letzte Fehlermeldung,
- letzte erfolgreiche Aktion,
- Build-ID.

Ein abgebrochener Lauf soll diagnostizierbar bleiben.

Eine Resume-Funktion ist erwünscht, sofern sie robust implementierbar ist.

Beispielsweise:

```bash
./run_loop.sh --resume
```

---

# 7. DRY RUN

Implementiere zwingend:

```bash
./run_loop.sh --dry-run
```

Der Dry Run soll möglichst keine kostenpflichtigen oder externen Aktionen durchführen.

Prüfen:

- Repository-Struktur,
- Python,
- virtuelle Umgebung,
- Dependencies,
- Tests,
- Demo-Daten,
- Demo-State-Machine,
- FFmpeg-Verfügbarkeit,
- Display-/Recording-Möglichkeiten,
- Konfiguration,
- Timeline,
- TTS-Konfiguration,
- Ausgabeverzeichnisse.

Im Dry Run:

- keine kostenpflichtige TTS-Anfrage,
- keine externen Produktionsdaten,
- keine irreversible Aktion.

---

# 8. PYTHON DATA PROCESSOR

Behandle die eigentliche Anwendung als reales Portfolio-Projekt und nicht als reine Videoattrappe.

Stelle sicher, dass die vorhandenen Kernfunktionen stabil funktionieren.

Je nach bereits vorhandener Architektur insbesondere:

- strukturierter Datenimport,
- Schema-/Typvalidierung,
- Datenbereinigung,
- Transformation,
- Fehlererkennung,
- Plausibilitätsprüfungen,
- nachvollziehbares Logging,
- Ergebnisdarstellung,
- Export,
- Fehlerbehandlung.

Keine Feature-Behauptungen implementieren oder im Video nennen, die technisch nicht existieren.

Wenn die vorhandene Anwendung bereits mehr kann, nutze diese Funktionen.

---

# 9. GUI

Die bestehende GUI darf verbessert werden, wenn dies für die Demo notwendig ist.

Ziel:

- modern,
- übersichtlich,
- professionell,
- technisch,
- kein überladenes Design.

Für die Demo besonders wichtig:

- gut lesbare Schrift,
- klare Buttons,
- sichtbarer Workflow,
- Statusanzeige,
- Fortschrittsanzeige,
- Result Cards,
- Validierungsstatus,
- Fehleranzeige,
- Exportstatus.

Bei 1920×1080 muss alles gut lesbar sein.

Keine kritischen Informationen nur durch Hover sichtbar machen.

---

# 10. DEMO MODE

Implementiere einen dedizierten deterministischen Demo-Modus.

Beispielsweise:

```bash
python app.py --demo
```

oder einen zur vorhandenen Architektur passenden Entry Point.

Der Demo-Modus darf NICHT auf zufälligen Mauskoordinaten basieren.

Bevorzuge eine interne Steuerung der Anwendung.

Implementiere beispielsweise:

```text
DemoController
```

oder eine vergleichbare Abstraktion.

Der Controller soll kontrolliert Aktionen triggern können:

```text
START
↓
LOAD SYNTHETIC DATA
↓
SHOW IMPORT
↓
RUN VALIDATION
↓
SHOW VALIDATION RESULT
↓
RUN PROCESSING
↓
SHOW PROCESSING
↓
SHOW RESULTS
↓
SHOW DETECTED ISSUES
↓
SHOW LOG/QUALITY VIEW
↓
EXPORT
↓
SHOW TEST/QUALITY STATUS
↓
END
```

Der Ablauf soll bei jedem Durchlauf reproduzierbar sein.

---

# 11. DEMO-DATEN

Verwende ausschließlich synthetische Demo-Daten.

Niemals:

- persönliche Daten,
- Kundendaten,
- Produktionsdaten,
- echte Zugangsdaten.

Erzeuge einen aussagekräftigen Demo-Datensatz.

Er soll sowohl valide Datensätze als auch einige bewusst eingebaute Probleme enthalten.

Beispiele:

- fehlender Wert,
- falscher Datentyp,
- Duplikat,
- ungültiger Wertebereich,
- inkonsistente Schreibweise.

Die Daten sollen realistisch wirken, aber eindeutig synthetisch sein.

---

# 12. DEMO-TIMELINE

Erstelle:

```text
video/script/timeline.json
```

Die Timeline soll die zentrale Source of Truth für den Videoablauf sein.

Jede Szene enthält nach Möglichkeit:

```json
{
  "id": "validation",
  "order": 3,
  "action": "run_validation",
  "planned_duration": 12,
  "narration": "...",
  "overlay": "Automated Data Validation",
  "pause_before": 0.5,
  "pause_after": 1.0
}
```

Timeline und reale Anwendung müssen synchron bleiben.

Validiere das JSON über Schema oder vergleichbare Prüfungen.

---

# 13. VIDEO-LÄNGE

Ziel:

```text
2 bis 3 Minuten
```

Bevorzugt ungefähr:

```text
2:15 bis 2:45
```

Kein unnötig langes Video.

Der Betrachter soll innerhalb der ersten 15 Sekunden verstehen:

- was das Projekt ist,
- welches Problem es löst,
- dass es sich um ein echtes technisches Projekt handelt.

---

# 14. VIDEO-STRUKTUR

Empfohlene Dramaturgie:

```text
00:00–00:07
Intro

00:07–00:20
Dashboard / Projektüberblick

00:20–00:40
Datenimport

00:40–01:05
Validation

01:05–01:35
Processing

01:35–01:55
Results / Errors

01:55–02:15
Export

02:15–02:35
Testing / Quality / Automation

02:35–02:45
Outro
```

Passe dies an die reale Anwendung an.

---

# 15. RECORDING-ARCHITEKTUR

Implementiere eine möglichst reproduzierbare Linux-kompatible Recording-Lösung.

Bevorzuge keine fragile Desktop-Automation.

Bevorzugte Strategie, sofern mit dem bestehenden GUI-Framework kompatibel:

```text
Virtual Display
     +
Application Demo Mode
     +
FFmpeg Capture
```

Ermittle automatisch, welche Recording-Methode in der vorhandenen Umgebung sinnvoll ist.

Mögliche Komponenten können sein:

- Xvfb,
- X11,
- FFmpeg,
- vorhandene Desktop-Capture-Funktionen.

Aber:

Keine Annahme treffen, dass ein bestimmtes Tool bereits installiert ist.

Prüfe zuerst.

---

# 16. KEIN SUDO IM LOOP

Der automatische Loop darf niemals selbst:

```bash
sudo ...
```

ausführen.

Keine systemweiten Pakete ungefragt installieren.

Wenn ein zwingend benötigtes Systempaket fehlt:

1. erkenne dies sauber,
2. implementiere alle übrigen Komponenten,
3. dokumentiere die fehlende Dependency,
4. liefere einen präzisen einmaligen Installationsbefehl,
5. markiere nur den tatsächlich blockierten Schritt als BLOCKED.

Python-Abhängigkeiten dürfen innerhalb einer projektspezifischen virtuellen Umgebung installiert werden, sofern dies mit dem bestehenden Projektmodell vereinbar ist.

---

# 17. VIDEO-AUFLÖSUNG

Finale Aufnahme:

```text
1920×1080
30 FPS
```

Finales Format:

```text
MP4
H.264
AAC Audio
```

Nutze sinnvolle Encoder-Einstellungen für gute Qualität bei vertretbarer Dateigröße.

---

# 18. NARRATION

Erstelle den Sprechertext automatisch aus:

- tatsächlich implementierten Funktionen,
- README,
- Architektur,
- Demo-Timeline,
- Tests.

Keine erfundenen Fähigkeiten.

Der Text soll wie eine professionelle technische Produktdemo wirken.

Nicht wie Werbung.

Nicht übertreiben.

Vermeide Formulierungen wie:

```text
revolutionary
groundbreaking
world-leading
perfect
unbreakable
```

Stattdessen präzise technische Aussagen.

---

# 19. SPRACHE

Standard:

```text
Deutsch
```

Die Architektur soll eine spätere englische Version ermöglichen.

Beispielsweise:

```text
VIDEO_LANGUAGE=de
```

oder entsprechende Konfiguration.

Narration und Untertitel müssen dieselbe Sprache verwenden.

---

# 20. AI VOICE / TEXT TO SPEECH

Implementiere:

```text
video/generate_voice.py
```

oder eine entsprechend saubere Modulstruktur.

Default-Provider:

```text
OpenAI Text-to-Speech
```

Der Provider muss austauschbar konzipiert sein.

Credentials ausschließlich aus Environment Variables.

Beispielsweise:

```text
OPENAI_API_KEY
```

Niemals API-Keys:

- hardcoden,
- committen,
- loggen,
- in Reports schreiben,
- in Screenshots anzeigen.

Sprechstil:

```text
professionell
ruhig
souverän
technisch
natürlich
gut verständlich
keine Werbestimme
```

Technische Begriffe wie:

```text
Python
JSON
CSV
API
Data Processing
Validation
Pipeline
```

müssen verständlich ausgesprochen werden.

TTS-Modell und Stimme konfigurierbar halten.

---

# 21. FEHLENDER TTS-KEY

Wenn `OPENAI_API_KEY` fehlt:

Nicht den gesamten Auftrag stoppen.

Stattdessen:

- komplette Video-Pipeline weiter implementieren,
- Narration erzeugen,
- Untertitel erzeugen,
- Audio-Schnittstellen testen,
- TTS-Requests mocken,
- Tests abschließen,
- Dry Run abschließen.

Für den finalen TTS-Schritt:

```text
BLOCKED_EXTERNAL_CREDENTIAL
```

melden.

Keinen Key erfinden.

Keine echte Anfrage vortäuschen.

Keinen minderwertigen Voice-Provider stillschweigend als finales Ergebnis verwenden.

---

# 22. AUDIO-SEGMENTE

Erzeuge Voiceover möglichst szenenweise.

Beispielsweise:

```text
video/tmp/audio/
├── 001_intro.wav
├── 002_import.wav
├── 003_validation.wav
├── 004_processing.wav
├── 005_results.wav
├── 006_quality.wav
└── 007_outro.wav
```

Vorteile:

- besser synchronisierbar,
- einzelne Szenen erneut renderbar,
- keine komplette TTS-Neuerzeugung bei kleinen Änderungen.

Implementiere Cache-/Hash-Logik, wenn dies ohne unnötige Komplexität möglich ist.

Unveränderte Narration sollte nicht zwingend neu generiert werden.

---

# 23. UNTERTITEL

Generiere automatisch:

```text
video/tmp/subtitles.srt
```

oder ein vergleichbares Untertitelformat.

Untertitel basieren auf derselben Timeline wie die Narration.

Sicherstellen:

- keine Überlappungen,
- lesbare Segmentlängen,
- vernünftige Zeilenumbrüche,
- korrekte Reihenfolge,
- synchrones Timing.

---

# 24. VIDEO-OVERLAYS

Nutze wenige gezielte technische Einblendungen.

Beispiele:

```text
AUTOMATED DATA VALIDATION
```

```text
DATA CLEANING & TRANSFORMATION
```

```text
ERROR DETECTION
```

```text
AUTOMATED TESTING
```

```text
REPRODUCIBLE PIPELINE
```

Nicht zu viele Animationen.

Das Video soll wie eine Engineering-Demo aussehen und nicht wie eine Social-Media-Werbung.

---

# 25. INTRO

Kurzes Intro.

Maximal ungefähr 5–7 Sekunden.

Beispiel:

```text
PYTHON DATA PROCESSOR

Automated Processing
Validation
Testing
Reporting
```

Professionell und minimalistisch.

---

# 26. OUTRO

Ungefähr 5 Sekunden.

Beispiel:

```text
PYTHON DATA PROCESSOR

Python • Data Engineering • Automation

Demo Project
```

Wenn bereits ein sinnvoller Projektname vorhanden ist, verwende diesen.

---

# 27. FFmpeg RENDERING

Bevorzuge eine CLI-basierte reproduzierbare Video-Pipeline.

FFmpeg soll nach Möglichkeit übernehmen:

- Capture-Verarbeitung,
- Skalierung,
- Framerate,
- Audio-Mixing,
- Audio-Normalisierung,
- Intro,
- Outro,
- Overlays,
- Untertitel,
- Encoding,
- finalen Export.

Kapsle komplexe FFmpeg-Kommandos in nachvollziehbare Python- oder Shell-Abstraktionen.

Keine riesigen unwartbaren Shell-Strings ohne Dokumentation.

---

# 28. BUILD ENTRY POINT

Implementiere:

```bash
./video/build_demo.sh
```

Vollständiger Build:

```bash
./video/build_demo.sh
```

Dry Run:

```bash
./video/build_demo.sh --dry-run
```

Optional:

```bash
./video/build_demo.sh --skip-tts
```

und:

```bash
./video/build_demo.sh --resume
```

nur wenn diese Optionen sauber und sinnvoll implementiert werden können.

---

# 29. MASTER ENTRY POINT

Der komplette Entwicklungs-/Demo-/Video-Workflow soll über:

```bash
./run_loop.sh
```

steuerbar sein.

Das Video-Untermodul bleibt separat nutzbar.

---

# 30. VERZEICHNISSTRUKTUR

Bevorzuge ungefähr:

```text
project/
│
├── app/
├── tests/
├── demo_data/
│
├── automation/
│   ├── run_loop.py
│   ├── state.py
│   ├── gates.py
│   ├── retry.py
│   ├── diagnostics.py
│   ├── reports.py
│   └── state/
│
├── video/
│   ├── build_demo.sh
│   ├── config/
│   ├── script/
│   │   ├── timeline.json
│   │   └── narration.md
│   ├── recording/
│   ├── narration/
│   ├── subtitles/
│   ├── rendering/
│   ├── qa/
│   ├── assets/
│   ├── logs/
│   └── tmp/
│
├── dist/
│   └── solcom_demo.mp4
│
├── run_loop.sh
├── AGENTS.md
└── README.md
```

Passe sie an das vorhandene Repository an.

Keine künstliche Struktur erzwingen, wenn eine vorhandene Struktur besser passt.

---

# 31. AUTOMATISCHE TESTS – APPLICATION

Erhalte alle vorhandenen Tests.

Erweitere die Testabdeckung gezielt.

Mindestens relevant:

- Parser,
- Data Validation,
- Transformation,
- Error Handling,
- Export,
- Demo Controller,
- State Transitions,
- Konfiguration.

Keine Tests löschen oder abschwächen, nur damit der Build grün wird.

---

# 32. AUTOMATISCHE TESTS – LOOP

Schreibe Tests für:

- State Machine,
- erlaubte Übergänge,
- ungültige Übergänge,
- Retry Counter,
- Max Retries,
- globale Iterationsgrenze,
- Resume State,
- Fehlerklassifikation,
- Exit Codes,
- Success Criteria.

---

# 33. AUTOMATISCHE TESTS – VIDEO

Teste mindestens:

- Timeline Parser,
- Timeline Schema,
- Narration Mapping,
- Subtitle Generator,
- Subtitle Timing,
- TTS Configuration,
- fehlende Credentials,
- Audio Asset Mapping,
- Render Configuration,
- FFmpeg Command Construction,
- Video Quality Gates.

Wo reale externe Services benötigt werden:

Mocks verwenden.

Unit Tests dürfen keine kostenpflichtigen TTS-Aufrufe erzeugen.

---

# 34. QUALITY GATES APPLICATION

Vor dem Video muss die Anwendung einen definierten Quality Gate bestehen.

Mindestens:

```text
APPLICATION STARTS
TESTS PASS
DEMO DATA LOADS
VALIDATION WORKS
PROCESSING WORKS
RESULTS AVAILABLE
EXPORT WORKS
NO UNHANDLED EXCEPTION
```

Wenn der Application Gate fehlschlägt:

Kein finales Bewerbungsvideo erzeugen.

Erst Problem beheben.

---

# 35. VIDEO QUALITY GATE

Nach dem Rendering automatisch mit `ffprobe` oder vergleichbaren zuverlässigen Werkzeugen prüfen.

Mindestens:

```text
FILE EXISTS
FILE SIZE > MINIMUM
VIDEO DECODABLE
WIDTH = 1920
HEIGHT = 1080
EXPECTED FPS
AUDIO STREAM EXISTS
VIDEO STREAM EXISTS
DURATION WITHIN EXPECTED RANGE
NO ZERO-LENGTH STREAM
EXPECTED INTRO/OUTRO DURATION
```

Falls Untertitel eingebrannt werden, entsprechende Pipeline ebenfalls validieren.

---

# 36. ERWEITERTE VIDEO-QA

Wenn mit vertretbarem Aufwand möglich:

- Stichproben-Frames extrahieren,
- schwarze/leere Frames erkennen,
- Capture-Ausfall erkennen,
- ungewöhnlich lange statische Bereiche erkennen,
- Audio-Peak/Clipping prüfen,
- zu niedrige Lautstärke erkennen.

Keine komplexe Computer-Vision-Infrastruktur nur für diese Prüfungen hinzufügen, wenn einfache robuste Verfahren ausreichen.

---

# 37. LOGGING

Alle Phasen strukturiert loggen.

Beispielsweise:

```text
logs/
├── master-loop.log
├── application-tests.log
├── demo.log
├── recording.log
├── tts.log
├── render.log
└── video-qa.log
```

Secrets niemals loggen.

---

# 38. BUILD REPORT

Nach jedem vollständigen Lauf erzeugen:

```text
dist/build_report.md
```

Mit mindestens:

```text
Build ID
Date
Application status
Test status
Demo status
Recording status
Narration status
TTS status
Subtitle status
Render status
Video QA status
Final output
Warnings
External blockers
```

---

# 39. TERMINAL SUMMARY

Am Ende kompakte Ausgabe:

```text
================================================
PYTHON DATA PROCESSOR – AUTONOMOUS BUILD REPORT
================================================

APPLICATION:       PASS
UNIT TESTS:        PASS
INTEGRATION TESTS: PASS
SECURITY CHECK:    PASS
DEMO:              PASS
RECORDING:         PASS
NARRATION:         PASS
VOICEOVER:         PASS
SUBTITLES:         PASS
RENDER:            PASS
VIDEO QA:          PASS

FINAL STATUS:      COMPLETE

VIDEO:
dist/solcom_demo.mp4
================================================
```

Bei Blocker entsprechend:

```text
VOICEOVER: BLOCKED – OPENAI_API_KEY missing
```

Keine falschen PASS-Meldungen.

---

# 40. SECURITY REQUIREMENTS

Priorität hoch.

Der Loop darf:

- innerhalb des Repositorys arbeiten,
- definierte temporäre Projektverzeichnisse verwenden,
- benötigte lokale Tests starten,
- Anwendung starten,
- Demo-Daten verarbeiten.

Der Loop darf NICHT:

- fremde Benutzerdateien löschen,
- Home-Verzeichnisse durchsuchen, wenn nicht erforderlich,
- Browser-Credentials lesen,
- SSH-Keys lesen,
- System-Credentials lesen,
- API-Keys aus fremden Dateien extrahieren,
- Produktionssysteme verändern,
- unbekannte externe Programme herunterladen und ausführen,
- Sicherheitsmechanismen abschalten,
- sudo verwenden,
- Sandbox-Grenzen umgehen.

---

# 41. DATEISYSTEM-SICHERHEIT

Alle generierten Dateien bevorzugt unter:

```text
project/
video/tmp/
video/logs/
dist/
```

Löschoperationen nur innerhalb klar definierter temporärer Build-Verzeichnisse.

Vor rekursivem Löschen Pfade validieren.

Keine Operation wie:

```bash
rm -rf "$VARIABLE"
```

ohne robuste Schutzprüfung.

---

# 42. NETWORK SECURITY

Netzwerkzugriff nur verwenden, wenn tatsächlich notwendig.

Beispielsweise:

- TTS-API,
- explizit benötigte Paketinstallation innerhalb zulässiger Umgebung.

Keine unbekannten Dateien automatisch herunterladen und ausführen.

Keine Produktionsdaten an externe Services senden.

An TTS wird ausschließlich der Sprechertext übertragen.

---

# 43. SECRET MANAGEMENT

Implementiere `.env.example`, falls dies zum Projekt passt.

Beispielsweise:

```text
OPENAI_API_KEY=
VIDEO_LANGUAGE=de
```

`.env` muss in `.gitignore`.

Keine Secrets in:

- Git,
- Logs,
- Test Fixtures,
- Screenshots,
- Videos,
- Reports.

---

# 44. AGENTS.md

Erstelle oder erweitere die Repository-`AGENTS.md`.

Sie soll dauerhaft festlegen:

## Development Rules

- bestehende Tests erhalten,
- nach Änderungen Tests ausführen,
- keine Tests zur Fehlerkaschierung deaktivieren,
- Demo-Funktionen müssen reale Produktfunktionen repräsentieren,
- keine Secrets committen.

## Demo Rules

Bei Änderungen an relevanten Features prüfen:

- stimmt Demo Controller noch?
- stimmt Timeline noch?
- stimmt Narration noch?
- stimmen Screens und Feature-Namen noch?
- bestehen Demo Tests?

## Video Rules

Vor finalem Video:

```text
APPLICATION_QA
→ DEMO_QA
→ VIDEO_QA
```

Alle müssen bestehen.

Halte AGENTS.md fokussiert und wartbar.

Lange Detaildokumentation gehört in `docs/` oder `video/README.md`.

---

# 45. DOKUMENTATION

Erstelle mindestens:

```text
README.md
video/README.md
```

Dokumentiere:

- Projektüberblick,
- Setup,
- Start der Anwendung,
- Tests,
- Demo Mode,
- Loop,
- Dry Run,
- Video Build,
- benötigte systemweite Tools,
- API-Key-Konfiguration,
- Output,
- Troubleshooting.

---

# 46. ARCHITEKTURDOKUMENTATION

Erstelle zusätzlich eine kurze:

```text
docs/AUTOMATION_ARCHITECTURE.md
```

Dokumentiere:

```text
Application
↓
Demo Controller
↓
Timeline
↓
Recording
↓
TTS
↓
Subtitles
↓
Renderer
↓
Video QA
```

sowie:

```text
Master Loop
↓
Quality Gates
↓
Retry / Recovery
```

---

# 47. DEPENDENCY MANAGEMENT

Nutze vorhandenes Dependency Management.

Wenn das Projekt bereits beispielsweise:

```text
requirements.txt
pyproject.toml
Poetry
uv
```

nutzt, integriere dich sauber.

Keine parallelen Dependency-Systeme ohne Grund.

Neue Dependencies sparsam hinzufügen.

Bevorzugt:

- etablierte,
- gepflegte,
- notwendige Libraries.

---

# 48. IDEMPOTENZ

Der Build soll möglichst wiederholbar sein.

Ein zweiter Aufruf darf nicht durch Artefakte des ersten Builds kaputtgehen.

Temporäre Dateien kontrolliert bereinigen.

Caches bewusst behandeln.

Finale Outputs versionieren oder gezielt überschreiben.

---

# 49. DETERMINISMUS

Wo sinnvoll:

- feste Demo-Daten,
- stabile Sortierung,
- definierte IDs,
- keine zufälligen UI-Zustände,
- keine zufälligen Wartezeiten.

Falls Zufallsdaten benötigt werden:

Seed setzen.

---

# 50. PERFORMANCE

Der Loop soll gründlich sein, aber keine sinnlosen Wiederholungen erzeugen.

Keine Tests zehnmal ausführen, wenn sich der relevante Code nicht geändert hat.

Nach einem Fehler nur die relevanten Phasen wiederholen und vor FINAL_VERIFY anschließend den vollständigen notwendigen Quality Gate durchführen.

---

# 51. FEHLERBEHANDLUNG

Jeder relevante Fehler soll klassifiziert werden, beispielsweise:

```text
APPLICATION_ERROR
TEST_FAILURE
DEPENDENCY_MISSING
DISPLAY_UNAVAILABLE
RECORDING_FAILURE
TTS_FAILURE
NETWORK_FAILURE
RENDER_FAILURE
VIDEO_QA_FAILURE
EXTERNAL_CREDENTIAL_MISSING
SECURITY_BLOCK
```

Keine allgemeinen Catch-Alls, die Fehler verschlucken.

Stack Traces in Logs zulassen, aber Terminalausgabe kompakt halten.

---

# 52. EXTERNE BLOCKER

Wenn ein echter externer Blocker besteht:

Erzeuge einen klaren Bericht.

Beispiel:

```text
BLOCKED PHASE:
VOICE GENERATION

CAUSE:
OPENAI_API_KEY is not configured.

COMPLETED:
✓ Application
✓ Tests
✓ Demo
✓ Timeline
✓ Narration
✓ Subtitle generation
✓ Rendering pipeline tests

REQUIRED USER ACTION:
Configure OPENAI_API_KEY and rerun:

./run_loop.sh --resume
```

Nur die minimal notwendige Aktion vom Benutzer verlangen.

---

# 53. NICHT ERLAUBT

Vermeide:

- Fake-Success,
- Stub-Funktionen im finalen Pfad,
- Dummy-PASS-Ausgaben,
- hardcodierte Testresultate,
- deaktivierte Tests,
- verschluckte Exceptions,
- erfundene Video-QA-Ergebnisse,
- erfundene API-Ergebnisse,
- Endlosschleifen,
- unkontrollierte Shell-Kommandos.

Mocks sind ausschließlich für Tests erlaubt.

---

# 54. CODE QUALITY

Bevorzuge:

- kleine Module,
- klare Verantwortlichkeiten,
- Typannotationen wo sinnvoll,
- Dataclasses/Enums für States, falls passend,
- nachvollziehbare Interfaces,
- Dependency Injection bei externen Diensten,
- testbare Komponenten.

Keine unnötige Enterprise-Abstraktion.

---

# 55. VIDEO-DESIGN

Das Endvideo soll folgende Wirkung haben:

```text
seriös
modern
präzise
technisch
ruhig
professionell
```

Nicht:

```text
YouTube-Intro
Gaming-Video
TikTok
übertriebene Werbung
```

Das Tool steht im Mittelpunkt.

Keine künstlichen Effekte, die die technische Demonstration überdecken.

---

# 56. WAS DAS VIDEO KOMMUNIZIEREN SOLL

Der Betrachter soll nach dem Video verstehen:

1. Es handelt sich um ein funktionierendes Python-Projekt.
2. Daten werden strukturiert verarbeitet.
3. Daten werden validiert.
4. Fehler werden sichtbar behandelt.
5. Ergebnisse sind nachvollziehbar.
6. Es gibt automatisierte Tests.
7. Der Entwicklungsprozess ist reproduzierbar.
8. Das Projekt besitzt einen automatisierten Demo-/QA-Workflow.

Der Loop selbst darf kurz gezeigt werden, aber nicht die gesamte Demo dominieren.

Das Produkt bleibt Hauptthema.

---

# 57. ACCEPTANCE CRITERIA – APPLICATION

Die Application-Seite gilt nur als erfolgreich, wenn:

- Anwendung startet,
- Demo-Daten geladen werden,
- Verarbeitung funktioniert,
- Validierung funktioniert,
- Fehlerbehandlung funktioniert,
- Export funktioniert,
- Demo Mode funktioniert,
- relevante Tests bestehen.

---

# 58. ACCEPTANCE CRITERIA – LOOP

Der Loop gilt nur als erfolgreich, wenn:

- zentrale State Machine existiert,
- Retry-Limits existieren,
- globale Iterationsgrenze existiert,
- State gespeichert wird,
- Fehler korrekt propagiert werden,
- Logs erzeugt werden,
- Tests bestehen,
- Dry Run funktioniert.

---

# 59. ACCEPTANCE CRITERIA – VIDEO

Die Video-Pipeline gilt nur als erfolgreich, wenn:

- Timeline existiert,
- Narration existiert,
- Recording funktioniert,
- Voiceover vorhanden ist,
- Untertitel erzeugt werden,
- Rendering funktioniert,
- Video technisch validiert wurde,
- finale MP4 vorhanden und decodierbar ist.

---

# 60. FINAL ACCEPTANCE GATE

Der Auftrag ist erst COMPLETE, wenn alle erreichbaren Kriterien erfüllt sind.

Zieloutput:

```text
dist/solcom_demo.mp4
```

sowie:

```text
dist/build_report.md
```

und ein grüner Master-Loop.

Wenn nur ein externer Blocker fehlt, kennzeichne den Build nicht als COMPLETE.

Verwende:

```text
READY_EXCEPT_EXTERNAL_BLOCKER
```

---

# 61. ARBEITSWEISE

Arbeite iterativ:

```text
INSPECT
↓
PLAN
↓
IMPLEMENT
↓
TEST
↓
REVIEW
↓
FIX
↓
RETEST
```

Wiederhole diesen Prozess kontrolliert.

Nach jeder größeren Änderung:

1. relevante Tests,
2. relevante statische Checks,
3. Integration prüfen.

Vor Abschluss:

vollständiges relevantes Testset.

---

# 62. GIT-SICHERHEIT

Bestehende Änderungen des Benutzers nicht überschreiben.

Keine destruktiven Git-Kommandos.

Insbesondere nicht ungefragt:

```text
git reset --hard
git clean -fd
git checkout -- .
```

Benutzeränderungen erhalten.

Falls das Repository bereits uncommitted Änderungen besitzt, arbeite vorsichtig damit.

---

# 63. OPTIONALE SUBAGENTS

Wenn die aktuelle Codex-Umgebung Subagents unterstützt und deren Einsatz tatsächlich sinnvoll ist, darfst du Teilaufgaben delegieren.

Geeignete Bereiche:

- Application Review,
- Test Review,
- Video Pipeline Review,
- Security Review.

Der Hauptagent bleibt verantwortlich für:

- Integration,
- Konsistenz,
- finale Entscheidungen,
- Final Acceptance Gate.

Keine unnötige Agenten-Orchestrierung erzeugen.

---

# 64. PRIORITÄTEN

Bei Konflikten gilt:

```text
1. Sicherheit
2. Korrektheit
3. Reproduzierbarkeit
4. Testbarkeit
5. Zuverlässigkeit
6. Demo-Qualität
7. Performance
8. Komfort
```

---

# 65. ERSTER SCHRITT

Beginne jetzt selbstständig.

Zuerst:

```text
1. Repository vollständig genug analysieren.
2. Bestehende AGENTS.md lesen.
3. Bestehende Tests identifizieren.
4. Architektur und GUI verstehen.
5. vorhandene Demo-/Video-Komponenten suchen.
6. benötigte Änderungen planen.
7. Plan intern priorisieren.
8. Implementierung beginnen.
```

Keine lange theoretische Antwort an den Benutzer schreiben, bevor du mit der Arbeit beginnst.

Arbeite tatsächlich am Repository.

---

# 66. ABSCHLUSSVERHALTEN

Beende deine Arbeit nicht mit:

```text
You can now...
```

wenn du den betreffenden Schritt selbst ausführen kannst.

Führe ihn selbst aus.

Beende erst mit einer kompakten tatsächlichen Statusmeldung.

Beispiel:

```text
MASTER LOOP: PASS

Application ........ PASS
Tests .............. PASS
Demo ............... PASS
Recording .......... PASS
Narration .......... PASS
AI Voice ........... PASS
Subtitles .......... PASS
Rendering .......... PASS
Video QA ........... PASS

Final:
dist/solcom_demo.mp4

Report:
dist/build_report.md
```

Oder bei einem echten externen Blocker:

```text
MASTER LOOP: READY_EXCEPT_EXTERNAL_BLOCKER

Completed:
✓ Application
✓ Tests
✓ Demo
✓ Video pipeline
✓ Narration
✓ Subtitles
✓ QA implementation

Blocked:
AI Voice generation

Reason:
OPENAI_API_KEY missing

Required action:
Configure the credential and run:

./run_loop.sh --resume
```

Keine Erfolgsmeldung ausgeben, die nicht durch tatsächliche Tests bzw. Quality Gates belegt ist.

Beginne jetzt mit der Umsetzung.
