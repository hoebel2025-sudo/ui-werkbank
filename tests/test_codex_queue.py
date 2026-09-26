import json
import os
from types import SimpleNamespace

import pytest

from conftest import registered, api, submit
from werkbank import codex_queue
from werkbank.wake import enable
from werkbank.uiworkbench import attach


def configured(server, monkeypatch):
    client = registered(server, 'codex', '.local/terminal/codex.json')
    client.data['project'] = 'demo'
    client.path.write_text(json.dumps(client.data), encoding='utf-8')
    api(server, 'bind', {'project': 'demo', 'session': client.data['id'], 'auto': True})
    pointer = enable(client, server[1], mode='codex-queue', extra={'owner': {'pid': 123, 'created': 17, 'executable': 'test-codex'}})
    monkeypatch.setattr(codex_queue, 'process_stamp', lambda pid: 17)
    return client, pointer


def test_queue_waits_locally_and_delivers_once_without_claiming_or_launching_agent(server, monkeypatch):
    client, pointer = configured(server, monkeypatch); calls = []
    def run(args, **kwargs):
        calls.append(args); return SimpleNamespace(returncode=0, stderr='')
    monkeypatch.setattr(codex_queue.subprocess, 'run', run)
    assert codex_queue.watch(client.data['identity'], server[1], .05) == 0
    assert not calls
    submit(server, 'work', 'Test')
    assert codex_queue.watch(client.data['identity'], server[1], .1) == 0
    assert len(calls) == 1
    args = calls[0]
    assert args[:4] == ['test-codex', 'queue', '--thread', client.data['identity']]
    assert '--wake-marker' in args[-1] and client.data['secret'] not in args[-1] and 'werkbank.py' in args[-1]
    assert server[0].broker.status()['jobs']['work']['state'] == 'queued'
    assert codex_queue.watch(client.data['identity'], server[1], .05) == 0 and len(calls) == 1
    marker = json.loads(pointer.with_suffix('.delivered.json').read_text())['marker']
    assert client.call('next', {'wake_marker': marker})['type'] == 'job'


def test_dead_owner_or_pid_reuse_does_not_deliver(server, monkeypatch):
    client, _ = configured(server, monkeypatch)
    submit(server, 'work', 'Test')
    monkeypatch.setattr(codex_queue, 'process_stamp', lambda pid: 999)
    monkeypatch.setattr(codex_queue.subprocess, 'run', lambda *a, **kw: pytest.fail('Dead owner must not receive work'))
    assert codex_queue.watch(client.data['identity'], server[1], 1) == 0


def test_wrong_identity_or_unavailable_queue_does_not_replace_old_binding(server, monkeypatch):
    instance, root, url = server
    old = attach('claude', 'existing-claude', root=root, url=url)
    monkeypatch.setenv('CODEX_THREAD_ID', 'the-current-thread')
    with pytest.raises(RuntimeError, match='aufrufende'):
        attach('codex', 'someone-else', root=root, url=url)
    assert instance.broker.status()['bindings']['demo']['session'] == old['session']
    assert len(instance.broker.clients()) == 1


@pytest.mark.skipif(os.name == 'nt', reason='POSIX-Prozesskette')
def test_owner_walks_only_ancestors_on_posix(monkeypatch):
    chain = {100: (90, '/bin/zsh'), 90: (80, '/opt/homebrew/bin/codex'), 80: (1, '/sbin/launchd')}
    monkeypatch.setattr(codex_queue.os, 'getpid', lambda: 100)
    monkeypatch.setattr(codex_queue, '_posix_process', lambda pid: chain.get(pid))
    monkeypatch.setattr(codex_queue, 'process_stamp', lambda pid: 'Sa 26 Sep 14:12:16 2026' if pid == 90 else None)
    owner = codex_queue.owner()
    assert owner == {'pid': 90, 'executable': '/opt/homebrew/bin/codex', 'created': 'Sa 26 Sep 14:12:16 2026'}
    chain[90] = (80, '/usr/bin/python3')
    with pytest.raises(RuntimeError, match='Codex-CLI'):
        codex_queue.owner()


@pytest.mark.skipif(os.name == 'nt', reason='POSIX-Prozesskette')
def test_process_stamp_reports_live_process_and_none_for_dead():
    assert codex_queue.process_stamp(os.getpid())
    assert codex_queue.process_stamp(2 ** 22 - 1) is None
