#!/usr/bin/env python3
"""Open an immutable, SHA-pinned component PR; never merge or deploy."""
import argparse
import json
from pathlib import Path
import re
import subprocess
import tempfile

REPOSITORY = 'stewardful/deploy-platform'
BASE = 'dev'
VENDOR = 'distribution/vendor/deploy-delivery'
LOCK = VENDOR + '.lock.json'


def run(args, cwd, check=True):
    return subprocess.run(args, cwd=cwd, check=check, text=True, capture_output=True)


def allowed(paths):
    return bool(paths) and all(p == LOCK or p.startswith(VENDOR + '/') for p in paths)


def update(source, target, revision):
    source, target = Path(source).resolve(), Path(target).resolve()
    if not re.fullmatch('[a-f0-9]{40}', revision):
        raise ValueError('full source commit SHA required')
    if run(['git', 'rev-parse', 'HEAD'], source).stdout.strip() != revision:
        raise ValueError('source checkout differs from tested commit')
    if run(['git', 'status', '--porcelain'], source).stdout:
        raise ValueError('source checkout must be clean')
    if run(['git', 'status', '--porcelain'], target).stdout:
        raise ValueError('target checkout must be clean')
    # Remote identity is fixed by the workflow; refuse arbitrary push destinations.
    if run(['git', 'remote', 'get-url', 'origin'], target).stdout.strip() not in ('https://github.com/' + REPOSITORY, 'https://github.com/' + REPOSITORY + '.git'):
        raise ValueError('unexpected target remote')
    run(['git', 'fetch', 'origin', BASE], target)
    run(['git', 'checkout', '--detach', 'FETCH_HEAD'], target)
    synchronizer = target / 'distribution/ci/vendor-delivery.py'
    if not synchronizer.is_file():
        print('NOT READY: dev lacks the vendor protocol; merge platform PR #4 first, then rerun.')
        return
    pinned = json.loads((target / LOCK).read_text())['commit']
    if pinned == revision:
        print('UNCHANGED: dev already pins this commit')
        return
    # Do not downgrade dev when an older workflow is retried.
    ancestry = run(['git', 'merge-base', '--is-ancestor', pinned, revision], source, check=False)
    if ancestry.returncode != 0:
        raise ValueError('source update must descend from the current platform pin')
    branch = 'codex/deploy-delivery-' + revision
    run(['git', 'checkout', '-b', branch], target)
    run(['python3', '-B', str(synchronizer), 'sync', '--source', str(source), '--revision', revision], target)
    paths = run(['git', 'diff', '--name-only'], target).stdout.splitlines()
    if not allowed(paths):
        raise ValueError('component update changed paths outside its snapshot and lock')
    run(['python3', '-B', str(synchronizer), 'check'], target)
    run(['python3', '-B', '-m', 'unittest', 'discover', '-s', VENDOR + '/tests', '-v'], target)
    run(['git', 'add', '--', VENDOR, LOCK], target)
    run(['git', '-c', 'user.name=deploy-delivery[bot]', '-c',
         'user.email=deploy-delivery[bot]@users.noreply.github.com', 'commit', '-m',
         'chore: update deploy-delivery to ' + revision[:12]], target)
    remote = run(['git', 'ls-remote', '--heads', 'origin', branch], target).stdout.strip()
    if remote:
        # Retry without force-pushing or replacing any existing remote content.
        run(['git', 'fetch', 'origin', branch], target)
        if run(['git', 'diff', 'HEAD', 'FETCH_HEAD', '--'], target).stdout:
            raise ValueError('existing update branch differs; refusing to overwrite it')
    else:
        run(['git', 'push', 'origin', 'HEAD:refs/heads/' + branch], target)
    existing = json.loads(run(['gh', 'pr', 'list', '--repo', REPOSITORY, '--head', branch,
                               '--state', 'all', '--json', 'url,state,baseRefName'], target).stdout)
    if existing:
        print('EXISTING: ' + existing[0]['url'])
        return
    body = ('Update the reviewed delivery snapshot and file digests to stewardful/deploy-delivery@' + revision +
            '.\n\nSource CI passed for this exact commit. Snapshot validation and component tests passed; '
            'platform CI must validate installation and upgrade compatibility.\n\n'
            'Only the vendored snapshot and lock are changed. No auto-merge or deployment is requested. '
            'Merging into dev later invokes the normal dev deployment workflow.\n')
    with tempfile.TemporaryDirectory() as directory:
        body_file = Path(directory) / 'body.md'
        body_file.write_text(body)
        result = run(['gh', 'pr', 'create', '--repo', REPOSITORY, '--base', BASE, '--head', branch,
                      '--title', 'chore: update deploy-delivery to ' + revision[:12],
                      '--body-file', str(body_file)], target)
        print(result.stdout.strip())


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', required=True)
    parser.add_argument('--target', required=True)
    parser.add_argument('--revision', required=True)
    args = parser.parse_args()
    update(args.source, args.target, args.revision)
