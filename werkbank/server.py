"""Local HTTP service: UI, storage routes and the terminal attachment API. Loopback only."""
from __future__ import annotations

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import secrets
import socket
from urllib.parse import parse_qs, unquote, urlsplit

from .store import MAX_ASSET, MAX_BACKUP, MAX_DOCUMENT, Store, StoreError, encode
from .broker import Broker

ROOT = Path(__file__).resolve().parents[1]
UI = ROOT / 'ui'
PROJECTS = ROOT / 'projects'
DATA = ROOT / '.local/werkbank'
APP = 'ui-werkbank'
DEFAULT_PORT = 8119


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = os.name != 'nt'

    def server_bind(self):
        if os.name == 'nt':
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()

    def __init__(self, port=DEFAULT_PORT, directory=None, ui=UI, projects=PROJECTS, root=ROOT):
        self.ui = Path(ui)
        # Reserve the listener before migration/recovery touches any persistent jobs.
        # A second start on the same port must not interrupt the running instance.
        super().__init__(('127.0.0.1', port), Handler)
        try:
            self.store = Store(Path(directory or DATA), Path(projects))
            self.broker = Broker(self.store, root)
            self.token = secrets.token_urlsafe(32)
        except Exception:
            self.server_close()
            raise

    def server_close(self):
        super().server_close()
        if hasattr(self, 'store'):
            self.store.close()


