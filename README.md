# UI-Werkbank

Eine lokale Werkbank, um Oberflächen (HTML-Mockups, Frontends, einzelne Seiten) gemeinsam mit
einer **bereits laufenden** Claude-Code- oder Codex-Sitzung zu entwerfen. Im Browser schreibst du
Nachrichten, markierst Stellen im angezeigten Entwurf oder frierst ihn als Snapshot ein; die
angebundene Sitzung wird automatisch geweckt, bearbeitet den Auftrag und liefert Text oder eine
neue HTML-Version zurück. Jede Version bleibt erhalten.

Es wird kein Agent gestartet und kein API-Schlüssel gebraucht: Die Werkbank bindet genau die
Sitzung an, in der du `/uiworkbench` (Claude) oder `$uiworkbench` (Codex) eingibst.

## Einrichten

Voraussetzungen: Python 3.10 oder neuer, Claude Code oder Codex CLI.

```bash
git clone https://github.com/hoebel2025-sudo/ui-werkbank.git
cd ui-werkbank
python3 werkbank.py setup        # legt .venv an, installiert Playwright + Chromium für die HTML-Prüfung
```

Windows: `python werkbank.py setup` (dabei wird `.claude/settings.local.json` mit dem Pfad der
venv geschrieben, weil `python3` dort nicht sicher auf dem PATH liegt).

## Benutzen

1. Werkbank starten: `python3 werkbank.py start` oder Doppelklick auf `Werkbank starten.command`
   (Mac) bzw. `Werkbank_starten.cmd` (Windows). Adresse: <http://127.0.0.1:8119>. Beenden mit Strg+C.
   Der Schritt ist optional: `/uiworkbench` startet den Dienst bei Bedarf selbst.
2. Eine Claude- oder Codex-Sitzung **in diesem Ordner** starten (oder in einem Projekt, in das die
   Werkbank eingebunden ist, siehe [docs/EINBINDEN.md](docs/EINBINDEN.md)) und dort `/uiworkbench`
   bzw. `$uiworkbench` eingeben. Die Sitzung meldet sich an und beendet ihren Turn.
3. Im Browser oben im Chat ein Projekt anlegen („+ Neues Projekt …“), beschreiben, was entstehen
   soll, **Senden**. Antworten und neue Versionen erscheinen automatisch; Rückfragen beantwortest
   du im selben Chatfeld.

Eine andere Sitzung übernimmt mit demselben Befehl sofort; die vorherige wird entkoppelt und ihre
offenen Aufträge werden abgebrochen. Der Chat-Verlauf bleibt sichtbar, wird der neuen Sitzung aber
nicht als Kontext mitgeschickt.

Weitere Befehle: `python3 werkbank.py status` (Sitzungen, Bindungen, Aufträge),
`python3 werkbank.py cancel` (offene Aufträge abbrechen), `python3 werkbank.py detach --provider claude|codex`,
`python3 werkbank.py doctor`, `python3 werkbank.py test`.

## Dokumentation

- [docs/BEDIENUNG.md](docs/BEDIENUNG.md): Oberfläche, Snapshots, Versionen, Speicher und Sicherung.
- [docs/AGENT.md](docs/AGENT.md): Vertrag für die angebundene Sitzung (wird vom Skill gelesen).
- [docs/EINBINDEN.md](docs/EINBINDEN.md): Werkbank in einen anderen Projektordner einbinden.
- [docs/AENDERUNGEN.md](docs/AENDERUNGEN.md): Herkunft, Aufbau und behobene Randfälle.

## Was wo liegt

| Ort | Inhalt |
|---|---|
| `werkbank.py` | Einstiegsbefehl (setup, start, attach, install, status, cancel, test) |
| `werkbank/` | Python-Paket: SQLite-Speicher, HTTP-Dienst, Sitzungsanbindung, Weckdienste |
| `ui/` | Oberfläche (index.html, Adapter, html2canvas) |
| `projects/<id>/v/NNN.html` | veröffentlichte Versionen als Dateien (nicht im Git) |
| `.local/` | Datenbank, private Sitzungsprofile, Arbeitsordner, Protokolle (nicht im Git) |
| `.claude/`, `.agents/` | Skill `uiworkbench` für Claude bzw. Codex; Claude-Hooks |
| `tests/` | pytest-Suite inklusive echter Browser-Durchläufe |

Der Dienst hört nur auf 127.0.0.1. Fremde Hostnamen und Origins werden abgewiesen; private
Sitzungsprofile werden nie ausgeliefert.

## Aktualisieren

```bash
git pull
python3 werkbank.py setup      # nur nötig, wenn sich requirements.txt geändert hat
```

Eine laufende Werkbank danach neu starten. Angebundene Sitzungen `/uiworkbench` erneut aufrufen.
