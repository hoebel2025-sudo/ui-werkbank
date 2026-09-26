"""Wake the owning, existing Codex CLI via its native queue. No model polling."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time

from .terminal import Client, ROOT, interpreter
from .wake import Lease, audit, pointer_path, save, wake_command

CODEX_NAMES = {'codex', 'codex.exe'}
WAKE_TEXT = ('Werkbank: Neuer Chat-Auftrag oder Antwort. Arbeite nach "{agent_doc}". '
             'Fuehre dieses Argumentarray als einen Prozess aus:\n{args}\n'
             'Bearbeite nur den zurueckgegebenen Auftrag und gib die Antwort mit finish ab. '
             'Nach finish/question oder idle/waiting den Turn beenden, kein Polling. '
             'Ein veraltetes Wecksignal ignorieren; direkte Benutzerauftraege gehen vor.')


def _spawn_options():
    return {'creationflags': subprocess.CREATE_NO_WINDOW} if os.name == 'nt' else {}


def _posix_process(pid):
    """(parent pid, executable) of a live process, else None."""
    result = subprocess.run(['ps', '-o', 'ppid=,comm=', '-p', str(int(pid))], capture_output=True, text=True, timeout=10)
    line = result.stdout.strip()
    if result.returncode or not line: return None
    parent, _, command = line.partition(' ')
    command = command.strip()
    exe = Path('/proc') / str(pid) / 'exe'
    if exe.exists():
        try: command = os.readlink(exe)
        except OSError: pass
    return int(parent), command


def owner():
    """The Codex CLI process this call runs inside. Walks only our ancestors."""
    if os.name == 'nt':
        script = ("$p=Get-CimInstance Win32_Process -Filter ('ProcessId='+$PID); while($p.ParentProcessId) { "
                  "$p=Get-CimInstance Win32_Process -Filter ('ProcessId='+$p.ParentProcessId); if(!$p){break}; "
                  "if($p.Name -eq 'codex.exe'){ @{pid=[int]$p.ProcessId; executable=$p.ExecutablePath} | ConvertTo-Json -Compress; exit 0 } }; exit 1")
        result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', script],
                                capture_output=True, text=True, timeout=15, creationflags=subprocess.CREATE_NO_WINDOW)
        if result.returncode or not result.stdout.strip():
            raise RuntimeError('Keine bestehende Codex-CLI in diesem Prozesszweig gefunden. In der gewuenschten Sitzung uiworkbench aufrufen.')
        data = json.loads(result.stdout)
    else:
        pid, data = os.getpid(), None
        for _ in range(40):
            info = _posix_process(pid)
            if not info or info[0] <= 0: break
            parent, command = info
            if Path(command).name in CODEX_NAMES:
                data = {'pid': pid, 'executable': command}; break
            if parent == pid or parent <= 1: break
            pid = parent
        if not data:
            raise RuntimeError('Keine bestehende Codex-CLI in diesem Prozesszweig gefunden. In der gewuenschten Sitzung uiworkbench aufrufen.')
    data['created'] = process_stamp(data['pid'])
    if data['created'] is None: raise RuntimeError('Die Codex-Sitzung wurde beendet.')
    return data


def process_stamp(pid):
    """Stable identity of a live process (creation time); None once it has exited."""
    if os.name == 'nt':
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
        kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle: return None
        try:
            created, exited, kern, user = (wintypes.FILETIME() for _ in range(4)); code = wintypes.DWORD()
            if not kernel.GetExitCodeProcess(handle, ctypes.byref(code)) or code.value != 259: return None
            if not kernel.GetProcessTimes(handle, ctypes.byref(created), ctypes.byref(exited), ctypes.byref(kern), ctypes.byref(user)): return None
            return (created.dwHighDateTime << 32) | created.dwLowDateTime
        finally: kernel.CloseHandle(handle)
    try:
        result = subprocess.run(['ps', '-o', 'lstart=', '-p', str(int(pid))], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    stamp = result.stdout.strip()
    return stamp if result.returncode == 0 and stamp else None


def prepare(identity):
    if identity != os.environ.get('CODEX_THREAD_ID'):
        raise RuntimeError('Nur diese aufrufende Codex-Sitzung darf sich selbst verbinden (CODEX_THREAD_ID).')
    process = owner()
    check = subprocess.run([process['executable'], 'queue', '--help'], capture_output=True, text=True, timeout=10, **_spawn_options())
    if check.returncode or '--thread' not in check.stdout:
        raise RuntimeError('Diese Codex-CLI unterstuetzt "codex queue --thread" noch nicht. CLI aktualisieren.')
    return process


def enable(client, root=ROOT, prepared=None):
    process = prepared or prepare(client.data['identity'])
    from .wake import enable as select_profile
    pointer = select_profile(client, root, mode='codex-queue', extra={'owner': process})
    logs = pointer.with_suffix('.log')
    options = _spawn_options() if os.name == 'nt' else {'start_new_session': True}
    with logs.open('ab') as stream:
        subprocess.Popen([interpreter(), '-B', str(Path(root) / 'werkbank.py'), 'codex-watch', '--identity', client.data['identity']],
            cwd=root, stdin=subprocess.DEVNULL, stdout=stream, stderr=stream, **options)
    return pointer


def watch(identity, root=ROOT, max_seconds=604700):
    root = Path(root)
    pointer = pointer_path(root, identity)
    with Lease(pointer.with_suffix('.lock')) as acquired:
        if not acquired: return 0
        deadline = time.monotonic() + max_seconds
        errors = 0
        while time.monotonic() < deadline and pointer.exists():
            config = json.loads(pointer.read_text(encoding='utf-8'))
            process = config.get('owner', {})
            if not process or process_stamp(process['pid']) != process['created']: return 0
            profile = Path(config['profile']).resolve()
            if not profile.is_relative_to(root.resolve() / '.local/terminal'): return 1
            client = Client(profile)
            if client.data.get('provider') != 'codex' or client.data.get('identity') != identity: return 1
            receipt = pointer.with_suffix('.delivered.json')
            previous = json.loads(receipt.read_text(encoding='utf-8')) if receipt.exists() else {}
            after = previous.get('marker', '') if previous.get('generation') == config['generation'] else ''
            try:
                result = client.call('wake', {'project': client.data.get('project'), 'after': after,
                    'timeout': min(20, max(0, deadline - time.monotonic()))}, timeout=30)
            except (OSError, RuntimeError):
                errors += 1
                if errors == 1: audit(pointer, 'reconnecting')
                time.sleep(min(errors, 5)); continue
            errors = 0
            if not pointer.exists(): return 0
            if json.loads(pointer.read_text(encoding='utf-8')) != config: continue
            if result['type'] == 'handshake':
                client.call('ready', {'challenge': client.data['challenge'], 'protocol': 1})
                receipt.unlink(missing_ok=True); continue
            if result['type'] in {'detached', 'closed'}:
                audit(pointer, result['type']); return 0
            if result['type'] != 'wake': continue
            if process_stamp(process['pid']) != process['created']: return 0
            message = WAKE_TEXT.format(agent_doc=root / 'docs/AGENT.md',
                                       args=json.dumps(wake_command(profile, result['marker']), ensure_ascii=True))
            # A short-lived queue utility targets this exact thread; no agent is launched.
            try:
                delivered = subprocess.run([process['executable'], 'queue', '--thread', identity, '--message', message],
                    cwd=root, capture_output=True, text=True, timeout=20, **_spawn_options())
            except (OSError, subprocess.TimeoutExpired):
                audit(pointer, 'queue_failed', reason='process_error')
                client.call('transport', {'mode': 'terminal-pull'}); return 1
            if delivered.returncode:
                audit(pointer, 'queue_failed', exit_code=delivered.returncode, stderr=delivered.stderr[-500:])
                client.call('transport', {'mode': 'terminal-pull'}); return 1
            save(receipt, {'generation': config['generation'], 'marker': result['marker']})
            audit(pointer, 'queued', marker=result['marker'], job=result['job'])
        return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__); parser.add_argument('--identity', required=True)
    return watch(parser.parse_args(argv).identity)


if __name__ == '__main__': raise SystemExit(main())
