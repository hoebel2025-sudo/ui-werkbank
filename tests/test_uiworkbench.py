import json
import shutil
import socket
import subprocess

import pytest

from conftest import ROOT, api, submit
from werkbank.uiworkbench import attach, service, session_identity, choose_project
from werkbank.terminal import Client
from werkbank.server import Server, UI


def test_attach_reuse_switch_and_protect_active_work(server, monkeypatch):
    instance, tmp, url = server
    monkeypatch.setenv('CODEX_THREAD_ID', 'current-codex')
    first = attach('codex', root=tmp, url=url)
    assert first['connected'] and first['project'] == 'demo' and first['browser'] == url + '/'
    c = Client(first['profile'])
    submit(server, 'task', 'Version lesen')
    job = c.call('next')
    with pytest.raises(OSError):
        with Server(instance.server_port, instance.store.directory, UI, instance.store.projects, root=tmp):
            pass
    assert instance.broker.status()['jobs']['task']['state'] == 'working'
    assert attach('codex', root=tmp, url=url)['session'] == first['session']
    second = attach('claude', 'current-claude', root=tmp, url=url)
    assert instance.broker.status()['jobs']['task']['state'] == 'cancelled'
    with pytest.raises(RuntimeError, match='stale_attempt'):
        c.call('finish', {'attempt': job['attempt']['id'], 'text': 'Zu spaet'})
    assert second['session'] != first['session']
    assert Client(first['profile']).next(.1)['type'] == 'detached'
    assert not attach('claude', 'current-claude', root=tmp, url=url, detach=True)['connected']
    assert not instance.broker.status()['bindings']['demo']['enabled']
    assert attach('claude', 'current-claude', root=tmp, url=url)['session'] == second['session']
    assert instance.broker.status()['bindings']['demo']['session'] == second['session']
    assert len(instance.broker.clients()) == 2
    assert len([p for p in instance.store.state()['documents'] if p.startswith('nachrichten/antwort-')]) == 0


def test_uses_chat_project_then_bound_project_then_first(server, monkeypatch):
    instance, tmp, url = server
    instance.store.save_document('projekte/zweites', {'name': 'Zweites'})
    docs = instance.store.state()['documents']
    assert choose_project(docs, {}) == 'demo'
    assert choose_project(docs, {'zweites': {'enabled': True, 'session': 'x'}}) == 'zweites'
    instance.store.save_document('ui/chat', {'value': {'p': 'zweites', 'nr': None}})
    assert attach('claude', 'selected', root=tmp, url=url)['project'] == 'zweites'
    with pytest.raises(ValueError, match='fehlt'):
        attach('claude', 'selected', root=tmp, url=url, project='gibtsnicht')
    with pytest.raises(RuntimeError, match='anderen Ordner'):
        attach('claude', 'other', root=tmp / 'elsewhere', url=url)
    monkeypatch.delenv('CODEX_THREAD_ID', raising=False)
    with pytest.raises(ValueError, match='Sitzungs-ID fehlt'):
        session_identity('codex')
    monkeypatch.setenv('CLAUDE_CODE_SESSION_ID', 'from-env')
    assert session_identity('claude') == 'from-env'
    with pytest.raises(ValueError):
        session_identity('claude', '${CLAUDE_SESSION_ID}')
    with pytest.raises(ValueError, match='local'):
        service('http://example.invalid:8119', tmp)


def test_attach_without_any_project_registers_and_waits(empty_server):
    instance, tmp, url = empty_server
    result = attach('claude', 'no-project-yet', root=tmp, url=url)
    assert result['connected'] and result['project'] is None and 'Projekt' in result['hint']
    session = instance.broker.status()['sessions'][0]
    assert session['verified'] and session['bound'] == [] and not session['detached']
    client = Client(result['profile'])
    assert client.call('wake', {'timeout': 0})['type'] == 'idle'
    # Once a project exists it can be bound without a new attach.
    instance.store.save_document('projekte/neu', {'name': 'Neu'})
    api(empty_server, 'bind', {'project': 'neu', 'session': result['session'], 'auto': True})
    submit(empty_server, 'erste', 'Erste Seite', project='neu', version=None)
    assert client.call('wake', {'timeout': 0})['type'] == 'wake'


