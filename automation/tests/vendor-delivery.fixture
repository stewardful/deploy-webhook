#!/usr/bin/env python3
"""Explicitly import/check a pinned private delivery source snapshot, without CI tokens."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[2]
DEST = ROOT / 'distribution/vendor/deploy-delivery'
LOCK = DEST.parent / 'deploy-delivery.lock.json'


def check():
    lock = json.loads(LOCK.read_text())
    if lock['repository'] != 'stewardful/deploy-delivery' or not re.fullmatch('[a-f0-9]{40}', lock['commit']):
        raise ValueError('invalid delivery source identity')
    actual = {}
    for path in DEST.rglob('*'):
        if path.is_symlink(): raise ValueError('linked vendor file')
        if path.is_file():
            actual[path.relative_to(DEST).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    if actual != lock['files']: raise ValueError('delivery snapshot differs from its lock; sync from upstream')
    print('PASS: pinned deploy-delivery snapshot ' + lock['commit'])


def sync(source, revision):
    if not re.fullmatch('[a-f0-9]{40}', revision): raise ValueError('full commit SHA required')
    def git(*args):
        return subprocess.check_output(['git', '-C', source, *args])
    entries = git('ls-tree', '-rz', '--full-tree', revision).split(b'\0')
    files = {}
    for entry in entries:
        if not entry: continue
        meta, raw = entry.split(b'\t'); mode, kind, oid = meta.decode().split()
        name = raw.decode()
        if not (name.startswith(('scripts/', 'tests/')) or name == 'README.md'): continue
        if kind != 'blob' or mode not in ('100644', '100755'): raise ValueError('unsupported upstream file')
        files[name] = git('cat-file', 'blob', oid)
    if not files or 'scripts/deploy-test.sh' not in files: raise ValueError('invalid upstream layout')
    if DEST.exists(): shutil.rmtree(DEST)
    for name, data in files.items():
        path = DEST / name; path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(data)
    LOCK.write_text(json.dumps({'schema_version': 1, 'repository': 'stewardful/deploy-delivery', 'commit': revision,
                                'files': {n: hashlib.sha256(d).hexdigest() for n, d in sorted(files.items())}}, indent=2)+'\n')
    check()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='action', required=True)
    sub.add_parser('check')
    update = sub.add_parser('sync'); update.add_argument('--source', required=True); update.add_argument('--revision', required=True)
    args = parser.parse_args()
    if args.action == 'check': check()
    else: sync(args.source, args.revision)
