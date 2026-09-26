"""Tools invoked by an existing terminal agent. No CLI/LLM process is launched."""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import sys
import time
import urllib.request
import urllib.error
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_URL = 'http://127.0.0.1:8119'
VENV_PY = ROOT / '.venv' / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')


def interpreter():
    """The Python that has Playwright: the project's venv when present, else the running one."""
    return str(VENV_PY) if VENV_PY.exists() else sys.executable
BLANK = '''<!doctype html>
<html lang="de">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Neue Oberfläche</title>
</head>
<body>
</body>
</html>
'''


class Client:
    def __init__(self, profile=None, url=None):
        self.path = Path(profile or ROOT / '.local/terminal/default.json')
        self.data = json.loads(self.path.read_text(encoding='utf-8')) if self.path.exists() else {}
        self.url = (url or self.data.get('url') or DEFAULT_URL).rstrip('/')
        u = urlsplit(self.url)
        if u.scheme != 'http' or u.hostname not in {'127.0.0.1', 'localhost'} or u.username or u.password or u.path or u.query or u.fragment:
            raise ValueError('Only a local Werkbank HTTP address is supported')

    def call(self, action, body=None, timeout=15):
        request = urllib.request.Request(self.url + '/api/terminal/' + action,
                  data=json.dumps(body or {}, ensure_ascii=False).encode('utf-8'),
                  headers={'Content-Type': 'application/json', 'X-Werkbank-Client': self.data.get('id', ''),
                           'X-Werkbank-Key': self.data.get('secret', '')})
        try:
            with urllib.request.urlopen(request, timeout=timeout) as r: return json.load(r)
        except urllib.error.HTTPError as e:
            error = json.load(e)
            raise RuntimeError(error.get('code', 'error') + ': ' + error.get('message', '')) from e

    def register(self, code, identity, transport='terminal-pull'):
        if self.path.exists(): raise ValueError('Profile exists; choose a new --profile to preserve the other session')
        self.data = self.call('register', {'code': code, 'identity': identity, 'transport': transport})
        self.data['url'] = self.url
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open('x', encoding='utf-8') as f: json.dump(self.data, f, ensure_ascii=False, indent=2)
        return {k: v for k, v in self.data.items() if k != 'secret'}

    def prepare(self, result):
        if result.get('type') not in {'job', 'active'}: return result
        directory = ROOT / '.local/terminal/work' / result['attempt']['id']
        directory.mkdir(parents=True, exist_ok=True)
        def download(url, target):
            if not url.startswith('/'): url = '/' + url
            with urllib.request.urlopen(self.url + url, timeout=15) as r: body = r.read()
            if not target.exists(): target.write_bytes(body)
            return body
        ctx = result['context']
        if ctx.get('version'):
            body = download(ctx['version']['datei'], directory / 'base.html')
        else:
            # A project without any version starts from a blank page; the result becomes version 1.
            body = BLANK.encode('utf-8')
            if not (directory / 'base.html').exists(): (directory / 'base.html').write_bytes(body)
        if not (directory / 'result.html').exists(): (directory / 'result.html').write_bytes(body)
        for key, snapshot in ctx.get('snapshots', {}).items():
            asset = snapshot['bild']['asset']
            image = directory / (asset + '.jpg')
            download('/api/assets/' + asset, image)
            snapshot['local_image'] = str(image)
        (directory / 'context.full.json').write_text(json.dumps(ctx, ensure_ascii=False, indent=2), encoding='utf-8')
        compact = compact_context(ctx)
        (directory / 'context.json').write_text(json.dumps(compact, ensure_ascii=False, indent=2), encoding='utf-8')
        result['work_dir'] = str(directory)
        return {'type': result['type'], 'job': result['job'],
                'attempt': {k: result['attempt'][k] for k in ('id', 'state', 'provider')},
                'work_dir': str(directory), 'context_file': str(directory / 'context.json'),
                'result_file': str(directory / 'result.html'),
                'message': ctx['message']['text'], 'html_bytes': len(body), 'base_version': (ctx.get('version') or {}).get('nr'),
                'instructions': ('Lies context.json und referenzierte Bilder. Suche mit rg/Grep gezielt in result.html; grosse HTML-Dateien '
                                 'nicht komplett in den Modellkontext laden. Vollstaendiger Kontext bei Bedarf: context.full.json. '
                                 + ('Das Projekt hat noch keine Version: result.html ist eine leere Seite, dein Ergebnis wird Version 1.' if not ctx.get('version') else ''))}

    def once(self, wake_marker=None):
        result = self.call('next', {'wake_marker': wake_marker} if wake_marker else {})
        if result['type'] == 'active' and result['job']['state'] in {'review', 'question'}:
            return {'type': 'waiting', 'state': result['job']['state'], 'job_id': result['job']['id'],
                    'attempt_id': result['attempt']['id']}
        return self.prepare(result)

    def replaced(self):
        """True once another session holds the chat or this session was detached."""
        with urllib.request.urlopen(self.url + '/api/agents/state', timeout=15) as response:
            state = json.load(response)
        me = next((s for s in state['sessions'] if s['id'] == self.data.get('id')), None)
        if me is None or me.get('detached'): return True
        others = any(b.get('enabled') and b.get('session') not in (None, self.data.get('id')) for b in state['bindings'].values())
        return bool(others and not me.get('bound'))

    def next(self, timeout=0):
        end = time.monotonic() + timeout if timeout else None
        waiting_contact = 0
        while True:
            if self.replaced():
                return {'type': 'detached', 'instructions': 'Diese Sitzung wurde entkoppelt oder ersetzt. Abruf beenden.'}
            result = self.call('next')
            if result['type'] == 'active' and result['job']['state'] in {'review', 'question'}:
                state = result['job']['state']
                if state == 'question' and time.monotonic() - waiting_contact >= 30:
                    self.call('event', {'attempt': result['attempt']['id'], 'kind': 'heartbeat'})
                    waiting_contact = time.monotonic()
                if end is not None and time.monotonic() >= end:
                    return {'type': 'waiting', 'state': state, 'job_id': result['job']['id'],
                            'attempt_id': result['attempt']['id'],
                            'instructions': 'Wartet auf den Benutzer. Kein neuer Auftrag; weiter mit next warten.'}
                time.sleep(min(1, max(0, end - time.monotonic())) if end is not None else 1)
                continue
            if result['type'] != 'idle': return self.prepare(result)
            if end is not None and time.monotonic() >= end: return result
            time.sleep(1)


