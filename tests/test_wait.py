import threading
import time

import pytest

from conftest import registered, api, submit
import werkbank.terminal as terminal


def waiting_job(server, state):
    instance, tmp, _ = server
    client = registered(server)
    api(server, 'bind', {'project': 'demo', 'session': client.data['id']})
    client.data['project'] = 'demo'
    submit(server, 'wait-task', 'Bitte prüfen', dispatch=False)
    api(server, 'enqueue', {'message': 'wait-task'})
    job = client.call('next')
    client.call('event', {'attempt': job['attempt']['id'], 'kind': 'question', 'text': 'Welche Farbe?'})
    return client, job


def test_waits_without_reissuing_context_or_changing_attempt(server, monkeypatch):
    instance, tmp, _ = server
    client, job = waiting_job(server, 'question')
    monkeypatch.setattr(terminal, 'ROOT', tmp)
    start = time.monotonic()
    result = client.next(.4)
    assert time.monotonic() - start >= .4
    assert result['type'] == 'waiting' and result['state'] == 'question'
    assert 'context_file' not in result and not (tmp / '.local/terminal/work').exists()
    assert instance.broker.status()['jobs']['wait-task']['attempt'] == job['attempt']['id']
    assert instance.broker.status()['jobs']['wait-task']['state'] == 'question'


def test_wait_returns_user_answer_without_claiming_a_second_attempt(server, monkeypatch):
    instance, tmp, _ = server
    client, job = waiting_job(server, 'question')
    monkeypatch.setattr(terminal, 'ROOT', tmp)
    timer = threading.Timer(.15, lambda: api(server, 'answer', {'job': 'wait-task', 'text': 'Blau'}))
    timer.start()
    try:
        result = client.next(3)
        assert result['type'] == 'active' and result['job']['answer'] == 'Blau'
        assert result['attempt']['id'] == job['attempt']['id']
        assert instance.broker.status()['jobs']['wait-task']['state'] == 'working'
    finally: timer.join(timeout=5)


def test_wait_stops_after_cancel_and_detach(server, monkeypatch):
    instance, tmp, _ = server
    client, job = waiting_job(server, 'question')
    monkeypatch.setattr(terminal, 'ROOT', tmp)
    def cancel():
        api(server, 'cancel', {'job': 'wait-task'})
        api(server, 'detach', {'session': client.data['id']})
    timer = threading.Timer(.15, cancel); timer.start()
    try:
        assert client.next(3)['type'] == 'detached'
        assert not (tmp / '.local/terminal/work').exists()
    finally: timer.join(timeout=5)
