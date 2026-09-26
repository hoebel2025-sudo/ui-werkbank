import json
import threading
import time

import pytest

from conftest import registered, api, submit
from werkbank.broker import Broker
from werkbank.terminal import Client, compact_context
from werkbank.wake import Lease, enable, watch, wake_command


def connected(server):
    client = registered(server, filename='.local/terminal/claude.json')
    client.data['project'] = 'demo'
    client.path.write_text(json.dumps(client.data), encoding='utf-8')
    api(server, 'bind', {'project': 'demo', 'session': client.data['id'], 'auto': True})
    return client


def enqueue(server, key='event-task'):
    submit(server, key, 'Nur diese Testaufgabe', dispatch=False)
    return api(server, 'enqueue', {'message': key})


def wake(client, **kw):
    return client.call('wake', {'project': 'demo', 'timeout': 0, **kw}, timeout=5)


def test_event_wake_is_immediate_and_does_not_claim_work(server):
    client = connected(server); enable(client, server[1])
    result = []
    thread = threading.Thread(target=lambda: result.append(wake(client, timeout=3)))
    thread.start()
    time.sleep(.08)
    start = time.monotonic(); enqueue(server); thread.join(2)
    assert result and result[0]['type'] == 'wake'
    assert time.monotonic() - start < .6
    assert server[0].broker.status()['jobs']['event-task']['state'] == 'queued'
    assert wake(client, after=result[0]['marker'])['type'] == 'idle'
    job = client.call('next', {'wake_marker': result[0]['marker']})
    assert job['type'] == 'job'
    session = next(s for s in server[0].broker.status()['sessions'] if s['id'] == client.data['id'])
    assert session['wake_verified'] and session['last_wake']['seconds'] < 1
    assert not session['wake_armed']


def test_idle_hook_uses_no_agent_requests_and_deduplicates(server, capsys, monkeypatch):
    client = connected(server); pointer = enable(client, server[1]); calls = []
    original = Client.call
    def observed(self, action, *args, **kwargs):
        calls.append(action); return original(self, action, *args, **kwargs)
    monkeypatch.setattr(Client, 'call', observed)
    hook = {'session_id': client.data['identity'], 'cwd': str(server[1] / 'irgendwo/anders'), 'hook_event_name': 'Stop'}
    assert watch(hook, server[1], .1) == 0
    assert calls == ['wake'] and not server[0].broker.status()['jobs']
    enqueue(server)
    assert watch(hook, server[1], 1) == 2
    output = capsys.readouterr().err
    assert '--wake-marker' in output and client.data['secret'] not in output
    assert 'werkbank.py' in output and 'docs/AGENT.md' in output
    assert watch(hook, server[1], .1) == 0
    with Lease(pointer.with_suffix('.lock')) as acquired:
        assert acquired
        assert watch(hook, server[1], 1) == 0
    assert 'next' not in calls


def test_wake_command_is_absolute_and_runs_from_any_directory(server, tmp_path):
    import subprocess, sys, os
    client = connected(server); enable(client, server[1]); enqueue(server)
    marker = wake(client)['marker']
    args = wake_command(client.path, marker)
    assert args[2].endswith('werkbank.py') and os.path.isabs(args[2])
    elsewhere = tmp_path / 'elsewhere'; elsewhere.mkdir()
    result = subprocess.run(args, cwd=elsewhere, capture_output=True, text=True, timeout=60,
                            env={**os.environ, 'WERKBANK_NO_REEXEC': '1', 'PYTHONUTF8': '1'})
    assert result.returncode == 0, result.stderr
    delivered = json.loads(result.stdout)
    assert delivered['type'] == 'job' and delivered['result_file'].endswith('result.html')


def test_question_answer_wakes_once_and_publication_allows_next_message(server):
    client = connected(server); enable(client, server[1]); enqueue(server)
    first = wake(client)
    job = client.call('next', {'wake_marker': first['marker']})
    attempt = job['attempt']['id']
    client.call('event', {'attempt': attempt, 'kind': 'question', 'text': 'Welche Farbe?'})
    assert wake(client)['type'] == 'idle'
    api(server, 'answer', {'job': job['job']['id'], 'text': 'Blau'})
    answer = wake(client, after=first['marker'])
    assert answer['type'] == 'wake' and answer['marker'].startswith('answer:')
    assert wake(client, after=answer['marker'])['type'] == 'idle'
    assert client.call('next', {'wake_marker': answer['marker']})['attempt']['id'] == attempt
    client.call('finish', {'attempt': attempt, 'text': 'Vorschlag', 'html': '<body>Blau</body>'})
    enqueue(server, 'another-task')
    assert wake(client)['type'] == 'wake'
    assert client.once()['type'] == 'job'


def test_detach_wakes_waiter_and_fences_old_session(server):
    client = connected(server); enable(client, server[1]); result = []
    thread = threading.Thread(target=lambda: result.append(wake(client, timeout=3))); thread.start()
    time.sleep(.08)
    api(server, 'detach', {'session': client.data['id']})
    thread.join(1)
    assert result == [{'type': 'detached'}]
    enqueue(server)
    assert client.call('next')['type'] == 'idle'


def test_replacement_by_another_session_detaches_the_waiter(server):
    client = connected(server); enable(client, server[1]); result = []
    thread = threading.Thread(target=lambda: result.append(wake(client, timeout=3))); thread.start()
    time.sleep(.08)
    other = registered(server, 'codex', 'codex.json')
    api(server, 'bind', {'project': 'demo', 'session': other.data['id'], 'auto': True, 'takeover': True})
    thread.join(1)
    assert result == [{'type': 'detached'}]


