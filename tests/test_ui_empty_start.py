"""An empty workbench: no project, no version. Project creation in the browser, automatic
binding of the live session, a first job on a blank base and version 1 as the result."""
import json

import pytest

from conftest import registered
from werkbank.terminal import Client


def test_first_project_from_the_browser_binds_live_session_and_publishes_version_one(empty_server, monkeypatch):
    from playwright.sync_api import sync_playwright
    import werkbank.terminal as terminal
    instance, tmp, url = empty_server
    monkeypatch.setattr(terminal, 'ROOT', tmp)
    client = registered(empty_server)
    client.call('transport', {'mode': 'claude-hook'})
    errors = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            pg = browser.new_page(viewport={'width': 1500, 'height': 950})
            pg.on('pageerror', lambda e: errors.append(str(e)))
            pg.goto(url); pg.wait_for_selector('#senden:enabled')
            pg.wait_for_selector('#neu:not([hidden])')
            assert 'Noch kein Projekt' in (pg.frame_locator('#frame').locator('body').inner_text())
            # Sending without a project is refused with a hint, nothing is stored.
            pg.locator('#text').fill('Hallo'); pg.click('#senden')
            pg.wait_for_function("() => document.getElementById('pickhinweis').textContent.includes('Zuerst ein Projekt')")
            assert not [k for k in instance.store.state()['documents'] if k.startswith('nachrichten/')]
            # The armed session shows up as waiting for a project.
            assert client.call('wake', {'timeout': 0})['type'] == 'idle'
            pg.wait_for_function("() => document.getElementById('status').textContent.includes('wartet auf ein Projekt')")
            pg.fill('#neuName', 'Erste Oberfläche'); pg.click('#neuOk')
            pg.wait_for_function("() => document.getElementById('chatProj').value === 'erste-oberflache'")
            assert 'projekte/erste-oberflache' in instance.store.state()['documents']
            # sessions.js binds the live session to the new chat project on its own.
            pg.wait_for_function("() => document.getElementById('status').textContent.includes('claude-test · bereit')", timeout=10000)
            assert instance.broker.status()['bindings']['erste-oberflache']['session'] == client.data['id']
            pg.locator('#text').fill('Baue eine Startseite mit einem Knopf.'); pg.click('#senden')
            pg.wait_for_function("() => document.getElementById('text').value === ''")
            assert client.call('wake', {'timeout': 0})['type'] == 'wake'
            job = client.once()
            assert job['type'] == 'job' and job['base_version'] is None
            assert 'leere Seite' in job['instructions']
            result = tmp / '.local/terminal/work' / job['attempt']['id'] / 'result.html'
            assert result.read_text(encoding='utf-8').startswith('<!doctype html>')
            result.write_text('<!doctype html><html><body><button id="start">Los</button></body></html>', encoding='utf-8')
            done = client.call('finish', {'attempt': job['attempt']['id'], 'text': 'Startseite mit Knopf', 'html': result.read_text(encoding='utf-8')}, timeout=120)
            assert done['result_version'] == 1
            pg.wait_for_function("() => document.getElementById('frame').src.endsWith('/p/erste-oberflache/v/001.html')")
            pg.wait_for_function("() => document.getElementById('msgs').textContent.includes('erzeugte Version 1')")
            assert (tmp / 'projects/erste-oberflache/v/001.html').is_file()
            assert not errors, errors
        finally: browser.close()