def test_starts_only_local_webservice_and_reuses_it(tmp_path, monkeypatch):
    project = tmp_path / 'project'
    shutil.copytree(ROOT / 'werkbank', project / 'werkbank', ignore=shutil.ignore_patterns('__pycache__'))
    shutil.copytree(UI, project / 'ui')
    shutil.copy2(ROOT / 'werkbank.py', project / 'werkbank.py')
    (project / 'projects').mkdir()
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0)); port = probe.getsockname()[1]
    url = f'http://127.0.0.1:{port}'
    children = []
    real_popen = subprocess.Popen
    def record(*args, **kwargs):
        process = real_popen(*args, **kwargs); children.append((args[0], process)); return process
    monkeypatch.setattr(subprocess, 'Popen', record)
    monkeypatch.setattr('webbrowser.open', lambda *a, **k: True)
    try:
        first = attach('codex', 'existing-test', url=url, root=project)
        assert first['service_started']
        again = attach('codex', 'existing-test', url=url, root=project)
        assert again['session'] == first['session'] and not again['service_started']
        assert len(children) == 1
        assert children[0][0][2:4] == ['-m', 'werkbank.server']
    finally:
        for _, child in children:
            child.terminate(); child.wait(timeout=10)


def test_browser_send_reply_and_reconnect_without_duplicate_task(server):
    from playwright.sync_api import sync_playwright
    instance, tmp, url = server
    attached = attach('codex', 'existing-browser', root=tmp, url=url)
    client = Client(attached['profile'])
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            page = browser.new_page(viewport={'width': 1500, 'height': 950})
            page.goto(url); page.wait_for_function("() => document.getElementById('senden').textContent==='Senden'")
            page.locator('#text').fill('Snapshot lesen'); page.click('#senden')
            page.wait_for_function("() => document.querySelector('.msg')?.textContent.includes('Wartet')")
            job = client.call('next'); assert job['type'] == 'job'
            # A retry of enqueue is idempotent even after claiming the message.
            assert api(server, 'enqueue', {'message': job['job']['id']})['attempt'] == job['attempt']['id']
            client.call('event', {'attempt': job['attempt']['id'], 'kind': 'question', 'text': 'Welche Farbe?'})
            page.wait_for_function("() => document.getElementById('text').placeholder.includes('Antwort')")
            def lose_answer_ack(route):
                response = route.fetch(); assert response.status == 200
                route.fulfill(status=503, content_type='application/json', body='{"message":"Antwort verloren"}')
            page.route('**/api/messages', lose_answer_ack)
            page.locator('#text').fill('Blau'); page.click('#senden')
            page.wait_for_function("() => document.getElementById('pickhinweis').textContent.includes('Antwort verloren')")
            assert page.locator('#text').input_value() == 'Blau'
            page.unroute('**/api/messages', lose_answer_ack)
            page.wait_for_function("() => !document.getElementById('text').placeholder.includes('Antwort')")
            page.click('#senden')
            page.wait_for_function("() => document.getElementById('text').value === ''")
            client.call('finish', {'attempt': job['attempt']['id'], 'text': 'Snapshot wurde gelesen.'})
            page.wait_for_function("() => [...document.querySelectorAll('.msg.codex')].some(m=>m.textContent.includes('Snapshot wurde gelesen.'))")
            assert len(instance.broker.status()['jobs']) == 1
            assert len([d for p, d in instance.store.state()['documents'].items() if p.startswith('nachrichten/') and d.get('text') == 'Blau']) == 1
            assert len([d for d in instance.store.state()['documents'].values() if d.get('erzeugt')]) == 0
        finally:
            browser.close()


@pytest.fixture(autouse=True)
def no_real_codex_listener(monkeypatch):
    # Protocol tests are deliberately not host wake evidence.
    import werkbank.codex_queue as queue
    monkeypatch.setattr(queue, 'prepare', lambda identity: {'test_only': True})
    monkeypatch.setattr(queue, 'enable', lambda client, root, prepared=None: client.call('transport', {'mode': 'codex-queue'}))
    monkeypatch.setattr('webbrowser.open', lambda *a, **k: True)
