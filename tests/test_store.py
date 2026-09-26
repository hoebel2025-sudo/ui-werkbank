import base64
from concurrent.futures import ThreadPoolExecutor
import io
import json
import sqlite3
import zipfile

import pytest

from conftest import make_projects
from werkbank.store import Store, StoreError, digest, USER

PNG = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aX1kAAAAASUVORK5CYII=')


@pytest.fixture
def setup(tmp_path):
    projects = make_projects(tmp_path)
    store = Store(tmp_path / 'data', projects)
    try:
        yield store, projects, tmp_path
    finally:
        store.close()


def message(text='Bitte prüfen'):
    return {'rolle': USER, 'text': text, 'projekt': 'demo', 'version': 1,
            't': '2026-09-25T10:00:00+00:00', 'status': 'wartet', 'chips': []}


def snapshot(store):
    image = store.upload(PNG)
    data = {'ref': 'SC1', 't': '2026-09-25T10:00:00+00:00',
            'zustand': {'projekt': 'demo', 'version': 1, 'fenster': {'b': 1000, 'h': 700},
                        'quelle': 'schnittstelle', 'seite': {'count': 3}, 'scroll': []},
            'markierungen': [{'nr': 'M1', 'form': 'rechteck', 'x': 1, 'y': 2, 'w': 30, 'h': 40}],
            'elemente': [], 'bild': {'asset': image['id'], 'url': image['url'], 'b': 1000, 'h': 700}}
    store.save_document('snapshots/sc-test', data)
    return data


def rewrite_archive(raw, change):
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        entries = {name: z.read(name) for name in z.namelist()}
    manifest = json.loads(entries.pop('manifest.json'))
    change(manifest, entries)
    out = io.BytesIO()
    with zipfile.ZipFile(out, 'w', zipfile.ZIP_DEFLATED) as z:
        z.writestr('manifest.json', json.dumps(manifest))
        for name, content in entries.items(): z.writestr(name, content)
    return out.getvalue()


def test_project_name_comes_from_projekt_json_and_versions_are_imported(setup):
    store, projects, _ = setup
    docs = store.state()['documents']
    assert docs['projekte/demo']['name'] == 'Demo'
    assert docs['versionen/demo~001']['datei'] == 'p/demo/v/001.html'
    assert store.disk_path('p/demo/v/001.html') == projects / 'demo/v/001.html'


def test_source_versions_survive_restart_without_source_files(setup):
    store, projects, root = setup
    before = store.state()['documents']
    other = Store(root / 'data', root / 'missing-projects')
    try:
        assert other.state()['documents'] == before
        assert other.version('p/demo/v/001.html')[0] == (projects / 'demo/v/001.html').read_bytes()
    finally: other.close()


def test_changed_historic_version_rolls_back_all_new_imports(setup):
    store, projects, root = setup
    before = store.state()
    new = projects / 'aaa/v/001.html'; new.parent.mkdir(parents=True); new.write_text('<title>New</title>')
    (projects / 'demo/v/001.html').write_text('changed')
    with pytest.raises(StoreError, match='verändert'): store.sync_sources()
    assert store.state() == before
    assert store.version('p/aaa/v/001.html') is None


def test_new_version_appends_without_changing_previous(setup):
    store, projects, root = setup
    old = store.version('p/demo/v/001.html')
    (projects / 'demo/v/002.html').write_text('<title>Two</title>')
    assert store.sync_sources()['added'] == 1
    assert store.sync_sources()['added'] == 0
    assert store.version('p/demo/v/001.html') == old
    assert store.state()['documents']['versionen/demo~002']['nr'] == 2


def test_message_retry_is_idempotent_and_changed_request_conflicts(setup):
    store, _, _ = setup
    first = store.submit('m-one', message())
    revision = store.state()['revision']
    assert store.submit('m-one', message()) == first
    assert store.state()['revision'] == revision
    with pytest.raises(StoreError) as error: store.submit('m-one', message('changed'))
    assert error.value.status == 409
    assert store.state()['documents']['nachrichten/m-one']['status'] == 'wartet'


def test_snapshot_references_real_asset_and_rejects_overwrite(setup):
    store, _, _ = setup
    packet = snapshot(store)
    assert store.asset(packet['bild']['asset'])[1] == PNG
    store.save_document('snapshots/sc-test', packet)
    with pytest.raises(StoreError, match='überschrieben'):
        store.save_document('snapshots/sc-test', {**packet, 'ref': 'SC2'})
    packet['bild']['asset'] = 'missing'
    with pytest.raises(StoreError, match='Screenshot'): store.save_document('snapshots/sc-missing', packet)


def test_message_cannot_reference_missing_snapshot(setup):
    store, _, _ = setup
    data = {**message(), 'chips': [{'art': 'snapshot', 'ref': 'SC1', 'snap': 'missing'}]}
    with pytest.raises(StoreError, match='Snapshot'): store.submit('m-bad', data)
    assert 'nachrichten/m-bad' not in store.state()['documents']


