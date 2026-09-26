# Herkunft, Aufbau und behobene Randfälle

## Herkunft

Die Werkbank entstand im Projekt „Stigma“ (Windows, Python 3.13, Claude Code 2.1.282,
Codex 0.156). Dieses Repository ist der davon gelöste, leere Harness: gleiche Funktionsweise,
ohne die dort gebauten Projekte, Versionen und Nachweise. Übernommen wurden Speicher (`store`),
Vermittler (`broker`), HTTP-Dienst (`server`), Terminal-Client (`terminal`), Einstieg
(`uiworkbench`), Claude-Hook (`wake`), Codex-Queue (`codex_queue`), Browserprüfung (`validate`)
sowie die Oberfläche (`ui/`). Weggelassen: der MCP-Kanal (`channel.py`) und die
App-Server-Brücke (`codex_attach.mjs`), die der Skill nicht nutzte.

## Funktionsweise in Kürze

1. `attach` registriert die aufrufende Sitzung (Einmalcode, Geheimnis, Challenge), bestätigt die
   Challenge (`ready`), bindet sie an das Chat-Projekt und **übernimmt** dabei alle Bindungen
   anderer Sitzungen. Die alte Sitzung gilt als ersetzt; ihre offenen Aufträge werden abgebrochen,
   verspätete Ergebnisse abgewiesen (`stale_attempt`).
2. Claude: `attach` legt unter `.local/terminal/wake/` einen Zeiger auf das Profil an. Der
   Projekt-Hook (`Stop`, `SessionStart`, `ConfigChange`; `asyncRewake`) startet `werkbank.py hook`,
   der mit einer Dateisperre genau einen Warteprozess je Sitzung hält und per HTTP-Long-Poll
   (`/api/terminal/wake`) ohne Modellaufrufe wartet. Liegt Arbeit vor, endet er mit Exit-Code 2 und
   dem Weckhinweis; Claude führt `next --once --wake-marker` aus.
3. Codex: `attach` findet den eigenen Codex-Prozess in der Prozesskette, startet einen lokalen
   Warteprozess (`werkbank.py codex-watch`), der Weckhinweise mit `codex queue --thread` zustellt.
4. `next` liefert einen Arbeitsordner mit `result.html`, `context.json` und Bildern. `finish`
   prüft HTML in Chromium und veröffentlicht sofort eine neue Version.

## Änderungen gegenüber dem Original

- **Pfade**: Oberfläche unter `ui/`, Versionen unter `projects/<id>/v/NNN.html`, Daten unter
  `.local/werkbank`. Kein Stigma-Code, keine Controller-/V7-Routen, keine Projektnamen im Code.
- **Ein Einstiegsbefehl** `werkbank.py`, der sich selbst unter der venv ausführt. Alle
  Weckhinweise enthalten absolute Pfade und funktionieren aus jedem Arbeitsverzeichnis
  (vorher: `python -m werkbank.terminal` setzte das Projektroot als Arbeitsverzeichnis voraus).
- **macOS/Linux für Codex**: Prozesserkennung über `ps`, Prozess-Identität über die Startzeit;
  `CREATE_NO_WINDOW` nur unter Windows (vorher stürzte `enable` außerhalb von Windows ab).
- **Sitzungs-ID**: Claude liest `${CLAUDE_SESSION_ID}` im Skill, zusätzlich die Umgebungsvariable
  `CLAUDE_CODE_SESSION_ID`.
- **Benutzerrolle** heißt `user` statt eines festen Namens; alte Sicherungen werden umgeschrieben.

## Behobene Randfälle

1. **Leeres Projekt ohne Version**: Aufträge setzten eine gespeicherte Ausgangsversion voraus;
   im neuen Projekt scheiterte „Senden“. Jetzt: Basis `null`, `result.html` ist eine leere Seite,
   das Ergebnis wird Version 1, die nächste Nachricht folgt automatisch dem neuen Stand.
2. **Keine Projekte**: Die Oberfläche zeigte ein nicht existierendes Standardprojekt; `attach`
   brach ab. Jetzt: Hinweis und offenes Anlegefeld, Senden wird abgefangen, `attach` registriert
   die Sitzung ohne Bindung und wartet; sobald ein Projekt entsteht, bindet die Oberfläche die
   wache Sitzung automatisch.
3. **Projektwechsel im Chat** erforderte ein erneutes `/uiworkbench`, weil die Bindung je Projekt
   galt und der Weckdienst nur ein Projekt beobachtete. Jetzt folgt die verbundene Sitzung dem
   Chat-Projekt; der Weckdienst beobachtet alle Bindungen der Sitzung.
4. **Blockierter Auftrag nach Versionskonflikt**: Scheiterte die Übernahme (Projektstand
   inzwischen verändert), blieb der Auftrag in „Wird geprüft“ und sperrte das Projekt dauerhaft,
   ohne Bedienelement. Jetzt endet er als „Versionskonflikt“ mit Begründung; Antworttext bleibt
   einsehbar; ebenso bei Neustart mit ungeprüftem Ergebnis. Zusätzlich gibt es **abbrechen** in der
   Kopfzeile des Chats und `werkbank.py cancel`.
5. **Hook nur im Projektroot**: Der Hook ignorierte Sitzungen, deren Arbeitsverzeichnis nicht
   exakt das Projektroot war (Unterordner, `cd`). Die Sitzungs-ID genügt als Zuordnung.
6. **Verwaiste Warteprozesse**: Endet Claude, lief der Hook bis zu sieben Tage weiter. Jetzt
   beendet er sich, sobald der Claude-Prozess, der ihn gestartet hat, verschwunden ist.
7. **Ersetzen/Entkoppeln** ist jetzt eine ausdrückliche Sitzungseigenschaft (`detached`) statt
   nur die Abwesenheit einer Bindung; `attach --detach` löst alle Bindungen der Sitzung.
8. **Transaktion bei fehlgeschlagener Übernahme**: Der Konfliktzustand wird in einer eigenen
   Transaktion gespeichert (die Rückabwicklung der `sqlite3`-Transaktion hätte ihn sonst verworfen).

## Nicht geänderte Grenzen

- Der Hook braucht eine Claude-Code-Version mit `asyncRewake` (geprüft: 2.1.265 und 2.1.280
  enthalten es; live verifiziert war 2.1.282 unter Windows). Nach sieben Tagen ohne Turn endet der
  Wartehook; `/uiworkbench` erneut aufrufen.
- `codex queue` muss in der installierten Codex-CLI vorhanden sein (ab 0.154 geprüft).
- Die Chromium-Prüfung belegt Browserstart, Seitenfehler und externe Zugriffe, keine fachliche
  Richtigkeit. Ein Snapshot bezieht sich auf die aufgezeichnete Fenstergröße.
- Eine angebundene Sitzung reagiert erst, wenn ihr aktueller Turn beendet ist; die Werkbank
  erzwingt keine gleichzeitigen Modellturns.