def test_restart_preserves_published_result_but_fences_running_work(server):
    client = connected(server); enable(client, server[1]); enqueue(server)
    job = client.call('next'); attempt = job['attempt']['id']
    client.call('finish', {'attempt': attempt, 'text': 'Vorschlag', 'html': '<body>OK</body>'})
    broker = Broker(server[0].store, server[1])
    assert broker.status()['jobs']['event-task']['state'] == 'done'
    assert broker.candidate(attempt) == '<body>OK</body>'
    assert broker.wake(client.data['id'], 'demo', timeout=0)['type'] == 'handshake'
    enqueue(server, 'running-task'); client.call('next')
    broker = Broker(server[0].store, server[1])
    assert broker.status()['jobs']['running-task']['state'] == 'interrupted'


def test_wake_auth_provider_and_stale_marker(server):
    client = connected(server); enable(client, server[1])
    with pytest.raises(RuntimeError, match='wake_marker'):
        client.call('next', {'wake_marker': 'not-issued'})
    with pytest.raises(RuntimeError, match='wake_request'):
        wake(client, timeout=60)
    client.data['secret'] = 'invalid'
    with pytest.raises(RuntimeError, match='terminal_auth'): wake(client)
    other = registered(server, 'codex', 'codex.json')
    with pytest.raises(RuntimeError, match='transport'): wake(other)


def test_compact_context_keeps_different_states_and_all_original_data():
    original = {'message': {'text': 'SC1 und SC2', 'chips': [{'thumb': 'data:image/jpeg;base64,ABC', 'ref': 'SC1'}],
                            'zustand': {'selection': [1, 2], 'version': 6}},
                'snapshots': {'first': {'zustand': {'selection': [1, 2], 'version': 6}, 'thumb': 'data:image/jpeg;base64,ABC',
                                        'local_image': 'image.jpg', 'markierungen': [{'x': 2}]},
                              'second': {'zustand': {'selection': [3], 'version': 5}, 'thumb': 'not a preview'}}}
    before = json.dumps(original)
    compact = compact_context(original)
    assert json.dumps(original) == before
    assert 'thumb' not in compact['message']['chips'][0]
    first = compact['snapshots']['first']
    assert 'zustand' not in first and first['zustand_ref'] == '#/message/zustand'
    assert first['markierungen'] == [{'x': 2}] and first['local_image'] == 'image.jpg'
    assert compact['snapshots']['second'] == original['snapshots']['second']


def test_hook_ignores_other_sessions(server):
    client = connected(server); enable(client, server[1]); enqueue(server)
    assert watch({'session_id': 'not-connected', 'cwd': str(server[1])}, server[1], .1) == 0
    assert watch({'session_id': 42}, server[1], .1) == 0
    assert server[0].broker.status()['jobs']['event-task']['state'] == 'queued'


def test_hook_stops_when_its_host_process_is_gone(server, monkeypatch):
    import werkbank.wake as wake_module
    client = connected(server); pointer = enable(client, server[1]); enqueue(server)
    monkeypatch.setattr(wake_module, 'guardian', lambda: 4242)
    monkeypatch.setattr(wake_module, 'host_alive', lambda pid: False)
    hook = {'session_id': client.data['identity'], 'hook_event_name': 'Stop'}
    assert watch(hook, server[1], 1) == 0
    events = [json.loads(l)['event'] for l in pointer.with_suffix('.events.jsonl').read_text().splitlines()]
    assert events[-1] == 'host_gone'
    assert server[0].broker.status()['jobs']['event-task']['state'] == 'queued'


def test_completed_result_survives_manual_rebind_after_restart(server):
    client = connected(server)
    api(server, 'bind', {'project': 'demo', 'session': client.data['id'], 'auto': False})
    enqueue(server); job = client.call('next')
    client.call('finish', {'attempt': job['attempt']['id'], 'text': 'Vorschlag', 'html': '<body>OK</body>'})
    broker = Broker(server[0].store, server[1])
    broker.authenticate(client.data['id'], client.data['secret'])
    broker.ready(client.data['id'], client.data['challenge'], 1)
    assert not broker.status()['bindings']['demo']['enabled']
    broker.bind('demo', client.data['id'])
    assert broker.status()['jobs']['event-task']['state'] == 'done'


def test_close_releases_long_wait_without_sqlite_use_after_close(server):
    client = connected(server); enable(client, server[1]); results = []
    worker = threading.Thread(target=lambda: results.append(server[0].broker.wake(client.data['id'], 'demo', timeout=5)))
    worker.start(); time.sleep(.08)
    server[0].store.close(); worker.join(1)
    assert results == [{'type': 'closed'}]


def test_restart_redelivers_unclaimed_work_after_lost_wake(server, capsys):
    client = connected(server); enable(client, server[1]); enqueue(server)
    hook = {'session_id': client.data['identity'], 'hook_event_name': 'Stop'}
    assert watch(hook, server[1], 1) == 2
    server[0].broker = Broker(server[0].store, server[1])
    assert watch(hook, server[1], 1) == 2
    job = server[0].broker.status()['jobs']['event-task']
    assert job['state'] == 'queued' and job['attempt'] is None


@pytest.fixture(autouse=True)
def no_real_browser(monkeypatch):
    import werkbank.validate as validate
    from werkbank.store import digest
    monkeypatch.setattr(validate, 'validate', lambda html: {'ok': True, 'sha256': digest(html.encode('utf-8')), 'errors': [], 'external': []})
