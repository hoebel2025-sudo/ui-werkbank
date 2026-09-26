"""SQLite persistence: documents, screenshot assets and immutable HTML versions in one file."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import re
import sqlite3
import threading
import uuid
import zipfile

SCHEMA = 2
FORMAT = 'ui-werkbank'
LEGACY_FORMATS = {'ui-werkbank', 'stigma-werkbank'}
MAX_DOCUMENT = 2 * 1024 * 1024
MAX_ASSET = 16 * 1024 * 1024
MAX_BACKUP = 100 * 1024 * 1024
COLLECTIONS = {'projekte', 'versionen', 'nachrichten', 'snapshots', 'ui', 'auftraege', 'versuche', 'bindungen'}
USER = 'user'
ROLES = {USER, 'claude', 'codex', 'agent', 'system'}
KEY = re.compile(r'^[a-zA-Z0-9_-]{1,100}$')
VERSION_PATH = re.compile(r'^p/([a-z0-9-]+)/v/([0-9]{3,6})\.html$')
JOB_STATES = {'queued', 'working', 'question', 'review', 'done', 'cancelled', 'interrupted', 'failed', 'conflict'}


class StoreError(ValueError):
    def __init__(self, code, message, status=400):
        super().__init__(message)
        self.code, self.status = code, status


def utc():
    return datetime.now(timezone.utc).isoformat()


def digest(value):
    return hashlib.sha256(value).hexdigest()


def encode(value):
    try:
        return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(',', ':'))
    except (ValueError, TypeError, RecursionError) as exc:
        raise StoreError('invalid_json', 'Nur endliche JSON-Daten können gespeichert werden.') from exc


def document_key(path):
    parts = path.split('/')
    if len(parts) != 2 or parts[0] not in COLLECTIONS or not re.fullmatch(r'[a-zA-Z0-9_~-]{1,130}', parts[1]):
        raise StoreError('invalid_path', 'Ungültiger Dokumentpfad.')
    return parts


def image_type(data):
    if data.startswith(b'\xff\xd8\xff'):
        return 'image/jpeg'
    if data.startswith(b'\x89PNG\r\n\x1a\n'):
        return 'image/png'
    raise StoreError('invalid_image', 'Screenshots müssen JPEG- oder PNG-Dateien sein.')


class Store:
    """A transaction covers each mutation; binary assets and version HTML live in the same DB.

    Backups therefore contain a consistent snapshot, including screenshots and immutable
    version bytes. Imports merge only identical or missing records, never overwrite history.
    Version files on disk live under ``projects/<id>/v/NNN.html``; their URL is ``p/<id>/v/NNN.html``.
    """
    def __init__(self, directory: Path, projects: Path):
        self.directory, self.projects = Path(directory), Path(projects)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.changed = threading.Condition(self.lock)
        self.closed = False
        self.db = sqlite3.connect(self.directory / 'werkbank.sqlite3', check_same_thread=False, timeout=10)
        version = self.db.execute('PRAGMA user_version').fetchone()[0]
        if version not in (0, 1, SCHEMA):
            self.db.close()
            raise StoreError('schema_version', f'Datenbankversion {version} wird nicht unterstützt.')
        if version == 1:
            migration = self.directory / 'before-schema-2.sqlite3'
            if not migration.exists():
                backup = sqlite3.connect(migration)
                try: self.db.backup(backup)
                finally: backup.close()
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS documents(path TEXT PRIMARY KEY, data TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS assets(id TEXT PRIMARY KEY, mime TEXT NOT NULL, body BLOB NOT NULL);
            CREATE TABLE IF NOT EXISTS versions(path TEXT PRIMARY KEY, sha256 TEXT NOT NULL, body BLOB NOT NULL);
            CREATE TABLE IF NOT EXISTS terminal_clients(id TEXT PRIMARY KEY, data TEXT NOT NULL);
        ''')
        with self.db:
            self.db.execute(f'PRAGMA user_version={SCHEMA}')
            self.db.execute("INSERT OR IGNORE INTO settings VALUES('revision','0')")
            self.db.execute("INSERT OR IGNORE INTO settings VALUES('instance',?)", (str(uuid.uuid4()),))
        self.sync_sources()

    def close(self):
        with self.lock:
            self.closed = True
            self.changed.notify_all()
            self.db.close()

    def disk_path(self, relative):
        """Map a version URL path (p/<id>/v/NNN.html) to its file below the projects folder."""
        if not VERSION_PATH.fullmatch(relative):
            raise StoreError('invalid_path', 'Ungültiger Versionspfad.')
        return self.projects / relative[2:]

    def _docs(self):
        return {path: json.loads(data) for path, data in self.db.execute('SELECT path,data FROM documents ORDER BY path')}

    def _bump(self):
        self.db.execute("UPDATE settings SET value=CAST(value AS INTEGER)+1 WHERE key='revision'")
        self.changed.notify_all()

    def state(self, since=-1):
        with self.lock:
            settings = dict(self.db.execute('SELECT key,value FROM settings'))
            result = {'schema': SCHEMA, 'instance': settings['instance'], 'revision': int(settings['revision']),
                      'agent': {'connected': False}}
            if since != result['revision']:
                result['documents'] = self._docs()
            return result

    def _put(self, path, data):
        self.db.execute('INSERT INTO documents VALUES(?,?) ON CONFLICT(path) DO UPDATE SET data=excluded.data',
                        (path, encode(data)))

    def _project_meta(self, project):
        meta = self.projects / project / 'projekt.json'
        if meta.is_file():
            try:
                data = json.loads(meta.read_text(encoding='utf-8'))
                if isinstance(data, dict):
                    return data
            except (ValueError, OSError):
                pass
        return {}

    def sync_sources(self):
        """Import version files that were added on disk; never change an imported version."""
        added = 0
        with self.lock, self.db:
            docs = self._docs()
            files = sorted(self.projects.glob('*/v/*.html')) if self.projects.is_dir() else []
            for file in files:
                relative = 'p/' + file.relative_to(self.projects).as_posix()
                match = VERSION_PATH.fullmatch(relative)
                if not match:
                    continue
                project, number = match.group(1), int(match.group(2))
                data = file.read_bytes()
                existing = self.db.execute('SELECT sha256 FROM versions WHERE path=?', (relative,)).fetchone()
                if existing:
                    if existing[0] != digest(data):
                        raise StoreError('version_conflict', f'{relative} wurde verändert. Bitte eine neue Versionsnummer anlegen.', 409)
                    continue
                meta = self._project_meta(project)
                project_path = 'projekte/' + project
                if project_path not in docs:
                    name = meta.get('name') if isinstance(meta.get('name'), str) and meta['name'].strip() else project
                    doc = {'name': name[:100], 'art': 'mockup', 't': '', 'herkunft': 'lokale Versionsdateien'}
                    self._put(project_path, doc); docs[project_path] = doc
                version_path = f'versionen/{project}~{number:03}'
                doc = {'projekt': project, 'nr': number, 'name': f'{docs[project_path]["name"]} · Version {number}',
                       'datei': relative, 't': '', 'episode': [], 'aenderungen': [],
                       'herkunft': 'lokale Datei', 'sha256': digest(data)}
                if version_path in docs and docs[version_path] != doc:
                    raise StoreError('version_conflict', 'Versionsmetadaten kollidieren: ' + version_path, 409)
                self._put(version_path, doc)
                self.db.execute('INSERT INTO versions VALUES(?,?,?)', (relative, digest(data), data))
                added += 1
            if added:
                self._bump()
        return {'added': added}

    def _validate(self, path, data, docs, asset_ids, version_paths):
        collection, key = document_key(path)
        if not isinstance(data, dict) or len(encode(data).encode('utf-8')) > MAX_DOCUMENT:
            raise StoreError('invalid_document', 'Dokument fehlt, ist zu groß oder kein Objekt.')
        if collection == 'projekte':
            if not KEY.fullmatch(key) or not isinstance(data.get('name'), str) or not 1 <= len(data['name'].strip()) <= 100:
                raise StoreError('invalid_project', 'Projektname fehlt oder ist zu lang.')
        elif collection == 'versionen':
            if data.get('datei') not in version_paths or data.get('projekt') is None or type(data.get('nr')) is not int:
                raise StoreError('invalid_version', 'Version ohne passende HTML-Datei.')
            match = VERSION_PATH.fullmatch(data['datei'])
            if not match or match.group(1) != data['projekt'] or int(match.group(2)) != data['nr'] or key != f"{data['projekt']}~{data['nr']:03}":
                raise StoreError('invalid_version', 'Versionsnummer und Dateipfad passen nicht zusammen.')
        elif collection == 'snapshots':
            state, picture = data.get('zustand'), data.get('bild')
            if not isinstance(state, dict) or not isinstance(picture, dict) or picture.get('asset') not in asset_ids:
                raise StoreError('missing_asset', 'Snapshot braucht einen dauerhaft gespeicherten Screenshot.')
            if not isinstance(data.get('markierungen'), list) or not isinstance(data.get('elemente'), list):
                raise StoreError('invalid_snapshot', 'Snapshot-Markierungen oder Elemente fehlen.')
            if type(state.get('version')) is not int or f"versionen/{state.get('projekt')}~{state['version']:03}" not in docs:
                raise StoreError('missing_version', 'Snapshot verweist auf eine fehlende Version.')
            picture['url'] = '/api/assets/' + picture['asset']
        elif collection == 'nachrichten':
            if data.get('rolle') not in ROLES or not isinstance(data.get('text'), str):
                raise StoreError('invalid_message', 'Nachricht braucht Rolle und Text.')
            if data.get('status') not in {'wartet', 'gesendet', 'beantwortet', 'fehler', 'sende'}:
                raise StoreError('invalid_message', 'Unbekannter Nachrichtenstatus.')
            if not isinstance(data.get('chips', []), list):
                raise StoreError('invalid_message', 'Referenzen müssen eine Liste sein.')
            for chip in data.get('chips', []):
                if not isinstance(chip, dict) or not re.fullmatch(r'(R|SC)[0-9]+', chip.get('ref', '')):
                    raise StoreError('invalid_reference', 'Ungültiges Referenzkürzel.')
                if chip.get('art') == 'snapshot' and 'snapshots/' + chip.get('snap', '') not in docs:
                    raise StoreError('missing_snapshot', 'Eine Snapshot-Referenz fehlt im lokalen Speicher.')
        elif collection == 'ui' and 'value' not in data:
            raise StoreError('invalid_preference', 'Gespeicherte Einstellung braucht einen Wert.')
        elif collection == 'auftraege':
            if data.get('id') != key or data.get('state') not in JOB_STATES:
                raise StoreError('invalid_job', 'Auftragskennung oder Zustand ungültig.')
            if 'nachrichten/' + str(data.get('message')) not in docs or 'projekte/' + str(data.get('project')) not in docs:
                raise StoreError('invalid_job', 'Auftrag ohne Nachricht oder Projekt.')
            base, base_hash = data.get('base'), data.get('base_hash')
            if type(data.get('head')) is not int or (base is None) != (base_hash is None):
                raise StoreError('invalid_job', 'Ausgangsversion fehlt.')
            if base is not None and (type(base) is not int or not re.fullmatch(r'[a-f0-9]{64}', str(base_hash))):
                raise StoreError('invalid_job', 'Ausgangsversion fehlt.')
        elif collection == 'versuche':
            if data.get('id') != key or 'auftraege/' + str(data.get('job')) not in docs or data.get('provider') not in {'claude', 'codex'}:
                raise StoreError('invalid_attempt', 'Bearbeitungsversuch ohne gültigen Auftrag oder Anbieter.')
            publication = data.get('publication')
            if publication and (not isinstance(publication, dict) or publication.get('path') not in version_paths):
                raise StoreError('invalid_publication', 'Veröffentlichung ohne gültige Version.')
        elif collection == 'bindungen':
            if 'projekte/' + key not in docs or type(data.get('enabled')) is not bool:
                raise StoreError('invalid_binding', 'Projektbindung ungültig.')
        if collection in {'versionen', 'nachrichten'}:
            if 'projekte/' + str(data.get('projekt')) not in docs:
                raise StoreError('missing_project', 'Das referenzierte Projekt fehlt.')
        if collection == 'nachrichten' and data.get('version') is not None:
            if type(data['version']) is not int or f"versionen/{data['projekt']}~{data['version']:03}" not in docs:
                raise StoreError('missing_version', 'Das Ziel der Nachricht fehlt.')

    def save_document(self, path, data):
        collection, key = document_key(path)
        if collection in {'versionen', 'nachrichten', 'auftraege', 'versuche', 'bindungen'}:
            raise StoreError('read_only', 'Versionen entstehen aus Ergebnissen oder Dateien; Nachrichten laufen über /api/messages.', 403)
        with self.lock, self.db:
            docs = self._docs()
            self._validate(path, data, {**docs, path: data}, self.asset_ids(), self.version_paths())
            if collection == 'snapshots' and path in docs and docs[path] != data:
                raise StoreError('snapshot_conflict', 'Ein bestehender Snapshot wird nicht überschrieben.', 409)
            if docs.get(path) != data:
                self._put(path, data); self._bump()
        return {'ok': True}

    def submit(self, key, data, dispatch=None):
        path = 'nachrichten/' + key
        document_key(path)
        # Chat submission saves and dispatches under the same transaction/lock.
        data = {**data, 'rolle': USER, 'status': 'wartet'}
        with self.lock, self.db:
            docs = self._docs()
            self._validate(path, data, docs, self.asset_ids(), self.version_paths())
            if path in docs and docs[path] != data:
                raise StoreError('message_conflict', 'Diese Nachrichten-ID gehört bereits zu einem anderen Inhalt.', 409)
            if path not in docs:
                self._put(path, data); self._bump()
            if dispatch is not None and 'auftraege/' + key not in docs:
                dispatch(key)
        return {'id': key, 'data': data, 'agent_connected': False}

    def asset_ids(self):
        return {r[0] for r in self.db.execute('SELECT id FROM assets')}

    def version_paths(self):
        return {r[0] for r in self.db.execute('SELECT path FROM versions')}

    def upload(self, data):
        if not data or len(data) > MAX_ASSET:
            raise StoreError('asset_size', 'Screenshot fehlt oder überschreitet 16 MB.', 413)
        mime, key = image_type(data), digest(data)
        with self.lock, self.db:
            self.db.execute('INSERT OR IGNORE INTO assets VALUES(?,?,?)', (key, mime, data))
        return {'id': key, 'url': '/api/assets/' + key, 'sizeBytes': len(data), 'contentType': mime}

    def asset(self, key):
        with self.lock:
            return self.db.execute('SELECT mime,body FROM assets WHERE id=?', (key,)).fetchone()

    def version(self, path):
        with self.lock:
            return self.db.execute('SELECT body FROM versions WHERE path=?', (path,)).fetchone()

    def export(self):
        with self.lock:
            documents = self._docs()
            files, payloads = {}, {}
            for path, sha, body in self.db.execute('SELECT path,sha256,body FROM versions ORDER BY path'):
                name = 'versions/' + path
                files[name] = {'sha256': sha, 'bytes': len(body)}; payloads[name] = body
            for key, mime, body in self.db.execute('SELECT id,mime,body FROM assets ORDER BY id'):
                name = 'assets/' + key
                files[name] = {'sha256': key, 'bytes': len(body), 'mime': mime}; payloads[name] = body
            manifest = {'format': FORMAT, 'schema': SCHEMA, 'exported_at': utc(),
                        'documents': documents, 'files': files,
                        'scope': 'Lokaler Werkbankstand ohne Terminal-Zugangsdaten.'}
            out = io.BytesIO()
            with zipfile.ZipFile(out, 'w', zipfile.ZIP_DEFLATED) as archive:
                archive.writestr('manifest.json', encode(manifest))
                for name, data in payloads.items():
                    archive.writestr(name, data)
            return out.getvalue()

    def import_backup(self, raw):
        try:
            if len(raw) > MAX_BACKUP:
                raise StoreError('backup_size', 'Sicherung überschreitet 100 MB.', 413)
            with zipfile.ZipFile(io.BytesIO(raw)) as archive:
                info = archive.infolist()
                names = [i.filename for i in info]
                if len(names) != len(set(names)) or sum(i.file_size for i in info) > MAX_BACKUP or any(i.file_size > MAX_ASSET for i in info):
                    raise StoreError('invalid_backup', 'Doppelte Einträge oder zu große Sicherung.')
                manifest = json.loads(archive.read('manifest.json'))
                if manifest.get('format') not in LEGACY_FORMATS or manifest.get('schema') not in (1, SCHEMA):
                    raise StoreError('schema_version', 'Sicherungsformat oder Version wird nicht unterstützt.')
                docs, files = manifest['documents'], manifest['files']
                if not isinstance(docs, dict) or not isinstance(files, dict) or set(names) != {'manifest.json', *files}:
                    raise StoreError('invalid_backup', 'Dateiliste der Sicherung stimmt nicht.')
                assets, versions = {}, {}
                for name, meta in files.items():
                    body = archive.read(name)
                    if len(body) != meta['bytes'] or digest(body) != meta['sha256']:
                        raise StoreError('checksum', 'Prüfsumme stimmt nicht: ' + name)
                    if name.startswith('versions/') and VERSION_PATH.fullmatch(name[9:]):
                        body.decode('utf-8')
                        versions[name[9:]] = (meta['sha256'], body)
                    elif re.fullmatch(r'assets/[a-f0-9]{64}', name) and digest(body) == name[7:]:
                        mime = image_type(body)
                        if meta.get('mime') != mime: raise StoreError('invalid_image', 'Bildtyp stimmt nicht.')
                        assets[name[7:]] = (mime, body)
                    else:
                        raise StoreError('invalid_path', 'Ungültiger Pfad in Sicherung.')
        except StoreError:
            raise
        except (ValueError, KeyError, TypeError, AttributeError, UnicodeError, zipfile.BadZipFile, RuntimeError) as exc:
            raise StoreError('invalid_backup', 'Sicherung ist beschädigt oder unvollständig.') from exc
        with self.lock, self.db:
            current = self._docs()
            if any(p.startswith('auftraege/') and d.get('state') in {'working', 'question', 'review'} for p, d in current.items()):
                raise StoreError('busy', 'Vor einem Import laufende Aufträge abschließen oder abbrechen.', 409)
            # A backup never re-authorizes a terminal or resumes interrupted work.
            for path, data in docs.items():
                if not isinstance(data, dict): raise StoreError('invalid_document', 'Sicherung enthält ein ungültiges Dokument.')
                if path.startswith('nachrichten/') and data.get('rolle') == 'jonas':
                    data['rolle'] = USER
                if path.startswith('bindungen/'):
                    data.update(enabled=False, session=None, auto=False)
                if path.startswith('auftraege/') and data.get('state') in {'working', 'question', 'review', 'queued'}:
                    data.update(state='interrupted', reason='Aus Sicherung wiederhergestellt; ausdrücklich erneut senden.')
                if path.startswith('versuche/') and data.get('state') in {'working', 'question', 'review'}:
                    data.update(state='interrupted')
            combined = {**current, **docs}
            asset_ids = self.asset_ids() | assets.keys()
            version_paths = self.version_paths() | versions.keys()
            def restore_metadata(path):
                return path.startswith('ui/') or (path.startswith('projekte/') and
                    current.get(path, {}).get('herkunft') == 'lokale Versionsdateien')
            for path, data in docs.items():
                self._validate(path, data, combined, asset_ids, version_paths)
                if not restore_metadata(path) and path in current and current[path] != data:
                    raise StoreError('import_conflict', 'Import würde vorhandene Daten ändern: ' + path, 409)
            for path, (sha, body) in versions.items():
                old = self.db.execute('SELECT sha256 FROM versions WHERE path=?', (path,)).fetchone()
                if old and old[0] != sha:
                    raise StoreError('version_conflict', 'Import würde eine Version überschreiben: ' + path, 409)
            backup_dir = self.directory / 'backups'
            backup_dir.mkdir(exist_ok=True)
            backup = backup_dir / (datetime.now(timezone.utc).strftime('before-import-%Y%m%dT%H%M%S-') + uuid.uuid4().hex[:8] + '.zip')
            with backup.open('xb') as stream:
                stream.write(self.export()); stream.flush(); os.fsync(stream.fileno())
            for key, (mime, body) in assets.items():
                self.db.execute('INSERT OR IGNORE INTO assets VALUES(?,?,?)', (key, mime, body))
            for path, (sha, body) in versions.items():
                self.db.execute('INSERT OR IGNORE INTO versions VALUES(?,?,?)', (path, sha, body))
            for path, data in docs.items():
                if path not in current or restore_metadata(path):
                    self._put(path, data)
            self._bump()
        return {'ok': True, 'added_documents': len(docs.keys() - current.keys()), 'assets': len(assets), 'versions': len(versions),
                'previous_backup': 'backups/' + backup.name}
