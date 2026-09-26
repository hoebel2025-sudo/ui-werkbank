"""Claude asyncRewake hook: local event waiting, no model calls or agent processes."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid

from .terminal import Client, ROOT, interpreter

WAKE_TEXT = ('Werkbank: Ein Chat-Auftrag oder eine Antwort liegt vor. '
             'Arbeite nach "{agent_doc}" (einmal lesen, falls noch nicht geschehen). '
             'Fuehre dieses Argumentarray als EINEN Prozess aus (Pfade einzeln quoten):\n{args}\n'
             'Bearbeite den gelieferten Versuch und gib das Ergebnis mit finish ab. '
             'Bei Rueckfrage question senden und Turn beenden. Bei review, waiting oder idle '
             'den Turn beenden. Kein next-Polling: Der Stop-Hook wartet und weckt erneut. '
             'Eine direkt eingegebene Benutzeraufgabe hat Vorrang.')


def pointer_path(root, identity):
    name = hashlib.sha256(identity.encode()).hexdigest()[:24]
    return Path(root) / '.local/terminal/wake' / (name + '.json')


def save(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    temp.write_text(json.dumps(data, ensure_ascii=True, indent=2), encoding='utf-8')
    os.replace(temp, path)


def enable(client, root=ROOT, mode='claude-hook', extra=None):
    """Select exactly one workbench connection for this already registered identity."""
    client.call('transport', {'mode': mode})
    pointer = pointer_path(root, client.data['identity'])
    previous = json.loads(pointer.read_text(encoding='utf-8')) if pointer.exists() else {}
    same = previous.get('profile') == str(client.path.resolve())
    data = {'profile': str(client.path.resolve()), 'identity': client.data['identity'],
            'generation': previous['generation'] if same else uuid.uuid4().hex, **(extra or {})}
    save(pointer, data)
    # Explicit re-attach allows retrying a wake lost because the host was interrupted.
    receipt = pointer.with_suffix('.delivered.json')
    receipt.unlink(missing_ok=True)
    return pointer


class Lease:
    """OS-owned file lock; released on crash, never steal from a living watcher."""
    def __init__(self, path): self.path = path; self.stream = None
    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        stream = self.path.open('a+b')
        if stream.seek(0, os.SEEK_END) == 0: stream.write(b'0'); stream.flush()
        stream.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            stream.close(); return False
        self.stream = stream
        return True
    def __exit__(self, *unused):
        if self.stream:
            self.stream.seek(0)
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(self.stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.stream, fcntl.LOCK_UN)
            self.stream.close()


def audit(pointer, kind, **details):
    with pointer.with_suffix('.events.jsonl').open('a', encoding='utf-8') as stream:
        stream.write(json.dumps({'time': time.time(), 'event': kind, **details}, ensure_ascii=True) + '\n')


def process_name(pid):
    try:
        if os.name == 'nt':
            script = "(Get-CimInstance Win32_Process -Filter ('ProcessId=" + str(int(pid)) + "')).Name"
            result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', script],
                                    capture_output=True, text=True, timeout=15, creationflags=subprocess.CREATE_NO_WINDOW)
        else:
            result = subprocess.run(['ps', '-o', 'comm=', '-p', str(int(pid))], capture_output=True, text=True, timeout=10)
        return result.stdout.strip() if result.returncode == 0 else ''
    except (OSError, subprocess.SubprocessError, ValueError):
        return ''


def guardian():
    """The Claude host process, if it spawned this hook directly. Used to stop orphaned waiters."""
    parent = os.getppid()
    name = Path(process_name(parent)).name.lower()
    if parent > 1 and any(tag in name for tag in ('claude', 'node')):
        return parent
    return None


def host_alive(pid):
    if pid is None: return True
    if os.name == 'nt':
        return bool(process_name(pid))
    if os.getppid() != pid: return False
    try:
        os.kill(pid, 0); return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def wake_command(profile, marker):
    """Argument vector for the woken session. No message text or credentials are interpolated."""
    return [interpreter(), '-B', str(Path(ROOT) / 'werkbank.py'), 'terminal', '--profile', str(profile),
            'next', '--once', '--wake-marker', marker]


def watch(hook, root=ROOT, max_seconds=604700):
    identity = hook.get('session_id')
    if not isinstance(identity, str) or not identity: return 0
    root = Path(root)
    pointer = pointer_path(root, identity)
    if not pointer.exists(): return 0
    host = guardian()
    with Lease(pointer.with_suffix('.lock')) as acquired:
        if not acquired: return 0
        audit(pointer, 'armed', session=identity, hook=hook.get('hook_event_name'), pid=os.getpid(), host=host)
        deadline = time.monotonic() + max_seconds
        errors = 0
        while time.monotonic() < deadline:
            if not pointer.exists(): return 0
            if not host_alive(host):
                audit(pointer, 'host_gone'); return 0
            config = json.loads(pointer.read_text(encoding='utf-8'))
            profile = Path(config['profile']).resolve()
            if not profile.is_relative_to(root.resolve() / '.local/terminal'): return 0
            client = Client(profile)
            if client.data.get('provider') != 'claude' or client.data.get('identity') != identity: return 0
            receipt = pointer.with_suffix('.delivered.json')
            previous = json.loads(receipt.read_text(encoding='utf-8')) if receipt.exists() else {}
            after = previous.get('marker', '') if previous.get('generation') == config['generation'] else ''
            try:
                result = client.call('wake', {'project': client.data.get('project'), 'after': after,
                                              'timeout': min(20, max(0, deadline - time.monotonic()))}, timeout=30)
                errors = 0
            except (OSError, RuntimeError) as exc:
                errors += 1
                if errors == 1: audit(pointer, 'reconnecting', error=type(exc).__name__)
                # Backoff is only for an unavailable service, not normal delivery.
                time.sleep(min(errors, 5))
                continue
            # A concurrent attach may have selected another local workbench/profile.
            if not pointer.exists(): return 0
            if json.loads(pointer.read_text(encoding='utf-8')) != config: continue
            if result['type'] == 'handshake':
                client.call('ready', {'challenge': client.data['challenge'], 'protocol': 1})
                # A new server boot lost its delivery receipts. Only queued work or
                # a still-valid human answer can wake again; claimed work is fenced
                # by Broker.recover(). Do not strand an unclaimed queued message.
                receipt.unlink(missing_ok=True)
                continue
            if result['type'] in {'detached', 'closed'}:
                audit(pointer, result['type']); return 0
            if result['type'] == 'wake':
                save(receipt, {'generation': config['generation'], 'marker': result['marker']})
                audit(pointer, 'wake', marker=result['marker'], job=result['job'])
                args = wake_command(profile, result['marker'])
                print(WAKE_TEXT.format(agent_doc=root / 'docs/AGENT.md', args=json.dumps(args, ensure_ascii=True)),
                      file=sys.stderr, flush=True)
                return 2
        audit(pointer, 'expired')
        return 0


def main():
    try:
        hook = json.load(sys.stdin)
        if not isinstance(hook, dict): return 0
        return watch(hook)
    except (ValueError, OSError, RuntimeError, KeyError) as exc:
        # Only a genuine work event exits 2; failures never create model error loops.
        print('Werkbank-Hook: ' + type(exc).__name__ + '. Bei Bedarf /uiworkbench erneut aufrufen.', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
