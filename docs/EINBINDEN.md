# Werkbank in einen anderen Projektordner einbinden

Standardfall: Die Sitzung läuft im Werkbank-Ordner selbst; Skill und Hooks liegen dort bereits
(`.claude/skills/uiworkbench`, `.claude/settings.json`, `.agents/skills/uiworkbench`).

Soll `/uiworkbench` auch in Sitzungen funktionieren, die in einem **anderen** Projektordner
starten (zum Beispiel ein Arbeitsbereich, in dem die Werkbank als Unterordner liegt), schreibt
`install` Skill und Hooks dorthin, mit absoluten Pfaden auf diese Werkbank:

```bash
python3 werkbank.py install --into "/Pfad/zum/Projekt" --settings-file settings.json
```

Geschrieben werden:

- `<Projekt>/.claude/skills/uiworkbench/SKILL.md` (Claude, Befehl `/uiworkbench`)
- `<Projekt>/.agents/skills/uiworkbench/SKILL.md` (Codex, Befehl `$uiworkbench`)
- `<Projekt>/.claude/settings.json` bzw. `settings.local.json`: die Hooks `Stop`, `SessionStart`
  und `ConfigChange` mit `asyncRewake`. Vorhandene andere Hooks und Einstellungen bleiben erhalten;
  nur frühere Werkbank-Einträge werden ersetzt.

Claude Code lädt Projekt-Skills und -Hooks aus dem Projektroot (dem Ordner, in dem `.claude`
liegt, in der Regel die Git-Wurzel). Sitzungen in Unterordnern finden sie ebenfalls. Codex liest
`.agents/skills` ab dem Arbeitsverzeichnis aufwärts.

Die Werkbank selbst (Datenbank, Profile, `projects/`) bleibt im Werkbank-Ordner. Liegt dieser
innerhalb eines Git-Repositories des Projekts, den Ordner dort in `.gitignore` aufnehmen, damit
die beiden Repositories getrennt bleiben.

Nach `git pull` einer neuen Werkbank-Version `install` erneut ausführen, falls sich die
Skill-Texte geändert haben (sie sind im Projekt Kopien).

**Wichtig:** Claude Code lädt die Hook-Konfiguration beim Sitzungsstart. Eine Sitzung, die
schon lief, als `install` die Hooks geschrieben hat, wird nicht automatisch geweckt (beobachtet
mit Claude Code 2.1.265: `attach` klappt, aber der Wartehook startet nicht). Nach `install` also
eine neue Sitzung starten und dort `/uiworkbench` aufrufen; in der laufenden Sitzung kann `/hooks`
die neue Konfiguration sichtbar machen. `python3 werkbank.py status` zeigt mit `wake_armed`, ob
der Wartehook einer Sitzung tatsächlich läuft.