def compact_context(ctx):
    """Keep all semantic values; only omit previews and reference identical state."""
    def without_previews(value):
        if isinstance(value, dict):
            return {k: without_previews(v) for k, v in value.items() if not (k == 'thumb' and isinstance(v, str) and v.startswith('data:image/'))}
        if isinstance(value, list): return [without_previews(v) for v in value]
        return value
    result = without_previews(ctx)
    for snapshot in result.get('snapshots', {}).values():
        if snapshot.get('zustand') == result.get('message', {}).get('zustand') and 'zustand' in snapshot:
            del snapshot['zustand']; snapshot['zustand_ref'] = '#/message/zustand'
    result['full_context_file'] = 'context.full.json'
    return result


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--profile', type=Path)
    ap.add_argument('--url')
    sub = ap.add_subparsers(dest='command', required=True)
    connect = sub.add_parser('connect'); connect.add_argument('--pair', required=True); connect.add_argument('--identity', required=True)
    ready = sub.add_parser('ready'); ready.add_argument('--challenge', required=True); ready.add_argument('--protocol', type=int, default=1)
    nxt = sub.add_parser('next'); nxt.add_argument('--timeout', type=float, default=0)
    nxt.add_argument('--once', action='store_true'); nxt.add_argument('--wake-marker')
    event = sub.add_parser('event'); event.add_argument('--attempt', required=True)
    event.add_argument('--kind', choices=['heartbeat', 'progress', 'question', 'failed'], required=True); event.add_argument('--text', default='')
    finish = sub.add_parser('finish'); finish.add_argument('--attempt', required=True); finish.add_argument('--text', required=True)
    finish.add_argument('--html', type=Path); finish.add_argument('--checks', type=Path)
    sub.add_parser('heartbeat')
    args = ap.parse_args(argv); client = Client(args.profile, args.url)
    if args.command == 'connect': result = client.register(args.pair, args.identity)
    elif args.command == 'ready': result = client.call('ready', {'challenge': args.challenge, 'protocol': args.protocol})
    elif args.command == 'next': result = client.once(args.wake_marker) if args.once else client.next(args.timeout)
    elif args.command == 'event': result = client.call('event', {'attempt': args.attempt, 'kind': args.kind, 'text': args.text})
    elif args.command == 'finish':
        result = client.call('finish', {'attempt': args.attempt, 'text': args.text,
            'html': args.html.read_text(encoding='utf-8') if args.html else None,
            'checks': json.loads(args.checks.read_text(encoding='utf-8')) if args.checks else []}, timeout=120)
    else: result = client.call('heartbeat')
    print(json.dumps(result, ensure_ascii=True, indent=2), flush=True)


if __name__ == '__main__':
    try: main()
    except (ValueError, RuntimeError, OSError) as exc:
        print(str(exc), file=sys.stderr); raise SystemExit(1)
