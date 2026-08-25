# Reproduzierbarer Demo-Video-Build

Dieses Modul erzeugt eine technische, deutschsprachige Demonstration der realen
RAGOps-Anwendung. Es verwendet keine separat erfundene Produktattrappe.

## Voraussetzungen

1. Projektumgebung mit `make setup`
2. FFmpeg und FFprobe
3. Google Chrome mit Headless-Modus
4. optional `OPENAI_API_KEY` für das finale Voiceover

Prüfen, ohne Dienste, Aufnahme oder TTS zu starten:

```bash
./video/build_demo.sh --dry-run
```

Vollständiger Video-Build:

```bash
./video/build_demo.sh
```

Der übergeordnete Ablauf inklusive aller Application- und Security-Gates ist:

```bash
./run_loop.sh
```

## Pipeline

1. Timeline und lokale Werkzeuge prüfen
2. echten Demo-Controller ausführen
3. deutschen Sprechertext aus der Timeline erzeugen
4. FastAPI und Streamlit lokal starten
5. allowlistete GUI-Szenen aufnehmen und sichtbare Begriffe prüfen
6. Narration gegen Code, Demo, Timing und unbelegte Claims validieren
7. erst nach bestandenem Gate Voice-Cache beziehungsweise TTS verwenden
8. reale Audiodauern messen und das optionale SRT-Sidecar synchronisieren
9. Szenen vollständig mit Vor-/Nachpause und weichen Übergängen rendern
10. MP4 ohne eingebrannte Untertitel für Streaming optimieren
11. Codec, Streams, Auflösung, 30 FPS, Dauer, Dateigröße und Volldecode prüfen
12. drei Stichprobenframes extrahieren und den QA-Bericht schreiben

Details zum Gate: [docs/NARRATION_QA.md](../docs/NARRATION_QA.md).

## TTS-Konfiguration und Cache

```bash
export OPENAI_API_KEY="<nur in der Shell oder einem Secret Store>"
export TTS_PROVIDER="openai"
export TTS_MODEL="gpt-4o-mini-tts"
export TTS_VOICE="coral"
./run_loop.sh --resume
```

Der Schlüssel darf nie in `.env`, Logs, Reports, Screenshots, Video, Cache-Key
oder Git geschrieben werden. Szenen-Audio wird persistent unter
`video/cache/tts/` gespeichert und vor jeder Wiederverwendung mit FFprobe
validiert. Der Cache überlebt normale Builds und `--clean-temp`.

```bash
./video/build_demo.sh --dry-run
./video/build_demo.sh --cache-only
./video/build_demo.sh --clean-temp
```

Fehlt der Schlüssel bei vollständigem Cache, wird das finale Video ohne
API-Aufruf gebaut. Fehlen Segmente, setzt der Build
`BLOCKED_EXTERNAL_CREDENTIAL`, beendet sich mit Code `10` und erzeugt kein
Fake-Audio. Eine stumme Vorschau gibt es ausschließlich über `--skip-tts`.
Details zu Hash-Feldern, Locks, Validierung, Limits und Reports:
[docs/TTS_CACHE.md](../docs/TTS_CACHE.md).

## Outputs

- final: `dist/solcom_demo.mp4`
- stumme Vorschau: `dist/solcom_demo_preview.mp4`
- Narration-QA: `dist/narration_qa_report.json`
- QA: `dist/video_qa_report.json`
- Bericht: `dist/build_report.md`
- Narration: `video/script/narration.md`
- Untertitel: `video/tmp/subtitles.srt`
- Aufnahmen: `video/tmp/screenshots/`
- Logs: `video/logs/`

## Determinismus und Wartung

Szenenreihenfolge, Text, Dauer, Capture-Modus, Overlay, Claims, Codebelege und
sichtbare Begriffe stehen in `video/script/timeline.json`. Capture-Modi
außerhalb der Code-Allowlist werden abgelehnt. Änderungen an Produktfunktionen
erfordern eine Aktualisierung von Timeline, Narration und Tests.

`VIDEO_BURN_SUBTITLES=false` ist der Standard. Optionales Einbrennen und
`VIDEO_TRANSITION_SECONDS` verändern den TTS-Cache-Key nicht.

## Troubleshooting

**Port belegt:** `VIDEO_API_PORT` und `VIDEO_DASHBOARD_PORT` auf freie
lokale Ports setzen.

**Chrome fehlt:** einen systemweit vorhandenen Google-Chrome-Binary
bereitstellen. Der Loop installiert keine Hostpakete und verwendet kein
`sudo`.

**FFmpeg-Fehler:** FFmpeg-Build auf `libx264`, AAC, `xfade` und
`acrossfade` prüfen. Der `subtitles`-Filter ist nur bei explizitem Burn-in nötig.

**Aufnahme bleibt leer:** `video/logs/recording-api.log`,
`video/logs/recording-dashboard.log` und den szenenspezifischen
Capture-Log prüfen.

**TTS blockiert:** Cache mit `--dry-run` prüfen. Anschließend entweder den
Schlüssel sicher in derselben Prozessumgebung setzen oder fehlende validierte
Cache-Dateien bereitstellen und `./run_loop.sh --resume` ausführen.
