# Vertrag für die angebundene Sitzung

Gilt für die Claude- oder Codex-Sitzung, die sich mit `/uiworkbench` bzw. `$uiworkbench`
an die UI-Werkbank angebunden hat. Nur diese bereits laufende Sitzung wird angebunden;
kein Ersatzagent, keine zweite Sitzung, kein Fortsetzen eines fremden Threads.
Ein Auftrag im normalen Terminal ist kein Auftrag an den Werkbank-Chat und umgekehrt.

## Ablauf

1. `attach` übernimmt sofort den Chat der Werkbank. Eine vorher verbundene Sitzung wird
   ersetzt und entkoppelt; ihre offenen Aufträge werden abgebrochen und nicht weitergegeben.
2. Nach erfolgreichem Anbinden den Turn beenden. Claude wird vom Projekt-Stop-Hook geweckt,
   Codex über `codex queue`. Keine `next`-Polling-Schleifen im Modellturn.
3. Der Weckhinweis enthält ein Argumentarray (Python der Werkbank, `werkbank.py terminal`,
   privates Profil, `next --once --wake-marker ...`). Genau einmal als einen Prozess ausführen.
   Es funktioniert aus jedem Arbeitsverzeichnis.
4. Bei `idle`, `waiting`, `detached` oder veraltetem Wecksignal (`wake_marker`) den Turn beenden.

## Bei `job` / `active`

`active` ist derselbe, schon begonnene Versuch (etwa nach einer beantworteten Rückfrage).

1. `context_file` (context.json) lesen, angehängte Originalbilder ansehen. Alte Chatantworten
   werden nicht mitgegeben; die Nachricht enthält Auftrag, Zielversion, Zustand der Ansicht
   und Referenzen (R1 = Element/Version, SC1 = Snapshot mit Markierungen und Bild).
   Aktuelle HTML-Basis und ausdrücklich referenzierte Versionen unterscheiden.
2. Nur die bereitgestellte `result.html` im Arbeitsordner ändern (`result_file`). Historische
   Versionen unter `projects/`, die Werkbank selbst und die Datenbank nie direkt ändern.
   Hat das Projekt noch keine Version, ist `result.html` eine leere Seite; das Ergebnis wird Version 1.
   Große HTML-Dateien gezielt mit `rg`/Grep durchsuchen, nicht komplett in den Kontext laden.
3. Erforderliche Bedienprüfungen wirklich durchführen; keine Prüferfolge erfinden. Für eigene
   Browsertests das Playwright der Werkbank-venv nutzen und Browser/Server im selben Aufruf schließen.
4. Rückfrage: `event --attempt ID --kind question --text "..."`, danach Turn beenden.
   Die Antwort im Chat weckt dieselbe Sitzung erneut. Nicht raten, nicht weiter abfragen.
5. Ergebnis: `finish --attempt ID --text "Antwort" --html PFAD/result.html`.
   Für reine Chatantworten `--html` weglassen. Optional `--checks DATEI.json` mit einer Liste
   tatsächlich durchgeführter Prüfungen. Danach Turn beenden.

Alle Befehle: `<Werkbank-Ordner>/.venv/bin/python -B <Werkbank-Ordner>/werkbank.py terminal --profile PROFIL ...`
(Windows: `.venv\Scripts\python.exe`). `finish` prüft HTML automatisch im lokalen Chromium
(JavaScript-Seitenfehler, externe Zugriffe; keine fachliche Abnahme) und veröffentlicht die neue
Version sofort im Chat. Bei einem Prüfungsfehler korrigieren und erneut abgeben; eine wiederholte
identische Abgabe erzeugt keine Doppelversion. Wurde der Projektstand inzwischen verändert,
wird der Auftrag als `Versionskonflikt` beendet: Antwort im Chat lesen, Auftrag erneut senden.

Bei nicht behebbarer Störung `event --attempt ID --kind failed --text "Grund"`. Bei längerer
Bearbeitung `event --kind progress` melden; 30 Minuten ohne Kontakt sperren den Versuch.

## Abbruch und Vorrang

Bei `stale_attempt`, `detached` oder Abbruch die Bearbeitung sofort beenden und eigene
Programme geordnet stoppen. Die Werkbank sperrt alte Ergebnisse, kann laufende Terminalaktionen
aber nicht rückwirkend stoppen. Direkte Benutzeraufträge im Terminal haben immer Vorrang.
Keine globale Konfiguration ändern; keine Zugangsdaten aus `.local/terminal` ausgeben.
