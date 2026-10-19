#!/usr/bin/env python3
"""Offline installer for the independent delivery tool; never installs a platform payload."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import stat
import subprocess
import tempfile
import fcntl

VERSION = '0.1.0'
SERVER_FILES = ('delivery.py', 'remote-control.py', 'remote-install.py', 'service.py', 'os.sh')
BUNDLE_FILES = tuple('scripts/' + name for name in (*SERVER_FILES, 'install.py')) + ('README.md',)
PREFIX = Path('/opt/deploy-delivery')
CONFIG = Path('/etc/deploy-delivery')
STATE = Path('/var/lib/deploy-delivery')
LAUNCHER = Path('/usr/local/bin/deploy-delivery')
LEGACY_LAUNCHER_TEXT = '#!/bin/sh\nexec /usr/bin/python3 -I -B /opt/deploy-delivery/current/scripts/remote-control.py "$@"\n'
LAUNCHER_TEXT = '#!/bin/sh\nset -eu\nrelease=$(/usr/bin/readlink -f /opt/deploy-delivery/current)\nexec /usr/bin/python3 -I -B "$release/scripts/remote-control.py" "$@"\n'


def inventory(source):
    manifest_path = source/'manifest.json'
    if manifest_path.is_symlink(): raise ValueError('linked manifest')
    manifest = json.loads(manifest_path.read_text())
    if set(manifest) != {'schema_version', 'version', 'files'} or manifest['schema_version'] != 1 or manifest['version'] != VERSION or set(manifest['files']) != set(BUNDLE_FILES):
        raise ValueError('invalid bundle inventory')
    data = {}
    for name in BUNDLE_FILES:
        path = source/name
        # Reject symlinked directories as well as linked files.
        if path.is_symlink() or any(p.is_symlink() for p in path.parents if p != source.parent):
            raise ValueError('linked bundle path')
        if not path.is_file(): raise ValueError('missing bundle file')
        data[name] = path.read_bytes()
        if hashlib.sha256(data[name]).hexdigest() != manifest['files'][name]:
            raise ValueError('bundle checksum mismatch')
    return data


def safe(path, *, directory=False, private=False):
    info = path.lstat()
    if not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)) or info.st_uid != 0 or info.st_mode & (0o077 if private else 0o022):
        raise ValueError('untrusted install path: ' + str(path))


def directory(path, mode):
    # Every ancestor is root-owned and cannot be replaced by an unprivileged user.
    if path != path.parent:
        if path.parent.exists(): safe(path.parent, directory=True)
        else: directory(path.parent, 0o755)
    if not path.exists() and not path.is_symlink():
        path.mkdir(mode=mode); path.chmod(mode)
    safe(path, directory=True, private=mode == 0o700)


def replace(path, data, mode):
    fd, name = tempfile.mkstemp(prefix='.delivery-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as handle:
            handle.write(data); handle.flush(); os.fsync(handle.fileno())
        os.chmod(name, mode)
        os.replace(name, path)
    finally: Path(name).unlink(missing_ok=True)


def activate(link, target):
    temporary = link.with_name('.activate-' + str(os.getpid()))
    try:
        temporary.symlink_to(target)
        os.replace(temporary, link)
    finally: temporary.unlink(missing_ok=True)


def install(source, repository, arch):
    if os.geteuid() != 0: raise ValueError('root required')
    if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repository): raise ValueError('invalid repository')
    if arch not in ('amd64', 'arm64') or {'x86_64': 'amd64', 'aarch64': 'arm64'}.get(platform.machine()) != arch:
        raise ValueError('host architecture differs')
    data = inventory(source)
    os.umask(0o077)
    directory(PREFIX, 0o755)
    directory(PREFIX/'releases', 0o755)
    directory(CONFIG, 0o700)
    directory(STATE, 0o700)
    directory(STATE/'jobs', 0o700)
    directory(LAUNCHER.parent, 0o755)
    policy = {'protocol': 1, 'repository': repository, 'arch': arch}
    policy_path = CONFIG/'config.json'
    # Coordinate activation with request acceptance, keeping all old releases for in-flight jobs.
    fd = os.open('/run/lock/deploy-platform-ci-launch.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as lock:
        safe(Path('/run/lock/deploy-platform-ci-launch.lock'), private=True)
        fcntl.flock(lock, fcntl.LOCK_EX)
        if policy_path.exists() or policy_path.is_symlink():
            safe(policy_path, private=True)
            if json.loads(policy_path.read_text()) != policy: raise ValueError('existing server policy differs; do not silently rebind a server')
        files = {name: data['scripts/'+name] for name in SERVER_FILES}
        hashes = {name: hashlib.sha256(value).hexdigest() for name, value in files.items()}
        identity = hashlib.sha256(json.dumps(hashes, sort_keys=True).encode()).hexdigest()
        release = PREFIX/'releases'/(VERSION+'-'+identity)
        if release.exists() or release.is_symlink():
            safe(release, directory=True)
            safe(release/'scripts', directory=True)
            if {p.name for p in (release/'scripts').iterdir()} != set(files): raise ValueError('existing release inventory differs')
            for name in files:
                path = release/'scripts'/name
                safe(path)
                if path.read_bytes() != files[name]: raise ValueError('immutable release changed')
        else:
            staging = Path(tempfile.mkdtemp(prefix='.install-', dir=PREFIX/'releases'))
            try:
                (staging/'scripts').mkdir(mode=0o755)
                for name, value in files.items():
                    path = staging/'scripts'/name
                    path.write_bytes(value); path.chmod(0o644)
                staging.chmod(0o755)
                staging.rename(release)
            finally:
                if staging.exists(): shutil.rmtree(staging)
        # Validate candidate before touching the active pointer, launcher or configuration.
        result = subprocess.run(['/usr/bin/python3', '-I', '-B', str(release/'scripts/remote-control.py'), 'version'], check=True, capture_output=True, text=True)
        if json.loads(result.stdout) != {'protocol': 1, 'version': VERSION}: raise ValueError('candidate protocol differs')
        current = PREFIX/'current'
        previous = None
        if current.exists() or current.is_symlink():
            if not current.is_symlink(): raise ValueError('current must be a managed symlink')
            previous = current.readlink()
            if current.resolve().parent != (PREFIX/'releases').resolve(): raise ValueError('current escapes release directory')
        if LAUNCHER.exists() or LAUNCHER.is_symlink():
            safe(LAUNCHER)
            if LAUNCHER.read_text() not in (LAUNCHER_TEXT, LEGACY_LAUNCHER_TEXT): raise ValueError('refusing to replace an unrelated command')
        if not policy_path.exists(): replace(policy_path, (json.dumps(policy)+'\n').encode(), 0o600)
        try:
            activate(current, release)
            replace(LAUNCHER, LAUNCHER_TEXT.encode(), 0o755)
            subprocess.run([str(LAUNCHER), 'version'], check=True, capture_output=True)
        except Exception:
            if previous is not None: activate(current, previous)
            else: current.unlink(missing_ok=True)
            raise
        print('PASS: deploy-delivery '+VERSION+' installed; existing jobs and releases retained')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repository', required=True)
    parser.add_argument('--arch', required=True, choices=['amd64', 'arm64'])
    args = parser.parse_args()
    try: install(Path(__file__).resolve().parent.parent, args.repository, args.arch)
    except Exception as error:
        raise SystemExit('Delivery installation failed: '+str(error)) from None
