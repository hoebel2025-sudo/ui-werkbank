#!/usr/bin/env python3
"""UI-Werkbank: ein Einstiegsbefehl fuer alle Aufgaben. Laeuft aus jedem Arbeitsverzeichnis.

  python3 werkbank.py setup            Python-Umgebung (.venv), Playwright und Chromium einrichten
  python3 werkbank.py start            lokalen Dienst starten (http://127.0.0.1:8119) und Browser oeffnen
  python3 werkbank.py attach ...       diese bestehende Claude-/Codex-Sitzung anbinden (macht /uiworkbench)
  python3 werkbank.py install --into DIR   Skill und Hooks in einen anderen Projektordner schreiben
  python3 werkbank.py status | cancel | detach | test | doctor
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import urllib.request

ROOT = Path(__file__).resolve().parent
VENV_PY = ROOT / '.venv' / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
DEFAULT_URL = 'http://127.0.0.1:8119'
HOOK_EVENTS = ('Stop', 'SessionStart', 'ConfigChange')


def reexec_in_venv():
    """Run every subcommand with the project's own interpreter, so that sys.executable in
    generated hook/wake commands always points to the environment that has Playwright."""
    # Compare the active prefix, not resolved paths: on macOS the venv's python is a symlink to the
    # same binary as the system python3, so resolve() would wrongly report "already inside".
    if not VENV_PY.exists() or Path(sys.prefix).resolve() == (ROOT / '.venv').resolve() or os.environ.get('WERKBANK_NO_REEXEC'):
        return
    args = [str(VENV_PY), '-B', str(ROOT / 'werkbank.py'), *sys.argv[1:]]
    if os.name == 'nt':
        raise SystemExit(subprocess.call(args))
    env = dict(os.environ); env.pop('__PYVENV_LAUNCHER__', None)
    os.execve(str(VENV_PY), args, env)


def python_for_hooks():
    return str(VENV_PY) if VENV_PY.exists() else (sys.executable if Path(sys.executable).exists() else 'python3')


# ----------------------------------------------------------------------------- Vorlagen
def claude_skill(root_display, python_cmd, script_display):
    return f'''---
name: uiworkbench
description: Diese laufende Claude-Sitzung an die lokale UI-Werkbank anbinden. Danach weckt die Werkbank die Sitzung bei neuen Chat-Auftraegen automatisch.
disable-model-invocation: true
---

Werkbank-Ordner: {root_display}
Lies einmal "{root_display}/docs/AGENT.md" (Arbeitsvertrag), falls in dieser Sitzung noch nicht gelesen.

Fuehre genau diesen Befehl aus (ein Prozess, Pfade quoten; er startet bei Bedarf den lokalen Webdienst):

    {python_cmd} -B "{script_display}" attach --provider claude --identity "${{CLAUDE_SESSION_ID}}"

Die Kennung ist die echte ID dieser Sitzung; keine fremde oder erfundene ID. Ist "${{CLAUDE_SESSION_ID}}"
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
'''


def codex_skill(root_display, python_cmd, script_display):
    return f'''---
name: uiworkbench
description: Diese bestehende Codex-Sitzung an die lokale UI-Werkbank anbinden oder den Bearbeiter wechseln.
---

Werkbank-Ordner: {root_display}
Lies einmal "{root_display}/docs/AGENT.md" (Arbeitsvertrag), falls in dieser Sitzung noch nicht gelesen.

Fuehre genau diesen Befehl aus (ein Prozess, Pfade quoten; er startet bei Bedarf den lokalen Webdienst):

    {python_cmd} -B "{script_display}" attach --provider codex

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
'''


def hook_entry(python_cmd, script_path):
    return {'type': 'command', 'command': python_cmd, 'args': ['-B', script_path, 'hook'],
            'asyncRewake': True, 'timeout': 604800}


def hooks_config(python_cmd, script_path):
    entry = hook_entry(python_cmd, script_path)
    return {'Stop': [{'hooks': [entry]}], 'SessionStart': [{'hooks': [entry]}],
            'ConfigChange': [{'matcher': 'project_settings', 'hooks': [dict(entry)]}]}


def is_werkbank_hook(hook):
    return isinstance(hook, dict) and any('werkbank.py' in str(a) for a in hook.get('args', [])) or 'werkbank.py' in str(hook.get('command', ''))


def merge_hooks(settings, python_cmd, script_path):
    """Replace only our own hook entries; keep every other hook and setting untouched."""
    hooks = settings.setdefault('hooks', {})
    for event, groups in hooks_config(python_cmd, script_path).items():
        existing = [g for g in hooks.get(event, []) if not any(is_werkbank_hook(h) for h in g.get('hooks', []))]
        hooks[event] = existing + groups
    return settings


def install_user():
    """Make /uiworkbench and the wake hooks available in every session of this user account:
    ~/.claude/skills, ~/.claude/settings.json (hooks merged) and ~/.codex/skills for Codex."""
    home = Path.home()
    root, script, python = str(ROOT), str(ROOT / 'werkbank.py'), python_for_hooks()
    written = []
    claude_dir = home / '.claude/skills/uiworkbench'; claude_dir.mkdir(parents=True, exist_ok=True)
    (claude_dir / 'SKILL.md').write_text(claude_skill(root, '"' + python + '"', script), encoding='utf-8'); written.append(claude_dir / 'SKILL.md')
    codex_dir = home / '.codex/skills/uiworkbench'; codex_dir.mkdir(parents=True, exist_ok=True)
    (codex_dir / 'SKILL.md').write_text(codex_skill(root, '"' + python + '"', script), encoding='utf-8'); written.append(codex_dir / 'SKILL.md')
    settings_path = home / '.claude/settings.json'
    settings = {}
    if settings_path.exists():
        try: settings = json.loads(settings_path.read_text(encoding='utf-8'))
        except ValueError: raise SystemExit(f'{settings_path} ist kein gueltiges JSON; bitte zuerst reparieren.')
    backup = settings_path.with_name('settings.json.vor-werkbank')
    if settings_path.exists() and not backup.exists():
        shutil.copy2(settings_path, backup); print('Sicherung:', backup)
    merge_hooks(settings, python, script)
    settings_path.write_text(json.dumps(settings, ensure_ascii=False, indent=2) + '\n', encoding='utf-8'); written.append(settings_path)
    for path in written: print('geschrieben:', path)
    print('Gilt fuer neue Sitzungen in jedem Ordner. Claude: /uiworkbench, Codex: $uiworkbench.')
    return 0


def install(into, settings_file='settings.local.json', generic=False):
    into = Path(into).resolve()
    if generic:
        root_display, script_display, python_cmd = '${CLAUDE_SKILL_DIR}/../../..', '${CLAUDE_SKILL_DIR}/../../../werkbank.py', 'python3'
        codex_root, codex_script = 'drei Verzeichnisse oberhalb dieser SKILL.md', 'WERKBANK-ORDNER/werkbank.py'
        hook_python, hook_script = 'python3', '${CLAUDE_PROJECT_DIR}/werkbank.py'
    else:
        root_display = script_display = str(ROOT)
        script_display = str(ROOT / 'werkbank.py')
        python_cmd = '"' + python_for_hooks() + '"'
        codex_root, codex_script = root_display, script_display
        hook_python, hook_script = python_for_hooks(), str(ROOT / 'werkbank.py')
    written = []
    claude_dir = into / '.claude/skills/uiworkbench'; claude_dir.mkdir(parents=True, exist_ok=True)
    (claude_dir / 'SKILL.md').write_text(claude_skill(root_display, python_cmd, script_display), encoding='utf-8')
    written.append(claude_dir / 'SKILL.md')
    codex_dir = into / '.agents/skills/uiworkbench'; codex_dir.mkdir(parents=True, exist_ok=True)
    (codex_dir / 'SKILL.md').write_text(codex_skill(codex_root, python_cmd, codex_script), encoding='utf-8')
    written.append(codex_dir / 'SKILL.md')
    settings_path = into / '.claude' / settings_file
    settings = {}
    if settings_path.exists():
        try: settings = json.loads(settings_path.read_text(encoding='utf-8'))
        except ValueError: raise SystemExit(f'{settings_path} ist kein gueltiges JSON; bitte zuerst reparieren.')
    merge_hooks(settings, hook_python, hook_script)
    settings_path.write_text(json.dumps(settings, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    written.append(settings_path)
    for path in written: print('geschrieben:', path)
    print('Claude: in einer Sitzung im Projekt /uiworkbench eingeben. Codex: $uiworkbench.')
    return 0


# ----------------------------------------------------------------------------- Befehle
def setup(args):
    if sys.version_info < (3, 10):
        raise SystemExit('Python 3.10 oder neuer wird benoetigt.')
    if not VENV_PY.exists():
        print('Erzeuge .venv ...', flush=True)
        if shutil.which('uv'):
            subprocess.check_call(['uv', 'venv', '--python', sys.executable, str(ROOT / '.venv')])
        else:
            subprocess.check_call([sys.executable, '-m', 'venv', str(ROOT / '.venv')])
    print('Installiere Playwright und pytest ...', flush=True)
    subprocess.check_call([str(VENV_PY), '-m', 'pip', 'install', '--quiet', '--upgrade', 'pip'] if not shutil.which('uv') else ['uv', 'pip', 'install', '--python', str(VENV_PY), '-r', str(ROOT / 'requirements.txt')])
    if not shutil.which('uv'):
        subprocess.check_call([str(VENV_PY), '-m', 'pip', 'install', '--quiet', '-r', str(ROOT / 'requirements.txt')])
    if not args.no_browser:
        print('Installiere Chromium fuer die HTML-Pruefung ...', flush=True)
        subprocess.check_call([str(VENV_PY), '-m', 'playwright', 'install', 'chromium'])
    if os.name == 'nt':
        # "python3" is not reliably on PATH on Windows: point the hooks at the venv interpreter.
        install(ROOT, 'settings.local.json')
    print('Fertig. Start: python3 werkbank.py start   |   in Claude: /uiworkbench   |   in Codex: $uiworkbench')
    return 0


def start(args):
    from werkbank.server import serve, DEFAULT_PORT
    port = args.port or DEFAULT_PORT
    if not args.no_browser:
        import threading, webbrowser
        threading.Timer(1.0, lambda: webbrowser.open(f'http://127.0.0.1:{port}/')).start()
    serve(port, args.data_dir)
    return 0


def session_token(url):
    with urllib.request.urlopen(url + '/api/session', timeout=5) as r: return json.load(r)['token']


def api(url, action, data=None):
    headers = {'Content-Type': 'application/json'}
    if data is not None: headers['X-Werkbank-Token'] = session_token(url)
    req = urllib.request.Request(url + '/api/agents/' + action, headers=headers, data=None if data is None else json.dumps(data).encode())
    with urllib.request.urlopen(req, timeout=10) as r: return json.load(r)


def status(args):
    url = args.url.rstrip('/')
    try: state = api(url, 'state')
    except OSError:
        print('Werkbank laeuft nicht auf', url); return 1
    print('Sitzungen:')
    for s in state['sessions']:
        flags = [k for k in ('online', 'verified', 'wake_armed', 'wake_verified', 'detached') if s.get(k)]
        print(f"  {s['name']:<20} {s['id'][:8]}  {s['transport']:<13} {' '.join(flags)}  gebunden: {', '.join(s.get('bound', [])) or '-'}")
    print('Bindungen:', {p: (b.get('session') or '-')[:8] for p, b in state['bindings'].items() if b.get('enabled')} or '-')
    print('Auftraege:')
    for job_id, j in sorted(state['jobs'].items(), key=lambda kv: kv[1].get('queued', '')):
        print(f"  {job_id:<40} {j['project']:<16} {j['state']:<12} {j.get('reason', '')}")
    return 0


def cancel(args):
    url = args.url.rstrip('/')
    state = api(url, 'state')
    jobs = [k for k, j in state['jobs'].items() if j['state'] in {'queued', 'working', 'question', 'review', 'conflict'}]
    targets = [args.job] if args.job else jobs
    if not targets:
        print('Kein offener Auftrag.'); return 0
    for job in targets:
        print(json.dumps(api(url, 'cancel', {'job': job}), ensure_ascii=False))
    return 0


def requeue(args):
    """Put a cancelled, interrupted, failed or conflicting message back into the queue."""
    url = args.url.rstrip('/')
    state = api(url, 'state')
    retryable = sorted((k for k, j in state['jobs'].items() if j['state'] in {'cancelled', 'interrupted', 'failed', 'conflict'}),
                       key=lambda k: state['jobs'][k].get('queued', ''))
    targets = [args.job] if args.job else retryable[-1:]
    if not targets:
        print('Kein abgebrochener Auftrag.'); return 0
    for job in targets:
        result = api(url, 'enqueue', {'message': job})
        print(f"{job}: {result['state']}")
    return 0


def doctor(args):
    import socket
    from werkbank.server import DEFAULT_PORT
    info = {'python': sys.version.split()[0], 'executable': sys.executable, 'root': str(ROOT), 'venv': VENV_PY.exists()}
    try:
        import playwright; info['playwright'] = getattr(playwright, '__version__', 'ok')
    except ImportError:
        info['playwright'] = 'fehlt (python3 werkbank.py setup)'
    with socket.socket() as s:
        s.settimeout(.3); info['port_' + str(DEFAULT_PORT) + '_listening'] = s.connect_ex(('127.0.0.1', DEFAULT_PORT)) == 0
    info['claude_settings'] = (ROOT / '.claude/settings.json').exists()
    info['projects'] = sorted(p.name for p in (ROOT / 'projects').iterdir() if p.is_dir()) if (ROOT / 'projects').is_dir() else []
    print(json.dumps(info, indent=2, ensure_ascii=False))
    return 0


def test(args):
    env = {**os.environ, 'PYTHONDONTWRITEBYTECODE': '1', 'PYTHONUTF8': '1'}
    return subprocess.call([sys.executable, '-B', '-m', 'pytest', '-q', '-p', 'no:cacheprovider', str(ROOT / 'tests'), *args.pytest_args], cwd=ROOT, env=env)


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] in {'terminal', 'hook', 'codex-watch', 'attach', 'detach'}:
        # Pass-through commands keep their own argument parsers.
        sys.path.insert(0, str(ROOT))
        if argv[0] == 'terminal':
            from werkbank.terminal import main as run
            try: return run(argv[1:]) or 0
            except (ValueError, RuntimeError, OSError) as exc:
                print(str(exc), file=sys.stderr); return 1
        if argv[0] == 'hook':
            from werkbank.wake import main as run
            return run()
        if argv[0] == 'codex-watch':
            from werkbank.codex_queue import main as run
            return run(argv[1:])
        from werkbank.uiworkbench import main as run
        return run(argv[1:] + (['--detach'] if argv[0] == 'detach' else []))
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest='command', required=True)
    p = sub.add_parser('setup', help='Umgebung einrichten'); p.add_argument('--no-browser', action='store_true'); p.set_defaults(run=setup)
    p = sub.add_parser('start', help='Dienst starten'); p.add_argument('--port', type=int); p.add_argument('--data-dir', type=Path); p.add_argument('--no-browser', action='store_true'); p.set_defaults(run=start)
    p = sub.add_parser('install', help='Skill und Hooks in einen Projektordner (--into) oder benutzerweit (--user) schreiben')
    p.add_argument('--into'); p.add_argument('--user', action='store_true', help='~/.claude und ~/.codex: gilt in jedem Ordner')
    p.add_argument('--settings-file', default='settings.local.json', choices=['settings.json', 'settings.local.json'])
    p.add_argument('--generic', action='store_true', help='Vorlagen mit Platzhaltern (fuer das Repository selbst)')
    p.set_defaults(run=lambda a: install_user() if a.user else (install(a.into, a.settings_file, a.generic) if a.into else ap.error('--into DIR oder --user angeben')))
    p = sub.add_parser('status', help='Sitzungen, Bindungen und Auftraege anzeigen'); p.add_argument('--url', default=DEFAULT_URL); p.set_defaults(run=status)
    p = sub.add_parser('cancel', help='offene Auftraege abbrechen'); p.add_argument('--job'); p.add_argument('--url', default=DEFAULT_URL); p.set_defaults(run=cancel)
    p = sub.add_parser('requeue', help='abgebrochenen Auftrag erneut einreihen (zuletzt gesendeten oder --job ID)'); p.add_argument('--job'); p.add_argument('--url', default=DEFAULT_URL); p.set_defaults(run=requeue)
    p = sub.add_parser('doctor', help='Umgebung pruefen'); p.set_defaults(run=doctor)
    p = sub.add_parser('test', help='Testsuite ausfuehren'); p.add_argument('pytest_args', nargs='*'); p.set_defaults(run=test)
    sub.add_parser('attach', help='bestehende Sitzung anbinden (siehe attach --help)')
    sub.add_parser('detach', help='bestehende Sitzung entkoppeln')
    sub.add_parser('terminal', help='Terminalbefehle der angebundenen Sitzung')
    args = ap.parse_args(argv)
    sys.path.insert(0, str(ROOT))
    return args.run(args)


if __name__ == '__main__':
    reexec_in_venv()
    raise SystemExit(main())
