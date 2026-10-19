#!/usr/bin/env python3
"""Small root SSH control entry point. Accepts JSON on stdin; never receives archive bytes."""
import fcntl
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys

sys.dont_write_bytecode = True
import tempfile


def write(path, value):
    fd, temporary = tempfile.mkstemp(prefix='.control-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as handle:
            json.dump(value, handle); handle.flush(); os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally: Path(temporary).unlink(missing_ok=True)


def control(payload, action):
    request = payload['request']
    if not re.fullmatch(r'[0-9]{1,20}', request['run_id']) or not re.fullmatch(r'[0-9]{1,6}', request['attempt']): raise ValueError()
    stage = Path('/var/tmp')/('deploy-platform-ci-'+request['run_id']+'-'+request['attempt'])
    lock = os.open('/run/lock/deploy-platform-ci-launch.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(lock, 'w') as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022: raise ValueError()
        fcntl.flock(handle, fcntl.LOCK_EX)
        if action == 'start':
            if stage.exists() or stage.is_symlink(): raise ValueError('delivery stage already exists; inspect its result before retrying')
            stage.mkdir(mode=0o700)
            if set(payload['files']) != {'remote-install.py', 'delivery.py'}: raise ValueError()
            for name, content in payload['files'].items():
                if not isinstance(content, str) or len(content) > 128*1024: raise ValueError()
                fd = os.open(stage/name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, 'w') as target: target.write(content)
            sys.path.insert(0, str(stage))
            from delivery import validate, check_url
            validate(request); check_url(payload['ticket']['url'])
            write(stage/'request.json', request)
            write(stage/'download.json', payload['ticket'])
            write(stage/'result.json', {'commit': request['commit'], 'phase': 'accepted'})
            unit = 'deploy-platform-ci-'+request['run_id']+'-'+request['attempt']
            result = subprocess.run(['systemd-run', '--no-block', '--unit='+unit, '--property=Type=oneshot', '--property=TimeoutStartSec=infinity', '--property=UMask=0077', '--property=Nice=10', '--property=MemoryHigh=256M', '/usr/bin/python3', str(stage/'remote-install.py'), request['commit']], capture_output=True)
            if result.returncode:
                write(stage/'result.json', {'commit': request['commit'], 'phase': 'failed', 'error_code': 'unit_start_failed'})
                raise ValueError()
        elif action == 'refresh':
            info = stage.lstat()
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o077: raise ValueError()
            current = json.loads((stage/'request.json').read_text())
            if current != request: raise ValueError()
            phase = json.loads((stage/'result.json').read_text())['phase']
            if phase not in ('downloading', 'awaiting_url'): return
            sys.path.insert(0, str(stage))
            from delivery import check_url, read_ticket
            check_url(payload['ticket']['url'])
            previous = read_ticket(stage)
            if payload['ticket']['revision'] > previous['revision']:
                write(stage/'download.json', payload['ticket'])
        else: raise ValueError()


if __name__ == '__main__':
    os.umask(0o077)
    try:
        if os.geteuid() != 0 or sys.argv[1:] not in (['start'], ['refresh']): raise ValueError()
        raw = sys.stdin.buffer.read(512*1024+1)
        if len(raw) > 512*1024: raise ValueError()
        control(json.loads(raw), sys.argv[1])
        print('PASS: deployment control request accepted')
    except Exception:
        raise SystemExit('Deployment control failed; inspect the private stage and unit before retrying') from None
