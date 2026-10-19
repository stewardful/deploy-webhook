#!/usr/bin/env python3
"""Root systemd entry point for one CI delivery, independent of SSH and controld."""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import stat

from delivery import DeliveryError, record, validate


def verify(stage):
    installer = stage / 'installer.run'
    checksum = stage / 'SHA256SUMS'
    for path in (installer, checksum):
        if path.is_symlink() or not path.is_file():
            raise ValueError('CI inputs must be regular files')
    text = checksum.read_text()
    if not re.fullmatch(r'[a-f0-9]{64}  installer\.run\n', text):
        raise ValueError('invalid checksum record')
    with installer.open('rb') as handle:
        digest = hashlib.sha256()
        for chunk in iter(lambda: handle.read(256 * 1024), b''): digest.update(chunk)
        actual = digest.hexdigest()
    if actual != text[:64]:
        raise ValueError('uploaded installer checksum mismatch')


def install(stage, commit):
    if not re.fullmatch('[a-f0-9]{40}', commit):
        raise ValueError('invalid deployment commit')
    result = stage / 'result.json'
    status = {'commit': commit, 'phase': 'checking'}
    def report(phase, **fields):
        status.update(fields, phase=phase)
        record(result, status)
    report('checking')
    try:
        with (stage / 'install.log').open('w') as log:
            try:
                if (stage/'request.json').exists():
                    request = validate(json.loads((stage/'request.json').read_text()))
                    if request['commit'] != commit: raise DeliveryError('commit_mismatch')
                    status.update(version=request['version'], artifact_id=request['artifact_id'])
                    # Enforce a wall-clock limit even during DNS/TLS/redirect stalls.
                    # Kill only the download child; never put a timeout around an active installer.
                    try:
                        subprocess.run(['/usr/bin/python3', str(stage/'delivery.py'), str(stage)], check=True, timeout=request['download_seconds'], stdout=log, stderr=subprocess.STDOUT)
                    except subprocess.TimeoutExpired:
                        raise DeliveryError('download_timeout') from None
                    except subprocess.CalledProcessError:
                        child = json.loads(result.read_text())
                        status.update(child)
                        raise DeliveryError(child.get('error_code', 'download_failed')) from None
                    status.update(json.loads(result.read_text()))
                verify(stage)
                report('installing')
                # Download credentials are no longer needed once the archive has been verified.
                (stage/'download.json').unlink(missing_ok=True)
                command = ['/usr/bin/bash', str(stage / 'installer.run')]
                subprocess.run(command + ['--check'], check=True, stdout=log, stderr=subprocess.STDOUT)
                args = ['--upgrade']
                if Path('/etc/deploy-platform/install.json').exists(): args.append('--skip-apt')
                subprocess.run(command + args, check=True, stdout=log, stderr=subprocess.STDOUT)
            except Exception as error:
                code = error.code if isinstance(error, DeliveryError) else 'installer_or_validation_failed'
                log.write('Deployment failed: '+code+'\n')
                status['error_code'] = code
                raise
        status.update(phase='succeeded', message='Installer committed and existing application probes passed')
        (stage / 'installer.run').unlink()
    except Exception:
        status.update(phase='failed', message='Inspect install.log and installer transaction before retrying')
        raise
    finally:
        (stage/'download.json').unlink(missing_ok=True)
        record(result, status)


if __name__ == '__main__':
    os.umask(0o077)
    if os.geteuid() != 0 or len(sys.argv) != 2:
        raise SystemExit('root and event commit required')
    try:
        # Serialize complete deliveries even if an earlier Actions runner disconnected.
        descriptor = os.open('/run/lock/deploy-platform-ci.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, 'w') as lock:
            info = os.fstat(lock.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022: raise ValueError('invalid lock')
            try: fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                stage = Path(__file__).resolve().parent
                (stage/'download.json').unlink(missing_ok=True)
                record(stage/'result.json', {'commit': sys.argv[1], 'phase': 'failed', 'error_code': 'deployment_busy'})
                raise
            if not Path(__file__).with_name('request.json').is_file(): raise ValueError('download request required')
            install(Path(__file__).resolve().parent, sys.argv[1])
    except Exception:
        stage = Path(__file__).resolve().parent
        try:
            state = json.loads((stage/'result.json').read_text())
            if state.get('phase') != 'failed':
                record(stage/'result.json', {'commit': sys.argv[1], 'phase': 'failed', 'error_code': 'worker_setup_failed'})
        except (OSError, ValueError, IndexError): pass
        (stage/'download.json').unlink(missing_ok=True)
        raise SystemExit('CI deployment failed; inspect the private deployment log') from None
