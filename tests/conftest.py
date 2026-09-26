"""Shared fixtures: a real local server on a random port with one demo project."""
import json
import sys
import threading
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from werkbank.server import Server, UI  # noqa: E402
from werkbank.terminal import Client  # noqa: E402

MOCKUP = '''<!doctype html><html lang="de"><meta charset="utf-8"><title>Demo</title>
<style>body{font:18px system-ui;margin:24px}#tap{margin:85px 120px 35px;padding:20px}
#scroll{height:270px;overflow:auto;border:1px solid #888}#inside{height:1100px;padding:25px}</style>
<h1>Demo-Oberflaeche</h1><button id="tap" data-id="counter">Zähler: 0</button>
<div id="scroll"><div id="inside">Langer Inhalt zum Wiederherstellen der Scrollstelle.</div></div>
<script>let count=0; const button=document.getElementById('tap');
function paint(){button.textContent='Zähler: '+count;}
button.onclick=()=>{count++;paint();};
window.__zustand=()=>({count}); window.__setzeZustand=s=>{count=s.count;paint();};
</script></html>'''


def make_projects(root, versions=(1,)):
    projects = root / 'projects'
    for number in versions:
        file = projects / 'demo' / 'v' / f'{number:03}.html'
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(MOCKUP.replace('Demo-Oberflaeche', f'Demo-Oberflaeche V{number}'), encoding='utf-8')
    (projects / 'demo' / 'projekt.json').write_text(json.dumps({'name': 'Demo'}), encoding='utf-8')
    return projects


@pytest.fixture
def server(tmp_path):
    projects = make_projects(tmp_path)
    instance = Server(0, tmp_path / 'data', UI, projects, root=tmp_path)
    thread = threading.Thread(target=instance.serve_forever, daemon=True); thread.start()
    try:
        yield instance, tmp_path, f'http://127.0.0.1:{instance.server_port}'
    finally:
        instance.shutdown(); thread.join(); instance.server_close()


@pytest.fixture
def empty_server(tmp_path):
    (tmp_path / 'projects').mkdir()
    instance = Server(0, tmp_path / 'data', UI, tmp_path / 'projects', root=tmp_path)
    thread = threading.Thread(target=instance.serve_forever, daemon=True); thread.start()
    try:
        yield instance, tmp_path, f'http://127.0.0.1:{instance.server_port}'
    finally:
        instance.shutdown(); thread.join(); instance.server_close()


def api(server, action, data):
    instance, _, url = server
    request = urllib.request.Request(url + '/api/agents/' + action, data=json.dumps(data).encode(),
        headers={'Content-Type': 'application/json', 'X-Werkbank-Token': instance.token})
    with urllib.request.urlopen(request, timeout=30) as r: return json.load(r)


def registered(server, provider='claude', filename='profile.json'):
    _, tmp, url = server
    pair = api(server, 'pair', {'provider': provider, 'name': provider + '-test'})
    client = Client(tmp / filename, url)
    client.register(pair['code'], 'existing-' + provider)
    client.call('ready', {'challenge': client.data['challenge'], 'protocol': 1})
    return client


def submit(server, key, text='Bitte pruefen', project='demo', version=1, dispatch=True, **extra):
    instance = server[0]
    data = {'projekt': project, 'version': version, 'text': text, 'chips': [], **extra}
    return instance.store.submit(key, data, dispatch=instance.broker.enqueue if dispatch else None)
