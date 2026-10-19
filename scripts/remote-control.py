#!/usr/bin/env python3
"""Installed deploy-delivery command. JSON data only; no uploaded executable code."""
import argparse
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))
from delivery import check_url, read_ticket, record, validate
import service

MAX_REQUEST = 24 * 1024
SCRIPTS = Path(__file__).resolve().parent


def control(payload, action):
    if set(payload) != {'protocol', 'request', 'ticket'} or payload['protocol'] != service.PROTOCOL:
        raise ValueError('unsupported control envelope; executable files are forbidden')
    request = validate(payload['request'])
    policy = service.config()
    if (request['repository'], request['arch']) != (policy['repository'], policy['arch']):
        raise ValueError('request differs from server policy')
    ticket = payload['ticket']
    if set(ticket) != {'url', 'revision'} or type(ticket['revision']) is not int or not 1 <= ticket['revision'] <= 4:
        raise ValueError('invalid ticket revision')
    check_url(ticket['url'])
    with service.locked(service.LAUNCH_LOCK):
        stage = service.stage(request['run_id'], request['attempt'], exists=action != 'start')
        if action == 'start':
            if ticket['revision'] != 1: raise ValueError('initial revision must be one')
            # mkdir fails on existing paths, including links: requests never replace a job.
            stage.mkdir(mode=0o700)
            record(stage/'request.json', request)
            record(stage/'download.json', ticket)
            record(stage/'result.json', {'commit': request['commit'], 'phase': 'accepted'})
            unit = 'deploy-platform-ci-' + request['run_id'] + '-' + request['attempt']
            result = subprocess.run(['systemd-run', '--no-block', '--unit='+unit,
                                     '--property=Type=oneshot', '--property=TimeoutStartSec=infinity',
                                     '--property=UMask=0077', '--property=Nice=10', '--property=MemoryHigh=256M',
                                     '/usr/bin/python3', '-I', '-B', str(SCRIPTS/'remote-install.py'),
                                     request['run_id'], request['attempt'], request['commit']], capture_output=True)
            if result.returncode:
                (stage/'download.json').unlink(missing_ok=True)
                record(stage/'result.json', {'commit': request['commit'], 'phase': 'failed', 'error_code': 'unit_start_failed'})
                raise ValueError('worker launch failed')
        elif action == 'refresh':
            current = json.loads(service.trusted(stage/'request.json').read_text())
            if current != request: raise ValueError('request changed')
            phase = json.loads(service.trusted(stage/'result.json').read_text())['phase']
            if phase not in ('downloading', 'awaiting_url'): return
            previous = read_ticket(stage)
            if ticket['revision'] > previous['revision']: record(stage/'download.json', ticket)
        else: raise ValueError('unsupported action')


def preflight(arch):
    policy = service.config()
    host_arch = {'x86_64': 'amd64', 'aarch64': 'arm64'}.get(platform.machine())
    if arch != policy['arch'] or arch != host_arch: raise ValueError('server architecture differs')
    subprocess.run(['/usr/bin/bash', '-c', 'source "$1"; check_supported_os', 'preflight', str(SCRIPTS/'os.sh')], check=True, capture_output=True)
    if not Path('/sys/fs/cgroup/cgroup.controllers').is_file() or not Path('/run/systemd/system').is_dir():
        raise ValueError('systemd and cgroup v2 required')
    subprocess.run(['systemctl', 'show-environment'], check=True, capture_output=True)
    if shutil.disk_usage('/opt').free < 2 * 1024**3: raise ValueError('2 GiB free disk required')
    service.stage('1', '1', exists=False)
    return dict(policy, version=service.VERSION, release=SCRIPTS.parent.name, phase='ready')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='action', required=True)
    sub.add_parser('version')
    pre = sub.add_parser('preflight'); pre.add_argument('arch', choices=['amd64', 'arm64'])
    for name in ('start', 'refresh'): sub.add_parser(name)
    for name in ('status', 'logs'):
        job = sub.add_parser(name); job.add_argument('run_id'); job.add_argument('attempt')
    args = parser.parse_args()
    if args.action == 'version':
        print(json.dumps({'protocol': service.PROTOCOL, 'version': service.VERSION})); return
    if os.geteuid() != 0: raise ValueError('root required')
    os.umask(0o077)
    service.config()
    if args.action == 'preflight': print(json.dumps(preflight(args.arch)))
    elif args.action in ('start', 'refresh'):
        raw = sys.stdin.buffer.read(MAX_REQUEST+1)
        if len(raw) > MAX_REQUEST: raise ValueError('control request too large')
        control(json.loads(raw), args.action)
        print('PASS: installed delivery tool accepted request')
    else:
        stage = service.stage(args.run_id, args.attempt)
        path = service.trusted(stage/('result.json' if args.action == 'status' else 'install.log'))
        if args.action == 'status':
            value = json.loads(path.read_text())
            # Never return stored download authorization or arbitrary JSON fields.
            keys = {'commit', 'phase', 'error_code', 'downloaded_bytes', 'total_bytes', 'url_revision', 'version', 'artifact_id', 'message'}
            print(json.dumps({k: v for k, v in value.items() if k in keys}))
        else:
            from collections import deque
            with path.open() as handle: sys.stdout.write(''.join(deque(handle, maxlen=100)))


if __name__ == '__main__':
    try: main()
    except Exception:
        raise SystemExit('Delivery command failed; check installed tool, server policy and private job status/logs') from None
