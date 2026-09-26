---
name: uiworkbench
description: Diese bestehende Codex-Sitzung an die lokale UI-Werkbank anbinden oder den Bearbeiter wechseln.
---

Werkbank-Ordner: drei Verzeichnisse oberhalb dieser SKILL.md
Lies einmal "drei Verzeichnisse oberhalb dieser SKILL.md/docs/AGENT.md" (Arbeitsvertrag), falls in dieser Sitzung noch nicht gelesen.

Fuehre genau diesen Befehl aus (ein Prozess, Pfade quoten; er startet bei Bedarf den lokalen Webdienst):

    python3 -B "WERKBANK-ORDNER/werkbank.py" attach --provider codex

Die Identitaet kommt aus der Umgebungsvariable CODEX_THREAD_ID dieser Sitzung; keine fremde oder
erfundene ID. `--project NAME` nur bei ausdruecklich genanntem Ziel; auf Wunsch `--detach`.

Der Befehl uebernimmt sofort den Chat der Werkbank. Eine vorher verbundene Sitzung wird ersetzt
und entkoppelt; ihre offenen Auftraege werden abgebrochen.

Bei `transport: codex-queue`: kurz bestaetigen, die Adresse aus `browser` nennen und den Turn
beenden. Ein lokaler Helfer stellt Weckhinweise ueber `codex queue` an genau diese Sitzung zu.
Keine Polling-Schleife. Den im Weckhinweis enthaltenen `next --once`-Aufruf ausfuehren, nur den
gelieferten Auftrag bearbeiten, mit `finish` abgeben. Bei Rueckfrage `event --kind question`.
Nach `finish`, `question`, `waiting` oder `idle` wieder den Turn beenden. Keine neue Sitzung und
keine globale Konfigurationsaenderung. Direkte Benutzerauftraege gehen vor.
