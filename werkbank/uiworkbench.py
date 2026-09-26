"""One entry point for an existing session; never starts an agent runtime."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit
import webbrowser

from .terminal import Client, ROOT, DEFAULT_URL, interpreter

APP = 'ui-werkbank'
IDENTITY_VARIABLES = {'claude': ('CLAUDE_CODE_SESSION_ID', 'CLAUDE_SESSION_ID'), 'codex': ('CODEX_THREAD_ID',)}


def request(url, path, data=None, token=None):
    headers = {'Content-Type': 'application/json'}
    if token:
        headers['X-Werkbank-Token'] = token
    req = urllib.request.Request(url + path, headers=headers,
        data=None if data is None else json.dumps(data).encode('utf-8'))
    try:
        with urllib.request.urlopen(req, timeout=5) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        error = json.load(exc)
        raise RuntimeError(error.get('code', 'http') + ': ' + error.get('message', '')) from exc


def service(url, root=ROOT, start=True):
    """Return (session, started). Starts the local web service only when nothing answers."""
    # URL validation also prevents credentials being sent outside localhost.
    Client(root / '.local/terminal/unused.json', url)
    started = False
    try:
        session = request(url, '/api/session')
    except urllib.error.URLError:
        if not start:
            raise RuntimeError('Werkbank nicht erreichbar. Im Werkbank-Ordner "python3 werkbank.py start" ausfuehren.')
        port = urlsplit(url).port or 80
        logs = root / '.local/terminal/service'
        logs.mkdir(parents=True, exist_ok=True)
        options = {'creationflags': subprocess.CREATE_NO_WINDOW} if os.name == 'nt' else {'start_new_session': True}
        with (logs / 'stdout.log').open('ab') as out, (logs / 'stderr.log').open('ab') as err:
            process = subprocess.Popen([interpreter(), '-B', '-m', 'werkbank.server', '--port', str(port)],
                cwd=root, stdin=subprocess.DEVNULL, stdout=out, stderr=err, **options)
        deadline = time.monotonic() + 10
        while True:
            try:
                session = request(url, '/api/session')
                started = True
                break
            except urllib.error.URLError:
                if time.monotonic() > deadline or process.poll() is not None:
                    raise RuntimeError('Werkbank konnte nicht starten; siehe .local/terminal/service im Werkbank-Ordner.')
                time.sleep(.2)
    if session.get('app') != APP or session.get('attach_protocol') != 1:
        raise RuntimeError('Auf diesem Port antwortet ein anderer Dienst. Anderen Port waehlen (--url) oder den Dienst beenden.')
    if Path(session.get('project_root', '')).resolve() != Path(root).resolve():
        raise RuntimeError('Auf diesem Port laeuft eine Werkbank aus einem anderen Ordner: ' + session.get('project_root', ''))
    return session, started


def session_identity(provider, identity=None):
    for name in () if identity else IDENTITY_VARIABLES[provider]:
        identity = os.environ.get(name)
        if identity: break
    if not identity or not identity.strip() or len(identity) > 200 or '${' in identity:
        raise ValueError('Aktuelle Sitzungs-ID fehlt (' + '/'.join(IDENTITY_VARIABLES[provider]) + '). Keine fremde oder erfundene Kennung verwenden.')
    return identity.strip()


def choose_project(docs, bindings, project=None):
    projects = sorted(p.split('/')[1] for p in docs if p.startswith('projekte/'))
    if project:
        if project not in projects:
            raise ValueError('Projekt "' + project + '" fehlt. Vorhanden: ' + (', '.join(projects) or 'keins'))
        return project
    chat = docs.get('ui/chat', {}).get('value', {}).get('p')
    if chat in projects:
        return chat
    bound = sorted(p for p, b in bindings.items() if b.get('enabled') and p in projects)
    if bound:
        return bound[0]
    return projects[0] if projects else None


def find_profile(root, provider, identity, url):
    for path in sorted((root / '.local/terminal').glob('*.json')):
        try:
            data = json.loads(path.read_text(encoding='utf-8'))
        except (ValueError, OSError):
            continue
        if (data.get('provider'), data.get('identity'), data.get('url')) == (provider, identity, url):
            return Client(path, url)
    return None


def attach(provider, identity=None, project=None, url=DEFAULT_URL, root=ROOT, start=True, detach=False, open_browser=False):
    identity = session_identity(provider, identity)
    url = url.rstrip('/')
    root = Path(root)
    session, started = service(url, root, start)
    state = request(url, '/api/agents/state')
    docs = request(url, '/api/state')['documents']
    match = find_profile(root, provider, identity, url)
    if detach:
        if match is None:
            return {'connected': False, 'note': 'Diese Sitzung war nicht angebunden.'}
        request(url, '/api/agents/detach', {'session': match.data.get('id')}, session['token'])
        from .wake import pointer_path
        pointer_path(root, identity).unlink(missing_ok=True)
        return {'connected': False, 'session': match.data.get('id'), 'note': 'Entkoppelt. Der Wartehook beendet sich.'}
    project = choose_project(docs, state['bindings'], project)
    codex_process = None
    if provider == 'codex' and session.get('native_wake_protocol') == 1:
        from .codex_queue import prepare
        codex_process = prepare(identity)
    if match is None:
        name = ('Codex' if provider == 'codex' else 'Claude') + ' · ' + identity[:8]
        pair = request(url, '/api/agents/pair', {'provider': provider, 'name': name}, session['token'])
        key = hashlib.sha256((url + '|' + provider + '|' + identity).encode()).hexdigest()[:24]
        match = Client(root / '.local/terminal' / (provider + '-' + key + '.json'), url)
        match.register(pair['code'], identity)
    match.call('ready', {'challenge': match.data['challenge'], 'protocol': 1})
    if project:
        request(url, '/api/agents/bind', {'project': project, 'session': match.data['id'], 'auto': True, 'takeover': True}, session['token'])
    match.data['project'] = project
    # The profile belongs to this identity; atomic replacement preserves its credentials.
    temporary = match.path.with_suffix('.tmp')
    temporary.write_text(json.dumps(match.data, ensure_ascii=False, indent=2), encoding='utf-8')
    temporary.replace(match.path)
    if started or open_browser:
        try: webbrowser.open(url + '/')
        except Exception: pass
    result = {'connected': True, 'project': project, 'profile': str(match.path), 'session': match.data['id'],
              'url': url, 'browser': url + '/', 'service_started': started}
    if not project:
        result['hint'] = 'Noch kein Projekt: im Browser oben im Chat "+ Neues Projekt" anlegen; die Sitzung wird dann automatisch daran gebunden.'
    if provider == 'claude' and session.get('native_wake_protocol') == 1:
        from .wake import enable
        enable(match, root)
        return {**result, 'transport': 'claude-hook',
                'note': 'Turn jetzt beenden. Der Projekt-Stop-Hook wartet ohne Modellaufrufe und weckt bei neuen Auftraegen. Dem Benutzer die Browser-Adresse nennen.'}
    if provider == 'codex' and session.get('native_wake_protocol') == 1:
        from .codex_queue import enable
        enable(match, root, prepared=codex_process)
        return {**result, 'transport': 'codex-queue',
                'note': 'Turn beenden. Neue Nachrichten werden mit codex queue an genau diese bestehende Sitzung zugestellt. Kein next-Polling. Dem Benutzer die Browser-Adresse nennen.'}
    return {**result, 'transport': 'terminal-pull',
            'next': [interpreter(), '-B', str(root / 'werkbank.py'), 'terminal', '--profile', str(match.path), 'next', '--timeout', '50'],
            'note': 'Abrufmodus: Diese Sitzung muss next weiter abwarten. Keine automatische Weckfunktion behaupten.'}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--provider', choices=['claude', 'codex'], required=True)
    parser.add_argument('--identity')
    parser.add_argument('--project')
    parser.add_argument('--url', default=DEFAULT_URL)
    parser.add_argument('--no-start', action='store_true', help='Den lokalen Webdienst nicht automatisch starten')
    parser.add_argument('--open', action='store_true', help='Browser auch dann oeffnen, wenn der Dienst schon lief')
    parser.add_argument('--detach', action='store_true')
    args = parser.parse_args(argv)
    try:
        print(json.dumps(attach(args.provider, args.identity, args.project, args.url, ROOT, not args.no_start, args.detach, args.open),
                         ensure_ascii=True, indent=2))
    except (ValueError, RuntimeError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
