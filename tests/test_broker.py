import json
import sqlite3
import threading
import time

import pytest

from conftest import make_projects
from werkbank.broker import Broker, PROTOCOL
from werkbank.store import Store, StoreError, USER


@pytest.fixture
def setup(tmp_path):
    projects = make_projects(tmp_path)
    store = Store(tmp_path / 'data', projects); broker = Broker(store, tmp_path)
    try: yield broker, store, projects
    finally: store.close()


def client(broker, provider='claude', ready=True, identity=None):
    code = broker.pair(provider, 'Vorhandenes Terminal')['code']
    c = broker.register(code, identity or 'existing-session-' + provider)
    broker.authenticate(c['id'], c['secret'])
    if ready: broker.ready(c['id'], c['challenge'], PROTOCOL)
    return c


def job(broker, store, key='m-one', project='demo', version=1):
    store.submit(key, {'rolle': USER, 'projekt': project, 'version': version, 'text': 'Bitte Knopf ändern', 'chips': []})
    return broker.enqueue(key)


def active(broker, store):
    c = client(broker); broker.bind('demo', c['id']); j = job(broker, store)
    result = broker.next(c['id'])
    return c, result


def test_pair_is_one_time_and_never_lists_secret(setup):
    b, s, _ = setup; code = b.pair('codex', 'Terminal')['code']; c = b.register(code, 'existing')
    with pytest.raises(StoreError): b.register(code, 'another')
    assert c['secret'] not in json.dumps(b.status())
    with pytest.raises(StoreError): b.authenticate(c['id'], 'wrong')


def test_binding_requires_actual_ready_response(setup):
    b, s, _ = setup; c = client(b, ready=False)
    with pytest.raises(StoreError): b.bind('demo', c['id'])
    with pytest.raises(StoreError): b.ready(c['id'], c['challenge'], 999)
    with pytest.raises(StoreError): b.ready(c['id'], 'wrong', 1)
    b.ready(c['id'], c['challenge'], 1); b.bind('demo', c['id'])
    assert b.status()['sessions'][0]['verified'] and b.status()['sessions'][0]['bound'] == ['demo']


def test_saved_message_is_not_automatically_dispatched(setup):
    b, s, _ = setup; c = client(b); b.bind('demo', c['id'])
    s.submit('m', {'projekt': 'demo', 'version': 1, 'text': 'Notiz', 'chips': []})
    assert b.next(c['id'])['type'] == 'idle'


def test_idempotent_enqueue_and_claim_survive_lost_response(setup):
    b, s, _ = setup; c, r = active(b, s)
    assert b.enqueue(r['job']['id'])['attempt'] == r['attempt']['id']
    retry = b.next(c['id'])
    assert retry['type'] == 'active' and retry['attempt']['id'] == r['attempt']['id']
    assert retry['context'] == r['context']


def test_only_selected_existing_session_claims_job(setup):
    b, s, _ = setup; c = client(b); other = client(b, 'codex'); b.bind('demo', c['id']); job(b, s)
    assert b.next(other['id'])['type'] == 'idle'
    assert b.next(c['id'])['type'] == 'job'
    old = b.next(c['id'])
    job(b, s, 'queued-old-session')
    b.bind('demo', other['id'])
    # Started work is fenced; an unclaimed message simply waits for the next session.
    assert b.status()['jobs']['queued-old-session']['state'] == 'queued'
    assert b.status()['jobs'][old['job']['id']]['state'] == 'cancelled'
    with pytest.raises(StoreError, match='freigegeben'):
        b.finish(c['id'], old['attempt']['id'], 'Zu spaet')
    handed = b.next(other['id'])
    assert handed['type'] == 'job' and handed['job']['id'] == 'queued-old-session'
    assert b.next(c['id'])['type'] == 'idle'


def test_takeover_moves_every_binding_and_marks_the_old_session_detached(setup):
    b, s, projects = setup
    s.save_document('projekte/zweites', {'name': 'Zweites'})
    old = client(b); b.bind('demo', old['id']); b.bind('zweites', old['id'])
    job(b, s, 'laeuft'); b.next(old['id'])
    new = client(b, 'codex')
    b.bind('demo', new['id'], auto=True, takeover=True)
    bindings = b.status()['bindings']
    assert bindings['demo']['session'] == new['id'] and bindings['zweites']['session'] == new['id']
    assert b.status()['jobs']['laeuft']['state'] == 'cancelled'
    sessions = {x['id']: x for x in b.status()['sessions']}
    assert sessions[old['id']]['detached'] and sessions[old['id']]['bound'] == []
    assert sessions[new['id']]['bound'] == ['demo', 'zweites']
    b.transport(old['id'], 'claude-hook')
    assert b.wake(old['id'], timeout=0)['type'] == 'detached'
    # A re-attach of the old identity revives it; bindings stay with the new session until rebound.
    b.ready(old['id'], old['challenge'], 1)
    assert not next(x for x in b.status()['sessions'] if x['id'] == old['id'])['detached']


