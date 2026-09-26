import http.client
import json
import threading

import pytest

from werkbank.server import Server


@pytest.fixture
def server(tmp_path):
    ui = tmp_path / 'ui'; ui.mkdir()
    (ui / 'index.html').write_text('<title>Local workbench</title>')
    (tmp_path / 'projects').mkdir()
    (tmp_path / '.git').mkdir(); (tmp_path / '.git/config').write_text('secret')
    instance = Server(0, tmp_path / 'state', ui, tmp_path / 'projects', root=tmp_path)
    thread = threading.Thread(target=instance.serve_forever, daemon=True); thread.start()
    try: yield instance
    finally: instance.shutdown(); thread.join(); instance.server_close()


def call(server, method, path, data=None, headers=None):
    conn = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=5)
    if isinstance(data, dict): data = json.dumps(data).encode()
    try:
        conn.request(method, path, body=data, headers=headers or {})
        response = conn.getresponse()
        return response.status, response.read(), dict(response.getheaders())
    finally: conn.close()


def test_project_is_not_a_general_file_server(server):
    assert call(server, 'GET', '/')[0] == 200
    for path in ['/.git/config', '/.local/werkbank/werkbank.sqlite3', '/werkbank.py', '/ui/index.html',
                 '/p/../../.local/terminal/default.json', '/p/%2e%2e%5c%2e%2e%5cREADME.md']:
        assert call(server, 'GET', path)[0] == 404


def test_session_identifies_the_app_and_its_root(server):
    session = json.loads(call(server, 'GET', '/api/session')[1])
    assert session['app'] == 'ui-werkbank' and session['attach_protocol'] == 1 and session['native_wake_protocol'] == 1
    assert session['project_root'] == str(server.broker.root)


def test_writes_require_token_and_same_origin(server):
    body = {'name': 'Test'}
    assert call(server, 'PUT', '/api/doc/projekte/test', body)[0] == 401
    headers = {'X-Werkbank-Token': server.token, 'Content-Type': 'application/json', 'Origin': 'https://outside.invalid'}
    assert call(server, 'PUT', '/api/doc/projekte/test', body, headers)[0] == 403
    headers['Origin'] = f'http://127.0.0.1:{server.server_port}'
    assert call(server, 'PUT', '/api/doc/projekte/test', body, headers)[0] == 200
    state = json.loads(call(server, 'GET', '/api/state')[1])
    assert state['documents']['projekte/test']['name'] == 'Test'
    assert not state['agent']['connected']


def test_rejects_host_rebinding_and_foreign_origin_reads(server):
    assert call(server, 'GET', '/api/session', headers={'Host': 'outside.invalid'})[0] == 403
    assert call(server, 'GET', '/api/state', headers={'Origin': 'https://outside.invalid'})[0] == 403


@pytest.mark.parametrize('body,mime,status', [(b'{', 'application/json', 400), (b'[]', 'application/json', 400),
                                             (b'{"value":NaN}', 'application/json', 400), (b'{}', 'text/plain', 415)])
def test_invalid_request_is_visible(server, body, mime, status):
    headers = {'X-Werkbank-Token': server.token, 'Content-Type': mime}
    assert call(server, 'PUT', '/api/doc/ui/test', body, headers)[0] == status
    assert 'ui/test' not in json.loads(call(server, 'GET', '/api/state')[1])['documents']


def test_incremental_state_and_session_token(server):
    code, payload, headers = call(server, 'GET', '/api/session')
    assert code == 200 and json.loads(payload)['token'] == server.token
    assert headers['Cache-Control'] == 'no-store'
    state = json.loads(call(server, 'GET', '/api/state')[1])
    assert 'documents' not in json.loads(call(server, 'GET', '/api/state?since=' + str(state['revision']))[1])


def test_export_import_through_http(server):
    headers = {'X-Werkbank-Token': server.token, 'Content-Type': 'application/json'}
    assert call(server, 'PUT', '/api/doc/projekte/test', {'name': 'Projekt'}, headers)[0] == 200
    status, backup, response_headers = call(server, 'GET', '/api/export')
    assert status == 200 and response_headers['Content-Type'] == 'application/zip'
    status, response, _ = call(server, 'POST', '/api/import', backup, {'X-Werkbank-Token': server.token})
    assert status == 200 and json.loads(response)['added_documents'] == 0
