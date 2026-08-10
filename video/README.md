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
3. deutschen Sprechertext und SRT aus der Timeline erzeugen
4. FastAPI und Streamlit lokal starten
5. allowlistete GUI-Szenen mit Headless Chrome aufnehmen
6. Voice-Segmente erzeugen oder explizite Preview-Stille markieren
7. Szenen mit FFmpeg als H.264/AAC rendern
8. Untertitel einbrennen und MP4 für Streaming optimieren
9. Codec, Streams, Auflösung, 30 FPS, Dauer, Dateigröße und Volldecode prüfen
10. drei Stichprobenframes extrahieren und den QA-Bericht schreiben

## TTS-Konfiguration

```bash
export OPENAI_API_KEY="<nur in der Shell oder einem Secret Store>"
export OPENAI_TTS_MODEL="gpt-4o-mini-tts"
export OPENAI_TTS_VOICE="coral"
./run_loop.sh --resume
```

Der Schlüssel darf nie in `.env`, Logs, Screenshots oder Git geschrieben
werden. Szenen-Audio wird unter `video/tmp/audio/` inhaltsadressiert
gecacht.

Fehlt der Schlüssel, ist dies kein Application-Fehler. Der Build erzeugt
`dist/solcom_demo_preview.mp4`, setzt Status
`READY_EXCEPT_EXTERNAL_BLOCKER` und beendet sich mit Code `10`. Die
Vorschau enthält bewusst Stille und darf nicht als finales Voiceover
veröffentlicht werden.

## Outputs

- final: `dist/solcom_demo.mp4`
- stumme Vorschau: `dist/solcom_demo_preview.mp4`
- QA: `dist/video_qa_report.json`
- Bericht: `dist/build_report.md`
- Narration: `video/script/narration.md`
- Untertitel: `video/tmp/subtitles.srt`
- Aufnahmen: `video/tmp/screenshots/`
- Logs: `video/logs/`

## Determinismus und Wartung

Szenenreihenfolge, Text, Dauer, Capture-Modus und Overlay stehen ausschließlich
in `video/script/timeline.json`. Capture-Modi außerhalb der Code-Allowlist
werden abgelehnt. Änderungen an Produktfunktionen erfordern eine Aktualisierung
von Timeline, Narration und Tests.

## Troubleshooting

**Port belegt:** `VIDEO_API_PORT` und `VIDEO_DASHBOARD_PORT` auf freie
lokale Ports setzen.

**Chrome fehlt:** einen systemweit vorhandenen Google-Chrome-Binary
bereitstellen. Der Loop installiert keine Hostpakete und verwendet kein
`sudo`.

**FFmpeg-Fehler:** FFmpeg-Build auf `libx264`, AAC und den
`subtitles`-Filter prüfen.

**Aufnahme bleibt leer:** `video/logs/recording-api.log`,
`video/logs/recording-dashboard.log` und den szenenspezifischen
Capture-Log prüfen.

**TTS blockiert:** Schlüssel sicher in der Prozessumgebung setzen und
`./run_loop.sh --resume` ausführen. Bereits bestandene Phasen werden nicht
unnötig neu ausgeführt.
