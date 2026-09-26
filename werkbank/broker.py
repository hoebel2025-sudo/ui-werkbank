"""Cooperative attachment of existing terminal sessions. Never starts or resumes an agent."""
from __future__ import annotations

import hashlib
import hmac
import json
from pathlib import Path
import secrets
import time
import uuid

from .store import StoreError, encode, digest, utc, MAX_DOCUMENT, document_key, USER

PROTOCOL = 1
LIVE_SECONDS = 45
ACTIVE = {'working', 'question', 'review'}
TRANSPORTS = {'terminal-pull', 'claude-hook', 'codex-queue'}
INSTRUCTIONS = '''Du bist die bereits geöffnete Terminal-Sitzung. Arbeite für Werkbank-Aufträge nur an der
Oberfläche des angegebenen Projekts. Bestehende Versionen und die Werkbank selbst bleiben erhalten.
Lies zuerst docs/AGENT.md im Werkbank-Ordner. Bestätige die erhaltene Challenge mit ready;
erst diese Rückmeldung gilt als Nachrichtenprobe. next liefert genau einen Auftrag mit
Versuch-ID, Ausgangsversion und Dateihash. Bearbeite ausschließlich result.html im
gelieferten Arbeitsordner; nutze Referenzen und Bilder dort. Stelle Rückfragen mit question,
melde Fortschritt mit progress, antworte mit finish. Antworten erscheinen direkt im Chat.
HTML wird automatisch im Browser geprüft und als neue Version angezeigt.
Bei Abbruch oder Entkopplung stoppe die Bearbeitung dieses Auftrags. Schreibe niemals direkt
in projects/*/v/*.html oder in die Datenbank. Nach finish/question den Turn beenden;
die Anbindung weckt bei neuer Arbeit. Nur im terminal-pull-Modus weiter mit next warten. Ein next
Timeout bedeutet keine neue Aufgabe. Terminalbefehle und Freigaben bleiben unter Kontrolle
dieser Sitzung. Keine neue Claude-/Codex-Sitzung und keinen weiteren Agenten starten.'''


def ident():
    return uuid.uuid4().hex


def require_text(value, name, maximum=20000):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise StoreError('invalid_field', name + ' fehlt oder ist zu lang.')
    return value


