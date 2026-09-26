import json
import os
import subprocess
import sys

import pytest

from conftest import ROOT, registered, api
from werkbank.validate import validate


def test_terminal_http_auth_and_no_unselected_work(server):
    c = registered(server)
    assert c.call('next')['type'] == 'idle'
    c.data['secret'] = 'wrong'
    with pytest.raises(RuntimeError, match='terminal_auth'): c.call('next')


def test_real_cli_register_ready_and_nonblocking_next(server, tmp_path):
    _, tmp, url = server; pair = api(server, 'pair', {'provider': 'codex', 'name': 'Bestehend'})
    profile = tmp / 'cli.json'
    base = [sys.executable, '-B', str(ROOT / 'werkbank.py'), 'terminal', '--url', url, '--profile', str(profile)]
    env = {**os.environ, 'PYTHONUTF8': '1', 'PYTHONDONTWRITEBYTECODE': '1', 'WERKBANK_NO_REEXEC': '1'}
    elsewhere = tmp_path / 'elsewhere'; elsewhere.mkdir()
    def run(*args):
        result = subprocess.run([*base, *args], cwd=elsewhere, env=env, capture_output=True, text=True, encoding='utf-8', timeout=30)
        assert result.returncode == 0, result.stderr
        return json.loads(result.stdout)
    registered_cli = run('connect', '--pair', pair['code'], '--identity', 'existing-cli-test')
    assert 'secret' not in registered_cli
    assert run('ready', '--challenge', registered_cli['challenge'])['ready']
    assert run('next', '--timeout', '0.1')['type'] == 'idle'


@pytest.mark.parametrize('html,ok', [
    ('<!doctype html><body><button>Gültig</button></body>', True),
    ('<!doctype html><body><script>throw new Error("Broken")</script></body>', False),
    ('<!doctype html><body><img src="https://outside.invalid/image.png"></body>', False)])
def test_independent_candidate_browser_validation(html, ok):
    assert validate(html)['ok'] == ok


def test_real_browser_chat_auto_publication_and_immediate_takeover(server):
    from playwright.sync_api import sync_playwright
    instance, tmp, url = server; first = registered(server); second = registered(server, 'codex', 'second.json')
    expected_version = 1 + max(d['nr'] for path, d in instance.store.state()['documents'].items() if path.startswith('versionen/demo~'))
    api(server, 'bind', {'project': 'demo', 'session': first.data['id']})
    errors = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            pg = browser.new_page(viewport={'width': 1500, 'height': 950})
            pg.on('pageerror', lambda e: errors.append(str(e)))
            pg.goto(url); pg.wait_for_selector('#senden:enabled')
            pg.locator('#text').fill('Bitte die Demo pruefen.'); pg.click('#senden')
            pg.wait_for_function("() => document.getElementById('text').value === ''")
            result = first.call('next'); assert result['type'] == 'job'; a = result['attempt']['id']
            first.call('event', {'attempt': a, 'kind': 'question', 'text': 'Soll der Knopf blau sein?'})
            pg.wait_for_function("() => document.getElementById('text').placeholder.includes('Antwort')")
            pg.locator('#text').fill('Ja, blau.')
            first.call('event', {'attempt': a, 'kind': 'progress', 'text': 'Warte auf die Antwort.'})
            pg.wait_for_timeout(1100)
            assert pg.locator('#text').input_value() == 'Ja, blau.'
            pg.click('#senden'); pg.wait_for_function("() => document.getElementById('text').value === ''")
            assert first.call('event', {'attempt': a, 'kind': 'heartbeat'})['answer'] == 'Ja, blau.'
            done = first.call('finish', {'attempt': a, 'text': 'Der Knopf ist blau.', 'html': '<!doctype html><html><body><button style="color:blue">Demo</button></body></html>'}, timeout=120)
            assert done['state'] == 'done' and done['result_version'] == expected_version
            pg.wait_for_function("ending => document.getElementById('frame').src.endsWith(ending)", arg=f'/{expected_version:03}.html')
            pg.wait_for_function("() => document.getElementById('msgs').textContent.includes('Der Knopf ist blau.')")
            pg.locator('#text').fill('Diese Arbeit wird abgebrochen'); pg.click('#senden')
            pg.wait_for_function("() => document.getElementById('text').value === ''")
            old = first.call('next')
            pg.wait_for_selector('#jobCancel')
            api(server, 'bind', {'project': 'demo', 'session': second.data['id']})
            with pytest.raises(RuntimeError, match='stale_attempt'):
                first.call('finish', {'attempt': old['attempt']['id'], 'text': 'Zu spaet'})
            assert second.call('next')['type'] == 'idle'
            pg.wait_for_function("() => document.getElementById('status').textContent.includes('codex-test')")
            pg.wait_for_function("() => !document.getElementById('jobCancel')")
            assert pg.locator('#terminalSessions, #sessionDialog, #chatAgentAction, #syncVersionen, #exportWb, #importWb').count() == 0
            assert not errors, errors
        finally: browser.close()


def test_cancel_button_frees_a_stuck_job_without_detaching(server):
    from playwright.sync_api import sync_playwright
    instance, tmp, url = server; client = registered(server)
    api(server, 'bind', {'project': 'demo', 'session': client.data['id']})
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            pg = browser.new_page(viewport={'width': 1500, 'height': 950})
            pg.goto(url); pg.wait_for_selector('#senden:enabled')
            pg.locator('#text').fill('Haengt fest'); pg.click('#senden')
            pg.wait_for_function("() => document.getElementById('text').value === ''")
            job = client.call('next'); assert job['type'] == 'job'
            pg.wait_for_selector('#jobCancel'); pg.click('#jobCancel')
            pg.wait_for_function("() => document.getElementById('msgs').textContent.includes('Abgebrochen')")
            assert instance.broker.status()['bindings']['demo']['session'] == client.data['id']
            with pytest.raises(RuntimeError, match='stale_attempt'):
                client.call('finish', {'attempt': job['attempt']['id'], 'text': 'Zu spaet'})
            # The cancelled message can be re-queued from the chat without retyping it.
            pg.wait_for_selector('[data-retry]'); pg.click('[data-retry]')
            pg.wait_for_function("() => document.getElementById('msgs').textContent.includes('Wartet')")
            again = client.call('next')
            assert again['type'] == 'job' and again['job']['id'] == job['job']['id'] and again['attempt']['id'] != job['attempt']['id']
            assert pg.locator('[data-retry]').count() == 0
        finally: browser.close()