@pytest.mark.parametrize('path,data', [
    ('ui/../../key', {'value': 1}), ('unknown/key', {}), ('ui/state', {'value': float('nan')}),
    ('ui/state', {}), ('projekte/empty', {'name': ''}), ('snapshots/invalid', {}),
    ('versionen/demo~001', {}), ('nachrichten/m-bypass', message()),
])
def test_invalid_documents_are_rejected_without_partial_write(setup, path, data):
    store, _, _ = setup
    before = store.state()
    with pytest.raises(StoreError): store.save_document(path, data)
    assert store.state() == before


def test_full_backup_roundtrip_includes_assets_versions_view_and_messages(setup):
    store, projects, root = setup
    packet = snapshot(store)
    data = {**message(), 'chips': [{'art': 'snapshot', 'ref': 'SC1', 'snap': 'sc-test'}]}
    store.submit('m-test', data)
    store.save_document('ui/entwurf', {'value': {'text': 'Ungesendeter Entwurf', 'chips': []}})
    restored = Store(root / 'restored', root / 'no-projects')
    try:
        result = restored.import_backup(store.export())
        assert restored.state()['documents'] == store.state()['documents']
        assert restored.asset(packet['bild']['asset'])[1] == PNG
        assert restored.version('p/demo/v/001.html') == store.version('p/demo/v/001.html')
        assert (restored.directory / result['previous_backup']).is_file()
        assert restored.import_backup(store.export())['added_documents'] == 0
    finally: restored.close()


def test_legacy_backup_format_and_role_are_accepted(setup):
    store, _, root = setup
    store.submit('m-old', message())
    def legacy(manifest, files):
        manifest['format'] = 'stigma-werkbank'
        manifest['documents']['nachrichten/m-old']['rolle'] = 'jonas'
    restored = Store(root / 'restored', root / 'no-projects')
    try:
        restored.import_backup(rewrite_archive(store.export(), legacy))
        assert restored.state()['documents']['nachrichten/m-old']['rolle'] == USER
    finally: restored.close()


def test_import_restores_draft_but_preserves_previous_state_in_backup(setup):
    store, _, _ = setup
    store.save_document('ui/entwurf', {'value': {'text': 'alt'}})
    backup = store.export()
    store.save_document('ui/entwurf', {'value': {'text': 'neu'}})
    result = store.import_backup(backup)
    assert store.state()['documents']['ui/entwurf']['value']['text'] == 'alt'
    with zipfile.ZipFile(store.directory / result['previous_backup']) as z:
        assert json.loads(z.read('manifest.json'))['documents']['ui/entwurf']['value']['text'] == 'neu'


def test_backup_project_name_replaces_only_inferred_file_metadata(setup):
    store, projects, root = setup
    store.save_document('projekte/demo', {'name': 'Mein Projekt', 't': '2026-09-25T10:00:00Z'})
    restored = Store(root / 'restored', projects)
    try:
        restored.import_backup(store.export())
        assert restored.state()['documents']['projekte/demo']['name'] == 'Mein Projekt'
    finally: restored.close()


def test_conflicting_import_is_atomic(setup):
    store, _, _ = setup
    store.submit('m-one', message())
    before = store.state()
    def corrupt(manifest, files):
        manifest['documents']['projekte/aaa'] = {'name': 'Must not appear'}
        manifest['documents']['nachrichten/m-one']['text'] = 'conflict'
    bad = rewrite_archive(store.export(), corrupt)
    with pytest.raises(StoreError, match='vorhandene Daten'): store.import_backup(bad)
    assert store.state() == before
    assert not (store.directory / 'backups').exists()


def test_tampered_backup_asset_is_rejected(setup):
    store, _, _ = setup
    snapshot(store)
    def corrupt(manifest, files): files['assets/' + digest(PNG)] += b'altered'
    before = store.state()
    with pytest.raises(StoreError, match='Prüfsumme'): store.import_backup(rewrite_archive(store.export(), corrupt))
    assert store.state() == before


@pytest.mark.parametrize('kind', ['schema', 'traversal', 'invalid_zip'])
def test_invalid_backup_formats(setup, kind):
    store, _, _ = setup
    def corrupt(manifest, files):
        if kind == 'schema': manifest['schema'] = 99
        else:
            data = b'bad'; name = '../outside.html'
            files[name] = data; manifest['files'][name] = {'sha256': digest(data), 'bytes': len(data)}
    bad = b'not-a-zip' if kind == 'invalid_zip' else rewrite_archive(store.export(), corrupt)
    before = store.state()
    with pytest.raises(StoreError): store.import_backup(bad)
    assert store.state() == before


def test_unknown_database_version_is_not_reinitialized(tmp_path):
    with sqlite3.connect(tmp_path / 'werkbank.sqlite3') as db: db.execute('PRAGMA user_version=99')
    with pytest.raises(StoreError, match='Datenbankversion'): Store(tmp_path, tmp_path / 'projects')
    with sqlite3.connect(tmp_path / 'werkbank.sqlite3') as db: assert db.execute('PRAGMA user_version').fetchone()[0] == 99


def test_concurrent_messages_keep_all_contents(setup):
    store, _, _ = setup
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda n: store.submit(f'm-{n}', message(str(n))), range(40)))
    messages = {k: v for k, v in store.state()['documents'].items() if k.startswith('nachrichten/')}
    assert len(messages) == 40
    assert {m['text'] for m in messages.values()} == {str(n) for n in range(40)}
