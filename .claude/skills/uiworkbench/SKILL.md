---
name: uiworkbench
description: Diese laufende Claude-Sitzung an die lokale UI-Werkbank anbinden. Danach weckt die Werkbank die Sitzung bei neuen Chat-Auftraegen automatisch.
disable-model-invocation: true
---

Werkbank-Ordner: ${CLAUDE_SKILL_DIR}/../../..
Lies einmal "${CLAUDE_SKILL_DIR}/../../../docs/AGENT.md" (Arbeitsvertrag), falls in dieser Sitzung noch nicht gelesen.

Fuehre genau diesen Befehl aus (ein Prozess, Pfade quoten; er startet bei Bedarf den lokalen Webdienst):

    python3 -B "${CLAUDE_SKILL_DIR}/../../../werkbank.py" attach --provider claude --identity "${CLAUDE_SESSION_ID}"

Die Kennung ist die echte ID dieser Sitzung; keine fremde oder erfundene ID. Ist "${CLAUDE_SESSION_ID}"
nicht aufgeloest, nimm die Shell-Variable $CLAUDE_CODE_SESSION_ID. `--project NAME` nur bei
ausdruecklich genanntem Ziel; auf Wunsch `--detach`.

Der Befehl uebernimmt sofort den Chat der Werkbank. Eine vorher verbundene Sitzung wird ersetzt
und entkoppelt; ihre offenen Auftraege werden abgebrochen. Gibt es noch kein Projekt, wird die
Sitzung automatisch gebunden, sobald im Browser eines angelegt wird.

Bei `transport: claude-hook`: kurz bestaetigen, die Adresse aus `browser` nennen und den Turn
beenden. Der Projekt-Stop-Hook wartet ohne Modellaufrufe und weckt bei neuer Arbeit. Keine
Abrufschleife im Vordergrund. Den `next --once --wake-marker ...`-Aufruf aus dem Weckhinweis genau
einmal ausfuehren, den gelieferten Auftrag bearbeiten und mit `finish` abgeben. Bei Rueckfrage
`event --kind question` senden. Nach `finish`, `question`, `waiting` oder `idle` den Turn beenden.
Grosse HTML-Dateien gezielt durchsuchen. Keine neue Sitzung und keinen weiteren Agenten starten.
Direkte Benutzerauftraege haben Vorrang.