def test_detach_releases_projects_and_stops_waking(setup):
    b, s, _ = setup; c, r = active(b, s)
    b.transport(c['id'], 'claude-hook')
    b.detach(c['id'])
    assert not b.status()['bindings']['demo']['enabled']
    assert b.status()['jobs'][r['job']['id']]['state'] == 'cancelled'
    assert b.wake(c['id'], timeout=0)['type'] == 'detached'


def test_session_without_binding_keeps_waiting_until_a_project_is_bound(setup):
    b, s, _ = setup; c = client(b); b.transport(c['id'], 'claude-hook')
    assert b.wake(c['id'], timeout=0)['type'] == 'idle'
    b.bind('demo', c['id']); job(b, s)
    assert b.wake(c['id'], timeout=0)['type'] == 'wake'


def test_concurrent_claims_allocate_one_attempt(setup):
    b, s, _ = setup; c = client(b); b.bind('demo', c['id']); job(b, s)
    results = []
    threads = [threading.Thread(target=lambda: results.append(b.next(c['id']))) for _ in range(8)]
    for t in threads: t.start()
    for t in threads: t.join()
    assert len({r['attempt']['id'] for r in results}) == 1
    assert sum(r['type'] == 'job' for r in results) == 1


def test_cancel_fences_late_result_then_other_session_can_take_over(setup):
    b, s, _ = setup; c, r = active(b, s); b.cancel(r['job']['id']); b.bind('demo', None)
    with pytest.raises(StoreError): b.finish(c['id'], r['attempt']['id'], 'Zu spät')
    other = client(b, 'codex'); b.bind('demo', other['id']); b.enqueue(r['job']['id'])
    nxt = b.next(other['id']); assert nxt['attempt']['id'] != r['attempt']['id']
    b.finish(other['id'], nxt['attempt']['id'], 'Antwort ohne HTML'); b.accept(r['job']['id'])
    assert s.state()['documents']['nachrichten/antwort-' + nxt['attempt']['id']]['rolle'] == 'codex'


def test_question_and_answer_roundtrip(setup):
    b, s, _ = setup; c, r = active(b, s); a = r['attempt']['id']
    b.event(c['id'], a, 'question', 'Welche Farbe?')
    b.answer(r['job']['id'], 'Blau')
    assert b.event(c['id'], a, 'heartbeat')['answer'] == 'Blau'


def test_candidate_requires_matching_independent_validation_and_appends_version(setup):
    b, s, projects = setup; c, r = active(b, s); a = r['attempt']['id']; html = '<!doctype html><body>Neu</body>'
    before = (projects / 'demo/v/001.html').read_bytes()
    with pytest.raises(StoreError):
        b.finish(c['id'], a, 'Neue Darstellung', html, validation={'ok': True, 'sha256': 'wrong'})
    assert b.status()['jobs'][r['job']['id']]['state'] == 'working'
    assert b.finish(c['id'], a, 'Neue Darstellung', html)['state'] == 'done'
    assert b.finish(c['id'], a, 'Neue Darstellung', html)['state'] == 'done'
    assert (projects / 'demo/v/001.html').read_bytes() == before
    assert (projects / 'demo/v/002.html').read_text() == html
    assert b.accept(r['job']['id'])['result_version'] == 2
    assert len([p for p in s.state()['documents'] if p.startswith('nachrichten/antwort-')]) == 1


def test_project_without_version_gets_version_one_from_blank_base(setup):
    b, s, projects = setup
    s.save_document('projekte/leer', {'name': 'Leer'})
    c = client(b); b.bind('leer', c['id'])
    j = job(b, s, 'erste', project='leer', version=None)
    assert j['base'] is None and j['base_hash'] is None and j['head'] == 0
    r = b.next(c['id']); assert r['type'] == 'job' and r['context']['version'] is None
    html = '<!doctype html><body><h1>Erste Version</h1></body>'
    assert b.finish(c['id'], r['attempt']['id'], 'Erste Oberflaeche', html)['result_version'] == 1
    assert (projects / 'leer/v/001.html').read_text() == html
    assert s.state()['documents']['versionen/leer~001']['basis'] is None
    # The next message follows the new head automatically.
    s.submit('zweite', {'projekt': 'leer', 'version': None, 'text': 'weiter', 'chips': [], 'follow_latest': True}, dispatch=b.enqueue)
    second = b.next(c['id'])
    assert second['job']['base'] == 1 and second['job']['head'] == 1


def test_external_new_version_blocks_stale_result_and_marks_conflict(setup):
    b, s, projects = setup; c, r = active(b, s)
    (projects / 'demo/v/002.html').write_text('<body>Andere Änderung</body>', encoding='utf-8'); s.sync_sources()
    with pytest.raises(StoreError, match='inzwischen'): b.finish(c['id'], r['attempt']['id'], 'Antwort', '<body>Neue Version</body>')
    st = b.status()
    assert st['jobs'][r['job']['id']]['state'] == 'conflict'
    assert st['jobs'][r['job']['id']]['proposal_text'] == 'Antwort'
    # The project is free again: a new message can be claimed.
    job(b, s, 'danach', version=2)
    assert b.next(c['id'])['type'] == 'job'


