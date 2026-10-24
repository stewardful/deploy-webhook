#!/usr/bin/env python3
"""Exercise the built service as an unprivileged process; no server or GitHub access."""
import hashlib
import hmac
import http.client
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request


def main():
    if os.geteuid() == 0:
        raise SystemExit('Run this smoke test as a non-root user')
    binary = str(Path(sys.argv[1]).resolve())
    with tempfile.TemporaryDirectory(prefix='release-notify-test-') as directory:
        root = Path(directory)
        secret = b'test-fixture-only-' + b'x'*32
        path = root/'webhook.secret'
        path.write_bytes(secret)
        path.chmod(0o600)
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', 0))
            port = listener.getsockname()[1]
        base = f'http://127.0.0.1:{port}'
        args = [binary, '--listen', f'127.0.0.1:{port}', '--secret-file', str(path), '--state-dir', str(root/'state')]
        process = None

        def request(url, body=None, signature=True):
            headers = {}
            if body is not None:
                headers = {'Content-Type': 'application/json', 'X-GitHub-Event': 'release',
                           'X-GitHub-Delivery': '00000000-0000-4000-8000-000000000001',
                           'X-Hub-Signature-256': 'sha256='+hmac.new(secret if signature else b'wrong', body, hashlib.sha256).hexdigest()}
            with urllib.request.urlopen(urllib.request.Request(base+url, data=body, headers=headers), timeout=3) as response:
                return json.load(response)

        def start(log):
            process = subprocess.Popen(args, stdout=log, stderr=log)
            deadline = time.monotonic()+10
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    raise RuntimeError('service exited before becoming healthy')
                try:
                    assert request('/healthz') == {'status': 'ok'}
                    return process
                except (OSError, AssertionError):
                    time.sleep(0.05)
            process.terminate()
            process.wait(timeout=8)
            raise RuntimeError('service did not become healthy')

        def stop():
            nonlocal process
            if process is not None:
                process.terminate()
                process.wait(timeout=8)
                process = None

        with (root/'service.log').open('wb') as log:
            try:
                process = start(log)
                assert request('/v1/releases/latest') == {'revision': 0, 'latest': {}}
                body = json.dumps({'action': 'published', 'repository': {'id': 1, 'full_name': 'stewardful/deploy-platform'},
                                   'release': {'id': 10, 'tag_name': 'dev-42', 'prerelease': True, 'draft': False,
                                               'published_at': '2026-10-25T00:00:00Z'}}).encode()
                try:
                    request('/hooks/github/releases', body, signature=False)
                    raise AssertionError('invalid signature accepted')
                except urllib.error.HTTPError as error:
                    assert error.code == 401
                    error.close()
                connection = http.client.HTTPConnection('127.0.0.1', port, timeout=3)
                connection.request('GET', '/v1/events')
                stream = connection.getresponse()
                assert stream.status == 200

                def snapshot():
                    value = None
                    while True:
                        line = stream.readline()
                        if line.startswith(b'data: '):
                            value = json.loads(line[6:])
                        if line == b'\n':
                            return value
                        if not line:
                            raise AssertionError('SSE disconnected')
                assert snapshot()['revision'] == 0
                assert request('/hooks/github/releases', body) == {'accepted': True}
                assert snapshot()['latest']['dev']['tag'] == 'dev-42'
                stream.close()
                connection.close()
                stop()
                process = start(log)
                assert request('/v1/releases/latest')['revision'] == 1
                assert request('/hooks/github/releases', body) == {'accepted': False}
                stop()
            finally:
                stop()
        assert secret not in (root/'service.log').read_bytes()
    print('PASS: non-root binary, HTTP signature validation, live SSE, graceful restart and durable replay protection')


if __name__ == '__main__':
    main()
