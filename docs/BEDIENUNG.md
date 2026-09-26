# Bedienung der Werkbank

## Start und Beenden

Im Werkbank-Ordner `python3 werkbank.py start` (Mac/Linux) oder `python werkbank.py start`
(Windows); alternativ `Werkbank starten.command` bzw. `Werkbank_starten.cmd` doppelklicken.
Adresse: http://127.0.0.1:8119. Beenden mit Strg+C im Startfenster. Ein belegter Port führt zu
einer Fehlermeldung; ein fremder Dienst wird nie beendet. Anderer Port und getrennter Speicher:

```bash
python3 werkbank.py start --port 8120 --data-dir .local/werkbank-versuch
```

`/uiworkbench` in einer Sitzung startet den Dienst bei Bedarf selbst (im Hintergrund, Protokoll
unter `.local/terminal/service/`).

## Oberfläche

- **Ansicht** (Leiste oben): zeigt ein beliebiges Projekt und eine beliebige Version an, ohne den
  Chat zu ändern. „Vorher zeigen“ (Taste V) wechselt zum Vorgänger.
- **Chat** (verschiebbares Fenster): Projektwahl, Verlauf, Eingabe und Versionsliste. Das
  Chat-Projekt bestimmt, wohin eine Nachricht geht und wo neue Versionen entstehen. „+ Neues
  Projekt …“ legt ein Projekt an; es beginnt ohne Version, die erste Antwort mit HTML wird Version 1.
- **Auswählen**: Elemente im angezeigten Entwurf anklicken, sie werden als R-Referenzen angehängt.
- **Snapshot**: Entwurf einfrieren, Rechteck/Kreis/Pfeil ziehen, Elemente wählen, beschriften und
  mit „Anhängen“ als SC-Referenz mit Zustand, Treffern und Bild speichern. Ein Klick auf eine
  SC-Referenz im Verlauf stellt Version, Zustand, Scrollstellen und Markierungen wieder her.
- **Senden** speichert und beauftragt gemeinsam. Der Status in der Kopfzeile zeigt die verbundene
  Sitzung (`bereit`, `arbeitet`, `wartet auf deine Antwort`) oder den Hinweis, `/uiworkbench`
  aufzurufen. Bei einem laufenden Auftrag erscheint dort **abbrechen**.
- Rückfragen der Sitzung erscheinen im Verlauf; die Antwort schreibst du in dasselbe Feld.
- Arbeitsansicht, Chatfenster und Entwurf werden automatisch gespeichert („Gespeichert“ /
  „Speichert …“ / „Ungespeichert“ bei Ausfall).

Ein Sitzungswechsel im Chat-Projekt geschieht automatisch: Die verbundene Sitzung folgt dem im
Chat gewählten Projekt. Ein Wechsel der Sitzung selbst geschieht nur über `/uiworkbench` in der
anderen Sitzung.

Die Zustandsaufnahme nutzt `window.__zustand()` und `window.__setzeZustand(z)` des Entwurfs,
sonst dessen `localStorage`. Nicht serialisierbare Zustände fremder Seiten werden nicht unterstützt.

## Aufträge und Zustände

Wartet → In Bearbeitung → (Rückfrage →) Wird geprüft → Beantwortet. Weitere Zustände:
Abgebrochen (Sitzungswechsel, „abbrechen“), Unterbrochen (Neustart des Dienstes, 30 Minuten ohne
Lebenszeichen), Fehlgeschlagen (Sitzung meldet einen Fehler), Versionskonflikt (der Projektstand
wurde während der Bearbeitung verändert oder ein Ergebnis lag beim Neustart ungeprüft vor).
Keiner dieser Zustände wird automatisch wiederholt: die Nachricht erneut senden.

## Versionen

Neue Versionen entstehen nur über `finish` einer angebundenen Sitzung nach dem lokalen
Chromium-Starttest. Die Datenbank ist maßgeblich; Dateikopien liegen unter
`projects/<id>/v/NNN.html` und werden nie überschrieben. Manuell dort abgelegte Dateien mit dem
gleichen Schema werden beim Start oder über `POST /api/sync` eingelesen.

## Speicher und Sicherung

Standardablage: `.local/werkbank/werkbank.sqlite3` (Dokumente, Screenshots, vollständige
HTML-Versionen; WAL-Journal). Eine Kopie der laufenden Datei kann unvollständig sein; zum
Übertragen `GET /api/export` (ZIP mit `manifest.json`, `assets/`, `versions/`) verwenden oder den
Dienst vorher beenden. `POST /api/import` ergänzt fehlende Inhalte, stellt Arbeitsansicht und
Entwurf wieder her, deaktiviert Bindungen und unterbricht offene Versuche. Vorher wird der
bisherige Stand unter `.local/werkbank/backups/` gesichert. Private Terminal-Zugangsdaten
(`.local/terminal/*.json`) gehören nicht in Sicherungen.

## Schnittstellen

| Route | Zweck |
|---|---|
| GET /api/session | Sitzungstoken für lokale Schreibzugriffe |
| GET /api/state?since=N | Dokumente und Revisionsstand |
| PUT /api/doc/Sammlung/ID | Projekt, Snapshot oder Einstellung speichern |
| POST /api/messages | Nachricht idempotent speichern und beauftragen |
| POST /api/assets; GET /api/assets/ID | Screenshot speichern/lesen (ID = SHA-256) |
| POST /api/sync | neue HTML-Dateien einlesen |
| GET /api/export; POST /api/import | Sicherung und Import |
| GET /p/ID/v/NNN.html | eingelesene Version aus dem Speicher |
| GET /api/agents/state; POST /api/agents/{pair,bind,detach,enqueue,cancel,answer,accept} | Sitzungen und Aufträge |
| POST /api/terminal/{register,ready,next,wake,event,finish,heartbeat,transport} | angebundene Sitzung |

## Prüfen

```bash
python3 werkbank.py test
```

Die Suite nutzt temporäre Speicher und eigene Ports, startet echte Chromium-Instanzen und
verändert keinen laufenden Werkbankstand. Sie belegt die Protokolle gegen Testgegenstellen,
nicht das tatsächliche Aufwecken einer Claude-/Codex-Sitzung; dafür gilt die Live-Probe aus
[AENDERUNGEN.md](AENDERUNGEN.md).