class Handler(BaseHTTPRequestHandler):
    server_version = 'UIWerkbank/1'

    def log_message(self, format, *args):
        # Do not log submitted content or connection tokens.
        pass

    def reply(self, status, data=b'', mime='application/json; charset=utf-8', download=None, sandbox=False):
        if isinstance(data, (dict, list)):
            data = encode(data).encode('utf-8')
        if isinstance(data, str):
            data = data.encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', mime)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('Content-Security-Policy', "default-src 'self' data: blob:; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; connect-src 'self'; frame-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'self'")
        if sandbox:
            self.send_header('Content-Security-Policy', 'sandbox allow-scripts')
        if download:
            self.send_header('Content-Disposition', f'attachment; filename="{download}"')
        self.end_headers()
        if self.command != 'HEAD':
            self.wfile.write(data)

    def guard(self, write=False):
        port = self.server.server_port
        allowed = {f'127.0.0.1:{port}', f'localhost:{port}'}
        if self.headers.get('Host') not in allowed:
            raise StoreError('host', 'Nur der lokale Werkbank-Endpunkt ist erlaubt.', 403)
        origin = self.headers.get('Origin')
        if origin and origin not in {'http://' + host for host in allowed}:
            raise StoreError('origin', 'Anfrage stammt nicht aus der lokalen Werkbank.', 403)
        if write and not secrets.compare_digest(self.headers.get('X-Werkbank-Token', ''), self.server.token):
            raise StoreError('session', 'Verbindung wurde erneuert. Bitte noch einmal speichern.', 401)

    def body(self, limit):
        try:
            length = int(self.headers.get('Content-Length', '-1'))
        except ValueError:
            length = -1
        if length < 0 or length > limit or self.headers.get('Transfer-Encoding'):
            raise StoreError('body_size', 'Anfrage fehlt oder überschreitet die Größenbegrenzung.', 413)
        self.connection.settimeout(15)
        data = self.rfile.read(length)
        if len(data) != length:
            raise StoreError('incomplete_body', 'Anfrage wurde nicht vollständig übertragen.')
        return data

    def json_body(self):
        if self.headers.get_content_type() != 'application/json':
            raise StoreError('content_type', 'JSON wird erwartet.', 415)
        try:
            value = json.loads(self.body(MAX_DOCUMENT), parse_constant=lambda s: (_ for _ in ()).throw(ValueError(s)))
        except (ValueError, UnicodeError) as exc:
            raise StoreError('invalid_json', 'Anfrage enthält kein gültiges JSON.') from exc
        if not isinstance(value, dict):
            raise StoreError('invalid_json', 'Ein JSON-Objekt wird erwartet.')
        return value

    def do_GET(self):
        self.handle_request()

    def do_HEAD(self):
        self.handle_request()

    def do_POST(self):
        self.handle_request()

    def do_PUT(self):
        self.handle_request()

    def handle_request(self):
        try:
            uri = urlsplit(self.path)
            path = unquote(uri.path)
            terminal = path.startswith('/api/terminal/')
            self.guard(self.command in {'POST', 'PUT'} and not terminal)
            store = self.server.store
            broker = self.server.broker
            if terminal and self.command == 'POST':
                body = self.json_body()
                action = path.removeprefix('/api/terminal/')
                if action == 'register':
                    self.reply(200, broker.register(body.get('code'), body.get('identity'), body.get('transport', 'terminal-pull'))); return
                key = self.headers.get('X-Werkbank-Client', '')
                broker.authenticate(key, self.headers.get('X-Werkbank-Key', ''))
                if action == 'ready': result = broker.ready(key, body.get('challenge'), body.get('protocol'))
                elif action == 'next': result = broker.next(key, body.get('wake_marker'))
                elif action == 'wake': result = broker.wake(key, body.get('project'), body.get('after', ''), body.get('timeout', 20))
                elif action == 'event': result = broker.event(key, body.get('attempt'), body.get('kind'), body.get('text', ''), body.get('usage'))
                elif action == 'finish': result = broker.finish(key, body.get('attempt'), body.get('text'), body.get('html'), body.get('checks'))
                elif action == 'heartbeat': result = {'ok': True, 'boot': broker.boot}
                elif action == 'transport': result = broker.transport(key, body.get('mode'))
                else: raise StoreError('route', 'Unbekannte Terminalaktion.', 404)
                self.reply(200, result); return
            if path.startswith('/api/agents/'):
                action = path.removeprefix('/api/agents/')
                if self.command == 'GET' and action == 'state':
                    self.reply(200, broker.status()); return
                if self.command == 'GET' and action.startswith('candidate/'):
                    candidate = broker.candidate(action.removeprefix('candidate/'))
                    if candidate is None: raise StoreError('candidate', 'Kein HTML-Vorschlag.', 404)
                    self.reply(200, candidate, 'text/html; charset=utf-8', sandbox=True); return
                if self.command == 'POST':
                    body = self.json_body()
                    if action == 'pair': result = broker.pair(body.get('provider'), body.get('name'))
                    elif action == 'bind': result = broker.bind(body.get('project'), body.get('session'), body.get('auto', False), body.get('takeover', False))
                    elif action == 'detach': result = broker.detach(body.get('session'))
                    elif action == 'enqueue': result = broker.enqueue(body.get('message'))
                    elif action == 'cancel': result = broker.cancel(body.get('job'))
                    elif action == 'answer': result = broker.answer(body.get('job'), body.get('text'))
                    elif action == 'accept':
                        job = broker.status()['jobs'].get(body.get('job'), {})
                        candidate = broker.candidate(job.get('attempt', ''))
                        validation = None
                        if candidate is not None:
                            from .validate import validate
                            validation = validate(candidate)
                            if not validation['ok']:
                                self.reply(409, {'code': 'validation', 'message': 'Browserprüfung fehlgeschlagen. Vorschlag bleibt erhalten.', 'validation': validation}); return
                        result = broker.accept(body.get('job'), validation)
                    else: raise StoreError('route', 'Unbekannte Werkbankaktion.', 404)
                    self.reply(200, result); return
            if self.command in {'GET', 'HEAD'}:
                if path == '/api/session':
                    self.reply(200, {'token': self.server.token, 'schema': 1, 'app': APP, 'attach_protocol': 1,
                                     'native_wake_protocol': 1, 'project_root': str(broker.root)}); return
                if path == '/api/state':
                    raw = parse_qs(uri.query).get('since', ['-1'])[0]
                    state = store.state(int(raw)); terminals = broker.status()
                    ready = {s['id'] for s in terminals['sessions'] if s['online'] and s['verified']}
                    state['agent'] = {'connected': any(b.get('enabled') and b.get('session') in ready for b in terminals['bindings'].values()), 'protocol': 1}
                    self.reply(200, state); return
                if path == '/api/export':
                    self.reply(200, store.export(), 'application/zip', 'ui-werkbank.zip'); return
                if path.startswith('/api/assets/'):
                    asset = store.asset(path[len('/api/assets/'):])
                    if asset: self.reply(200, asset[1], asset[0]); return
                if path.startswith('/p/'):
                    version = store.version(path[1:])
                    if version: self.reply(200, version[0], 'text/html; charset=utf-8'); return
                public = {'/': ('index.html', 'text/html; charset=utf-8'),
                          '/index.html': ('index.html', 'text/html; charset=utf-8'),
                          '/local-adapter.js': ('local-adapter.js', 'text/javascript; charset=utf-8'),
                          '/sessions.js': ('sessions.js', 'text/javascript; charset=utf-8'),
                          '/vendor/html2canvas-1.4.1.min.js': ('vendor/html2canvas-1.4.1.min.js', 'text/javascript; charset=utf-8')}
                if path in public:
                    name, mime = public[path]
                    self.reply(200, (self.server.ui / name).read_bytes(), mime); return
                if path == '/favicon.ico':
                    self.reply(204, b'', 'image/x-icon'); return
            elif self.command == 'PUT' and path.startswith('/api/doc/'):
                result = store.save_document(path[len('/api/doc/'):], self.json_body())
                self.reply(200, result); return
            elif self.command == 'POST':
                if path == '/api/messages':
                    body = self.json_body()
                    if not isinstance(body.get('id'), str) or not isinstance(body.get('data'), dict):
                        raise StoreError('invalid_message', 'ID oder Nachrichteninhalt fehlt.')
                    if body['data'].get('reply_to'):
                        self.reply(200, broker.answer(body['data']['reply_to'], body['data'].get('text'), body['id'], body['data'])); return
                    self.reply(200, store.submit(body['id'], body['data'], dispatch=broker.enqueue)); return
                if path == '/api/assets':
                    self.reply(201, store.upload(self.body(MAX_ASSET))); return
                if path == '/api/import':
                    self.reply(200, store.import_backup(self.body(MAX_BACKUP))); return
                if path == '/api/sync':
                    self.reply(200, store.sync_sources()); return
            self.reply(404, {'code': 'not_found', 'message': 'Nicht gefunden.'})
        except StoreError as exc:
            self.reply(exc.status, {'code': exc.code, 'message': str(exc)})
        except (ValueError, TypeError, KeyError, UnicodeError):
            self.reply(400, {'code': 'invalid_request', 'message': 'Ungültige Anfrage.'})
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            pass
        except Exception:
            self.reply(500, {'code': 'storage_error', 'message': 'Lokaler Vorgang fehlgeschlagen; Inhalt wurde nicht bestätigt.'})


def serve(port=DEFAULT_PORT, directory=None, ui=UI, projects=PROJECTS):
    with Server(port, directory, ui, projects) as server:
        print(f'Werkbank: http://127.0.0.1:{server.server_port}/', flush=True)
        print(f'Speicher: {server.store.directory.resolve()}', flush=True)
        print(f'Projekte: {server.store.projects.resolve()}', flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


def main(argv=None):
    ap = argparse.ArgumentParser(description='Lokale UI-Werkbank (nur 127.0.0.1).')
    ap.add_argument('--port', type=int, default=DEFAULT_PORT)
    ap.add_argument('--data-dir', type=Path)
    ap.add_argument('--ui-dir', type=Path, default=UI)
    ap.add_argument('--projects-dir', type=Path, default=PROJECTS)
    args = ap.parse_args(argv)
    serve(args.port, args.data_dir, args.ui_dir, args.projects_dir)


if __name__ == '__main__':
    main()