class Broker:
    def __init__(self, store, project_root=None):
        self.store = store
        self.root = Path(project_root or Path(__file__).resolve().parents[1]).resolve()
        self.pairs = {}
        self.live = {}
        self.wakes = {}
        self.boot = ident()
        self.recover()

    def docs(self):
        return self.store._docs()

    def put(self, path, value):
        self.store._put(path, value)

    def clients(self):
        return {i: json.loads(d) for i, d in self.store.db.execute('SELECT id,data FROM terminal_clients')}

    def client_put(self, key, data):
        self.store.db.execute('INSERT INTO terminal_clients VALUES(?,?) ON CONFLICT(id) DO UPDATE SET data=excluded.data', (key, encode(data)))

    def recover(self):
        with self.store.lock, self.store.db:
            for path, doc in self.docs().items():
                if path.startswith('auftraege/') and doc.get('state') in {'working', 'question'}:
                    doc.update(state='interrupted', reason='Werkbank wurde neu gestartet; Ergebnisannahme gesperrt.')
                    self.put(path, doc)
                    self._close_attempt(doc, 'interrupted')
                if path.startswith('auftraege/') and doc.get('state') == 'review':
                    # A proposal that was never taken over would block its project forever.
                    doc.update(state='conflict', reason='Ergebnis lag beim Neustart ungeprüft vor; Auftrag erneut senden.')
                    self.put(path, doc)
                    self._close_attempt(doc, 'conflict')
                if path.startswith('bindungen/') and not doc.get('auto'):
                    doc['enabled'] = False
                    self.put(path, doc)
            self.store._bump()
        self.materialize()

    def _close_attempt(self, job, state):
        path = 'versuche/' + str(job.get('attempt'))
        attempt = self.docs().get(path)
        if attempt:
            attempt.update(state=state, ended=utc())
            self.put(path, attempt)

    def _expire(self):
        for path, job in self.docs().items():
            if path.startswith('auftraege/') and job.get('state') in {'working', 'question'}:
                if time.time() - job.get('heartbeat', 0) > 1800:
                    job.update(state='interrupted', reason='Bearbeitung nicht mehr bestätigt; keine automatische Wiederholung.')
                    self.put(path, job)
                    self._close_attempt(job, 'interrupted')
                    self.store._bump()

    def _bindings(self, docs=None):
        docs = docs if docs is not None else self.docs()
        return {p.split('/')[1]: d for p, d in docs.items() if p.startswith('bindungen/')}

    def status(self):
        with self.store.lock, self.store.db:
            self._expire()
            docs = self.docs()
            bindings = self._bindings(docs)
            sessions = []
            for key, c in self.clients().items():
                live = self.live.get(key, {})
                sessions.append({'id': key, 'provider': c['provider'], 'name': c['name'],
                                 'identity': c.get('identity', ''), 'transport': c['transport'],
                                 'verified': bool(live.get('verified')), 'online': time.time() - live.get('seen', 0) < LIVE_SECONDS,
                                 'wake_armed': time.time() - live.get('wake_seen', 0) < LIVE_SECONDS,
                                 'wake_verified': bool(c.get('wake_verified')), 'detached': bool(c.get('detached')),
                                 'bound': sorted(p for p, b in bindings.items() if b.get('enabled') and b.get('session') == key),
                                 'last_wake': self.wakes.get(key, {}).get('receipt'),
                                 'protocol': PROTOCOL, 'registered': c['registered']})
            jobs = {}
            for p, d in docs.items():
                if not p.startswith('auftraege/'): continue
                attempt = docs.get('versuche/' + str(d.get('attempt')), {})
                proposal = attempt.get('proposal', {})
                jobs[p.split('/')[1]] = {**d, 'proposal_text': proposal.get('text'),
                    'has_html': proposal.get('html') is not None, 'agent_checks': proposal.get('checks', []),
                    'events': attempt.get('events', [])[-5:], 'usage': attempt.get('usage'),
                    'publication': attempt.get('publication')}
            return {'protocol': PROTOCOL, 'boot': self.boot, 'sessions': sessions, 'bindings': bindings, 'jobs': jobs}

    def pair(self, provider, name):
        if provider not in {'claude', 'codex'}:
            raise StoreError('provider', 'Claude oder Codex auswählen.')
        require_text(name, 'Sitzungsname', 100)
        with self.store.lock:
            self.pairs = {k: v for k, v in self.pairs.items() if v['expires'] > time.time()}
            code = secrets.token_hex(5)
            self.pairs[code] = {'provider': provider, 'name': name, 'expires': time.time() + 600}
        return {'code': code, 'expires_in': 600, 'project_root': str(self.root), 'protocol': PROTOCOL}

    def register(self, code, identity='', transport='terminal-pull'):
        if transport not in TRANSPORTS:
            raise StoreError('transport', 'Unbekannter Nachrichtenweg.')
        with self.store.lock, self.store.db:
            pair = self.pairs.get(code)
            if not pair or pair['expires'] < time.time():
                raise StoreError('pair', 'Anmeldecode ungültig oder abgelaufen.', 403)
            require_text(identity, 'Kennung der bestehenden Sitzung', 200)
            key, secret, challenge = ident(), secrets.token_urlsafe(32), secrets.token_hex(12)
            c = {**pair, 'identity': identity, 'transport': transport, 'registered': utc(),
                 'secret_hash': digest(secret.encode()), 'challenge': challenge}
            del c['expires']
            self.client_put(key, c)
            self.live[key] = {'seen': time.time(), 'verified': False}
            del self.pairs[code]
            return {'id': key, 'secret': secret, 'challenge': challenge, 'protocol': PROTOCOL,
                    'provider': c['provider'], 'identity': identity, 'instructions': INSTRUCTIONS, 'project_root': str(self.root), 'boot': self.boot}

    def transport(self, key, mode):
        with self.store.lock, self.store.db:
            c = self.clients()[key]
            allowed = {'terminal-pull', 'claude-hook' if c['provider'] == 'claude' else 'codex-queue'}
            if mode not in allowed: raise StoreError('transport', 'Nachrichtenweg passt nicht zum Anbieter.')
            c['transport'] = mode; self.client_put(key, c)
            return {'ok': True}

    def authenticate(self, key, secret):
        with self.store.lock:
            c = self.clients().get(key)
            if not c or not isinstance(secret, str) or not hmac.compare_digest(c['secret_hash'], digest(secret.encode())):
                raise StoreError('terminal_auth', 'Sitzung nicht angemeldet.', 401)
            live = self.live.setdefault(key, {'verified': False})
            live['seen'] = time.time()
            return c

    def ready(self, key, challenge, protocol):
        with self.store.lock, self.store.db:
            c = self.clients()[key]
            if protocol != PROTOCOL or not hmac.compare_digest(str(challenge), c['challenge']):
                raise StoreError('handshake', 'Nachrichtenprobe oder Protokollversion stimmt nicht.', 409)
            self.live.setdefault(key, {}).update(seen=time.time(), verified=True)
            if c.get('detached'):
                # An explicit re-attach revives the session; bindings are decided separately.
                c['detached'] = False; self.client_put(key, c)
            self.store.changed.notify_all()
            return {'ready': True, 'boot': self.boot, 'instructions': INSTRUCTIONS}

    def _cancel_project_jobs(self, docs, project, reason):
        for path, job in docs.items():
            if path.startswith('auftraege/') and job.get('project') == project and job.get('state') in ACTIVE | {'queued'}:
                job.update(state='cancelled', reason=reason)
                self._close_attempt(job, 'cancelled'); self.put(path, job)

    def bind(self, project, session, auto=False, takeover=False):
        """Select the session that receives this project's chat. ``takeover`` also moves every
        other enabled binding to the same session, replacing and detaching previous sessions."""
        with self.store.lock, self.store.db:
            docs = self.docs()
            if 'projekte/' + str(project) not in docs: raise StoreError('project', 'Projekt fehlt.')
            previous = docs.get('bindungen/' + project, {})
            same_session = session is not None and previous.get('session') == session
            if session is not None:
                live = self.live.get(session, {})
                if session not in self.clients() or not live.get('verified') or time.time() - live.get('seen', 0) > LIVE_SECONDS:
                    raise StoreError('not_ready', 'Sitzung muss die Nachrichtenprobe bestätigen und erreichbar sein.', 409)
            if not same_session and previous.get('session'):
                self._cancel_project_jobs(docs, project, 'Sitzung gewechselt. Dieser Auftrag wird nicht weitergegeben.')
            self.put('bindungen/' + project, {'session': session, 'enabled': session is not None, 'auto': bool(auto)})
            if takeover and session is not None:
                for other, binding in self._bindings(docs).items():
                    if other != project and binding.get('enabled') and binding.get('session') != session:
                        self._cancel_project_jobs(docs, other, 'Sitzung gewechselt. Dieser Auftrag wird nicht weitergegeben.')
                        self.put('bindungen/' + other, {'session': session, 'enabled': True, 'auto': True})
            self._settle_detached(previous.get('session'))
            self.store._bump()
        return {'ok': True}

    def _settle_detached(self, key):
        """A session that lost its last binding while another session holds one is replaced."""
        if not key: return
        clients = self.clients()
        if key not in clients: return
        bindings = self._bindings()
        mine = [p for p, b in bindings.items() if b.get('enabled') and b.get('session') == key]
        others = any(b.get('enabled') and b.get('session') not in (None, key) for b in bindings.values())
        if not mine and others and not clients[key].get('detached'):
            clients[key]['detached'] = True; self.client_put(key, clients[key])

    def detach(self, session):
        """Explicitly release a session: unbind its projects and stop waking it."""
        with self.store.lock, self.store.db:
            docs = self.docs()
            for project, binding in self._bindings(docs).items():
                if binding.get('session') == session and binding.get('enabled'):
                    self._cancel_project_jobs(docs, project, 'Sitzung entkoppelt. Dieser Auftrag wird nicht weitergegeben.')
                    self.put('bindungen/' + project, {'session': None, 'enabled': False, 'auto': False})
            c = self.clients().get(session)
            if c:
                c['detached'] = True; self.client_put(session, c)
            self.store._bump()
        return {'ok': True}

    def _latest(self, docs, project):
        versions = [d for p, d in docs.items() if p.startswith('versionen/') and d['projekt'] == project]
        return max(versions, key=lambda d: d['nr']) if versions else None

    def enqueue(self, message_id):
        with self.store.lock, self.store.db:
            docs = self.docs()
            m = docs.get('nachrichten/' + message_id)
            if not m or m.get('rolle') != USER: raise StoreError('message', 'Benutzernachricht fehlt.')
            old = docs.get('auftraege/' + message_id)
            if old and old['state'] not in {'interrupted', 'failed', 'cancelled', 'conflict'}:
                return old
            project = m['projekt']
            number = m.get('version')
            v = docs.get(f'versionen/{project}~{number:03}') if type(number) is int else None
            if number is not None and not v:
                raise StoreError('version', 'Auftrag braucht eine gespeicherte Ausgangsversion.')
            latest = self._latest(docs, project)
            job = {'id': message_id, 'project': project, 'base': number if v else None, 'base_hash': v['sha256'] if v else None,
                   'head': latest['nr'] if latest else 0, 'state': 'queued', 'queued': utc(), 'attempt': None, 'message': message_id}
            self.put('auftraege/' + message_id, job); self.store._bump()
            return job

    def cancel(self, job_id):
        with self.store.lock, self.store.db:
            job = self.docs().get('auftraege/' + job_id)
            if not job: raise StoreError('job', 'Auftrag fehlt.')
            if job['state'] == 'done': raise StoreError('finished', 'Fertige Ergebnisse werden nicht zurückgenommen.', 409)
            job.update(state='cancelled', reason='Ergebnisannahme gesperrt. Laufende Terminalaktionen müssen dort stoppen.')
            self._close_attempt(job, 'cancelled'); self.put('auftraege/' + job_id, job); self.store._bump()
            return job

    def _owned(self, key, attempt_id):
        docs = self.docs()
        a = docs.get('versuche/' + str(attempt_id))
        job = docs.get('auftraege/' + a['job']) if a else None
        if not job or a['session'] != key or job.get('attempt') != attempt_id or job['state'] not in ACTIVE:
            raise StoreError('stale_attempt', 'Auftrag ist nicht mehr für diese Bearbeitung freigegeben.', 409)
        binding = docs.get('bindungen/' + job['project'], {})
        if not binding.get('enabled') or binding.get('session') != key:
            raise StoreError('detached', 'Sitzung ist entkoppelt.', 409)
        return job, a

    def next(self, key, wake_marker=None):
        with self.store.lock, self.store.db:
            if wake_marker is not None:
                delivery = self.wakes.get(key, {})
                if wake_marker != delivery.get('marker'):
                    raise StoreError('wake_marker', 'Wecksignal ist abgelaufen. Erneut anbinden.', 409)
                delivery['receipt'] = {'marker': wake_marker, 'sent': delivery['sent'], 'received': utc(),
                                       'seconds': round(time.time() - delivery['time'], 4)}
                c = self.clients()[key]
                c['wake_verified'] = True
                self.client_put(key, c)
                self.live.setdefault(key, {})['wake_seen'] = 0
            if not self.live.get(key, {}).get('verified'):
                return {'type': 'handshake', 'challenge': self.clients()[key]['challenge'], 'protocol': PROTOCOL, 'instructions': INSTRUCTIONS}
            self._expire()
            docs = self.docs()
            for p, j in docs.items():
                if p.startswith('auftraege/') and j.get('state') in ACTIVE:
                    a = docs.get('versuche/' + str(j.get('attempt')), {})
                    if a.get('session') == key: return {'type': 'active', 'job': j, 'attempt': a, 'context': self.context(j)}
            for p, j in sorted(docs.items(), key=lambda item: item[1].get('queued', '')):
                if not p.startswith('auftraege/') or j.get('state') != 'queued': continue
                binding = docs.get('bindungen/' + j['project'], {})
                if not binding.get('enabled') or binding.get('session') != key: continue
                if any(n.startswith('auftraege/') and d.get('project') == j['project'] and d.get('state') in ACTIVE for n, d in docs.items()): continue
                attempt = ident()
                if docs['nachrichten/' + j['message']].get('follow_latest'):
                    latest = self._latest(docs, j['project'])
                    if latest: j.update(base=latest['nr'], head=latest['nr'], base_hash=latest['sha256'])
                a = {'id': attempt, 'job': j['id'], 'session': key, 'provider': self.clients()[key]['provider'],
                     'state': 'working', 'started': utc(), 'events': [], 'usage': None}
                j.update(state='working', attempt=attempt, heartbeat=time.time())
                self.put(p, j); self.put('versuche/' + attempt, a); self.store._bump()
                return {'type': 'job', 'job': j, 'attempt': a, 'context': self.context(j)}
            return {'type': 'idle'}

    def wake(self, key, project=None, after='', timeout=20):
        """Wait for eligible work without claiming it or starting an agent/model turn.

        The session is woken for every project bound to it. Without any binding it keeps
        waiting (a project may still be created), unless it was replaced or detached."""
        if not isinstance(after, str) or len(after) > 500 or type(timeout) not in (int, float) or not 0 <= timeout <= 25:
            raise StoreError('wake_request', 'Ungueltige Warteanfrage.')
        deadline = time.monotonic() + timeout
        with self.store.changed:
            c = self.clients()[key]
            if c['transport'] not in {'claude-hook', 'codex-queue'}:
                raise StoreError('transport', 'Dieser Weckdienst gilt nur fuer angebundene Sitzungen (claude-hook/codex-queue).')
            if not self.live.get(key, {}).get('verified'):
                return {'type': 'handshake'}
            self.live[key]['wake_seen'] = time.time()
            while not self.store.closed:
                docs = self.docs()
                bindings = self._bindings(docs)
                mine = [p for p, b in bindings.items() if b.get('enabled') and b.get('session') == key]
                others = any(b.get('enabled') and b.get('session') not in (None, key) for b in bindings.values())
                if self.clients()[key].get('detached') or (not mine and others):
                    self.live[key]['wake_seen'] = 0
                    return {'type': 'detached'}
                active = [j for p, j in docs.items() if p.startswith('auftraege/') and j.get('state') in ACTIVE
                          and docs.get('versuche/' + str(j.get('attempt')), {}).get('session') == key]
                candidate, marker = None, ''
                if active:
                    j = active[0]
                    if j['state'] == 'question':
                        # Waiting for a human is healthy while this hook is alive.
                        with self.store.db:
                            j['heartbeat'] = time.time(); self.put('auftraege/' + j['id'], j)
                    if j['state'] == 'working' and j.get('answer'):
                        events = docs['versuche/' + j['attempt']].get('events', [])
                        answer = next((e for e in reversed(events) if e['kind'] == 'answer'), {})
                        marker = 'answer:' + j['attempt'] + ':' + answer.get('t', '')
                        candidate = j
                else:
                    for p, j in sorted(docs.items(), key=lambda item: item[1].get('queued', '')):
                        if p.startswith('auftraege/') and j.get('state') == 'queued' and j['project'] in mine:
                            if not any(n.startswith('auftraege/') and a.get('project') == j['project'] and a.get('state') in ACTIVE for n, a in docs.items()):
                                candidate, marker = j, 'job:' + j['id'] + ':' + j['queued']
                                break
                if candidate and marker != after:
                    self.wakes[key] = {'marker': marker, 'sent': utc(), 'time': time.time()}
                    return {'type': 'wake', 'marker': marker, 'job': candidate['id'], 'state': candidate['state']}
                remaining = deadline - time.monotonic()
                if remaining <= 0: return {'type': 'idle'}
                self.store.changed.wait(remaining)
            return {'type': 'closed'}

    def context(self, job):
        docs = self.docs()
        m = docs['nachrichten/' + job['message']]
        v = docs.get(f"versionen/{job['project']}~{job['base']:03}") if job.get('base') is not None else None
        answer = docs.get('nachrichten/' + str(job.get('answer_message')))
        chips = m.get('chips', []) + (answer.get('chips', []) if answer else [])
        snapshots = {c['snap']: docs['snapshots/' + c['snap']] for c in chips if c.get('art') == 'snapshot'}
        project = docs.get('projekte/' + job['project'], {})
        return {'protocol': PROTOCOL, 'instructions': INSTRUCTIONS, 'project_root': str(self.root),
                'project': {'id': job['project'], 'name': project.get('name', job['project'])},
                'message': m, 'version': v, 'snapshots': snapshots, **({'answer_message': answer} if answer else {})}

    def event(self, key, attempt_id, kind, text='', usage=None):
        if kind not in {'heartbeat', 'progress', 'question', 'answer', 'failed'}:
            raise StoreError('event', 'Unbekanntes Ereignis.')
        if kind != 'heartbeat': require_text(text, 'Text')
        with self.store.lock, self.store.db:
            j, a = self._owned(key, attempt_id)
            j['heartbeat'] = time.time()
            if kind == 'question':
                j.update(state='question', question=text, answer=None, question_message='frage-' + ident())
                self.put('nachrichten/' + j['question_message'], {'rolle': a['provider'], 'text': text,
                    'projekt': j['project'], 'version': j['base'], 'chips': [], 't': utc(), 'status': 'beantwortet'})
            if kind == 'failed': j.update(state='failed', reason=text); a.update(state='failed', ended=utc())
            if kind != 'heartbeat':
                a['events'] = (a.get('events', []) + [{'kind': kind, 'text': text[:4000], 't': utc()}])[-20:]
            if usage is not None:
                if not isinstance(usage, dict) or any(type(v) not in (int, float) or v < 0 for v in usage.values()):
                    raise StoreError('usage', 'Verbrauch muss aus nichtnegativen Zahlen bestehen.')
                a['usage'] = usage
            self.put('auftraege/' + j['id'], j); self.put('versuche/' + a['id'], a)
            if kind != 'heartbeat': self.store._bump()
            return {'state': j['state'], 'answer': j.get('answer')}

    def answer(self, job_id, text, message_id=None, data=None):
        require_text(text, 'Antwort')
        with self.store.lock, self.store.db:
            docs = self.docs()
            if message_id:
                document_key('nachrichten/' + message_id)
                old = docs.get('nachrichten/' + message_id)
                expected = {**data, 'rolle': USER, 'status': 'beantwortet'}
                if old:
                    if old != expected: raise StoreError('message_conflict', 'Nachrichten-ID bereits verwendet.', 409)
                    return {'id': message_id, 'data': old, 'ok': True}
            j = docs.get('auftraege/' + job_id)
            if not message_id and j and j['state'] == 'working' and j.get('answer') == text:
                return {'ok': True}
            if not j or j['state'] != 'question': raise StoreError('state', 'Keine offene Rückfrage.', 409)
            a = self.docs()['versuche/' + j['attempt']]
            message_id = message_id or 'antwort-' + j.get('question_message', a['id'])
            reply = {**(data or {'text': text, 'projekt': j['project'], 'version': j['base'], 'chips': [], 't': utc()}),
                     'rolle': USER, 'status': 'beantwortet'}
            if reply['projekt'] != j['project']: raise StoreError('project', 'Antwort gehoert zu einem anderen Projekt.')
            self.store._validate('nachrichten/' + message_id, reply, docs, self.store.asset_ids(), self.store.version_paths())
            self.put('nachrichten/' + message_id, reply)
            j.update(state='working', answer=text, answer_message=message_id, heartbeat=time.time())
            a['events'] = (a.get('events', []) + [{'kind': 'answer', 'text': text, 't': utc()}])[-20:]
            self.put('versuche/' + a['id'], a)
            self.put('auftraege/' + job_id, j); self.store._bump()
            return {'ok': True, 'id': message_id, 'data': reply}

    def finish(self, key, attempt_id, text, html=None, checks=None, validation=None):
        require_text(text, 'Antwort')
        if html is not None and (not isinstance(html, str) or len(html.encode('utf-8')) > MAX_DOCUMENT or '<' not in html):
            raise StoreError('html', 'HTML fehlt oder überschreitet 2 MB.')
        if checks is not None and (not isinstance(checks, list) or len(encode(checks)) > 10000):
            raise StoreError('checks', 'Prüfangaben ungültig.')
        proposal = {'text': text, 'html': html, 'checks': checks or []}
        with self.store.lock:
            old = self.docs().get('versuche/' + str(attempt_id), {})
            if old.get('session') == key and old.get('state') == 'done' and old.get('proposal') == proposal:
                return self.docs()['auftraege/' + old['job']]
            self._owned(key, attempt_id)
        if html is not None:
            if validation is None:
                from .validate import validate
                validation = validate(html)
            if not validation.get('ok') or validation.get('sha256') != digest(html.encode('utf-8')):
                raise StoreError('validation', 'Automatische Browserpruefung fehlgeschlagen: ' + '; '.join(validation.get('errors', []) + validation.get('external', []))[:1500], 409)
        with self.store.lock, self.store.db:
            old = self.docs().get('versuche/' + str(attempt_id), {})
            if old.get('session') == key and old.get('state') == 'done' and old.get('proposal') == proposal:
                return self.docs()['auftraege/' + old['job']]
            j, a = self._owned(key, attempt_id)
            if j['state'] == 'question': raise StoreError('question', 'Zuerst die offene Rückfrage beantworten.', 409)
            if j['state'] == 'review' and a.get('proposal') != proposal:
                raise StoreError('result_conflict', 'Abgegebenes Ergebnis wird nicht überschrieben.', 409)
            j.update(state='review', heartbeat=time.time()); a.update(state='review', proposal=proposal)
            if len(encode(a).encode('utf-8')) > MAX_DOCUMENT: raise StoreError('size', 'Ergebnis ist zu groß für ein gesichertes Dokument.')
            self.put('auftraege/' + j['id'], j); self.put('versuche/' + a['id'], a); self.store._bump()
            job_id, attempt = j['id'], a['id']
        try:
            return self.accept(job_id, validation)
        except StoreError as exc:
            # Never leave a project blocked by a proposal nobody can take over. This runs in
            # its own transaction: the failed accept rolled back nothing but must not linger as review.
            with self.store.lock, self.store.db:
                j, a = self.docs()['auftraege/' + job_id], self.docs()['versuche/' + attempt]
                if j['state'] == 'review':
                    j.update(state='conflict', reason=str(exc)); a.update(state='conflict', ended=utc())
                    self.put('auftraege/' + job_id, j); self.put('versuche/' + attempt, a); self.store._bump()
            raise

    def candidate(self, attempt_id):
        with self.store.lock:
            a = self.docs().get('versuche/' + attempt_id, {})
            return a.get('proposal', {}).get('html')

    def accept(self, job_id, validation=None):
        with self.store.lock, self.store.db:
            docs = self.docs(); j = docs.get('auftraege/' + job_id)
            if j and j['state'] == 'done': return j
            if not j or j['state'] != 'review': raise StoreError('state', 'Kein übernehmbarer Vorschlag.', 409)
            a = docs['versuche/' + j['attempt']]; proposal = a['proposal']
            self._owned(a['session'], a['id'])
            versions = [d for p, d in docs.items() if p.startswith('versionen/') and d['projekt'] == j['project']]
            head = max((v['nr'] for v in versions), default=0)
            base = docs.get(f"versionen/{j['project']}~{j['base']:03}", {}) if j.get('base') is not None else {}
            if (j.get('base') is not None and base.get('sha256') != j['base_hash']) or head != j['head']:
                raise StoreError('base_conflict', 'Projektstand wurde inzwischen verändert; Auftrag erneut senden.', 409)
            number = None
            if proposal['html'] is not None:
                if not validation or not validation.get('ok') or validation.get('sha256') != digest(proposal['html'].encode()):
                    raise StoreError('validation', 'Unabhängige Browserprüfung fehlt oder ist fehlgeschlagen.', 409)
                number = head + 1
                path = f"p/{j['project']}/v/{number:03}.html"
                data = proposal['html'].encode('utf-8'); sha = digest(data)
                if self.store.disk_path(path).exists(): raise StoreError('file_conflict', 'Versionsdatei existiert bereits; zuerst Versionen einlesen.', 409)
                self.store.db.execute('INSERT INTO versions VALUES(?,?,?)', (path, sha, data))
                self.put(f"versionen/{j['project']}~{number:03}", {'projekt': j['project'], 'nr': number,
                         'datei': path, 'sha256': sha, 'name': proposal['text'].splitlines()[0][:100], 't': utc(),
                         'basis': j.get('base'), 'episode': [j['message']], 'aenderungen': [], 'herkunft': 'Werkbank-Auftrag', 'auftrag': j['id']})
                a['publication'] = {'path': path, 'sha256': sha, 'materialized': False}
            self.put('nachrichten/antwort-' + a['id'], {'rolle': a['provider'], 'text': proposal['text'],
                     'projekt': j['project'], 'version': j.get('base'), 'erzeugt': number, 'chips': [],
                     't': utc(), 'status': 'beantwortet', 'auftrag': j['id'], 'versuch': a['id']})
            j.update(state='done', result_version=number); a.update(state='done', ended=utc(), validation=validation)
            self.put('auftraege/' + job_id, j); self.put('versuche/' + a['id'], a); self.store._bump()
        self.materialize()
        return j

    def materialize(self):
        # Transactional publication outbox: committed DB bytes are authoritative.
        with self.store.lock, self.store.db:
            for path, a in self.docs().items():
                publication = a.get('publication') if path.startswith('versuche/') else None
                if not publication or publication.get('materialized'): continue
                try:
                    target = self.store.disk_path(publication['path']).resolve()
                    if not target.is_relative_to(self.store.projects.resolve()): raise ValueError('path')
                    body = self.store.version(publication['path'])[0]
                    if digest(body) != publication['sha256']: raise ValueError('hash')
                    target.parent.mkdir(parents=True, exist_ok=True)
                    if target.exists():
                        if digest(target.read_bytes()) != publication['sha256']: raise ValueError('file conflict')
                    else:
                        with target.open('xb') as f: f.write(body)
                    publication.update(materialized=True, error=None)
                except (OSError, ValueError, TypeError) as exc:
                    publication['error'] = 'Dateiabgleich ausstehend: ' + str(exc)
                self.put(path, a)