def test_restart_revokes_inflight_attempt_but_remembers_optional_binding(setup):
    b, s, _ = setup; c = client(b); b.bind('demo', c['id'], True); job(b, s); r = b.next(c['id'])
    restart = Broker(s, b.root)
    assert restart.status()['jobs'][r['job']['id']]['state'] == 'interrupted'
    assert restart.status()['bindings']['demo']['enabled']
    assert not restart.status()['sessions'][0]['online']
    with pytest.raises(StoreError): restart.finish(c['id'], r['attempt']['id'], 'Zu spät')
    restart.authenticate(c['id'], c['secret']); assert restart.next(c['id'])['type'] == 'handshake'


def test_restart_converts_orphaned_review_into_conflict(setup):
    b, s, _ = setup; c, r = active(b, s)
    with s.lock, s.db:
        j = s._docs()['auftraege/' + r['job']['id']]; j['state'] = 'review'; s._put('auftraege/' + j['id'], j)
    restart = Broker(s, b.root)
    assert restart.status()['jobs'][r['job']['id']]['state'] == 'conflict'


def test_timeout_never_retries_automatically(setup):
    b, s, _ = setup; c, r = active(b, s)
    with s.lock, s.db:
        r['job']['heartbeat'] = time.time() - 1801; s._put('auftraege/' + r['job']['id'], r['job'])
    assert b.status()['jobs'][r['job']['id']]['state'] == 'interrupted'
    assert b.next(c['id'])['type'] == 'idle'


def test_backup_keeps_jobs_but_not_credentials_or_active_authority(setup, tmp_path):
    b, s, projects = setup; c, r = active(b, s); raw = s.export()
    from zipfile import ZipFile
    from io import BytesIO
    with ZipFile(BytesIO(raw)) as z:
        assert c['secret'].encode() not in z.read('manifest.json')
    other = Store(tmp_path / 'restored', projects)
    try:
        other.import_backup(raw); restored = Broker(other, tmp_path)
        st = restored.status(); assert st['sessions'] == []
        assert st['jobs'][r['job']['id']]['state'] == 'interrupted'
        assert not st['bindings']['demo']['enabled']
    finally: other.close()


def test_import_is_blocked_during_work(setup):
    b, s, _ = setup; raw = s.export(); active(b, s)
    with pytest.raises(StoreError, match='laufende'): s.import_backup(raw)


@pytest.mark.parametrize('collection', ['auftraege', 'versuche', 'bindungen'])
def test_ui_generic_api_cannot_forge_agent_records(setup, collection):
    b, s, _ = setup
    with pytest.raises(StoreError): s.save_document(collection + '/fake', {})


def test_schema_one_migration_keeps_data_and_backup(tmp_path):
    p = tmp_path / 'db'; p.mkdir(); conn = sqlite3.connect(p / 'werkbank.sqlite3')
    conn.execute('PRAGMA user_version=1'); conn.execute('CREATE TABLE documents(path TEXT PRIMARY KEY,data TEXT NOT NULL)')
    conn.execute('INSERT INTO documents VALUES(?,?)', ('ui/draft', '{"value":"Bestehender Entwurf"}')); conn.commit(); conn.close()
    store = Store(p, tmp_path / 'missing')
    try:
        assert store.state()['documents']['ui/draft']['value'] == 'Bestehender Entwurf'
        assert (p / 'before-schema-2.sqlite3').is_file()
    finally: store.close()


def test_follow_latest_queued_messages_use_current_head_without_handover(setup):
    b, s, _ = setup; c = client(b); b.bind('demo', c['id'])
    for key in ('one', 'two'):
        s.submit(key, {'projekt': 'demo', 'version': 1, 'text': key, 'chips': [], 'follow_latest': True}, dispatch=b.enqueue)
    first = b.next(c['id'])
    b.finish(c['id'], first['attempt']['id'], 'Erste Antwort', '<body>Version zwei</body>')
    second = b.next(c['id'])
    assert second['job']['base'] == 2 and second['job']['head'] == 2
    assert 'handover' not in second['context']
    b.finish(c['id'], second['attempt']['id'], 'Zweite Antwort', '<body>Version drei</body>')
    assert b.status()['jobs']['two']['result_version'] == 3


def test_send_transaction_rolls_back_if_dispatch_fails_and_retry_does_not_revive_cancelled(setup):
    b, s, _ = setup
    data = {'projekt': 'demo', 'version': 7, 'text': 'Fehlende Version', 'chips': []}
    with pytest.raises(StoreError): s.submit('broken', data, dispatch=b.enqueue)
    assert 'nachrichten/broken' not in s.state()['documents']
    data['version'] = 1
    s.submit('same', data, dispatch=b.enqueue); b.cancel('same')
    s.submit('same', data, dispatch=b.enqueue)
    assert b.status()['jobs']['same']['state'] == 'cancelled'


@pytest.fixture(autouse=True)
def no_real_browser(monkeypatch):
    # Broker tests exercise the state machine; the Chromium check has its own tests.
    import werkbank.validate as validate
    from werkbank.store import digest
    monkeypatch.setattr(validate, 'validate', lambda html: {'ok': True, 'sha256': digest(html.encode('utf-8')), 'errors': [], 'external': []})
