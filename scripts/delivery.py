"""Bounded HTTPS artifact download and closed ZIP extraction, without GitHub credentials."""
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import socket
import ssl
import stat
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile

MAX_BYTES = 512 * 1024**2
CHUNK = 256 * 1024


class DeliveryError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)



def record(path, value):
    descriptor, temporary = tempfile.mkstemp(prefix='.result-', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'w') as handle:
            json.dump(value, handle); handle.flush(); os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try: os.fsync(directory)
        finally: os.close(directory)
    finally: Path(temporary).unlink(missing_ok=True)

def check_url(url):
    try:
        value = urllib.parse.urlsplit(url)
        host = value.hostname or ''
        allowed = any(host.endswith(suffix) for suffix in ('.blob.core.windows.net', '.actions.githubusercontent.com', '.githubusercontent.com'))
        if not (allowed and value.scheme == 'https' and value.port in (None, 443) and not value.username and not value.password and not value.fragment):
            raise ValueError()
        if len(url) > 16384 or any(ord(c) <= 32 for c in url): raise ValueError()
    except (ValueError, TypeError):
        raise DeliveryError('invalid_download_url') from None
    return url


def validate(request):
    if request.get('schema_version') != 1: raise DeliveryError('invalid_request')
    for key, pattern in [('commit', r'[a-f0-9]{40}'), ('run_id', r'[0-9]{1,20}'), ('attempt', r'[0-9]{1,6}'), ('version', r'[0-9]+\.[0-9]+\.[0-9]+'), ('installer_sha256', r'[a-f0-9]{64}'), ('archive_sha256', r'[a-f0-9]{64}')]:
        if not isinstance(request.get(key), str) or not re.fullmatch(pattern, request[key]): raise DeliveryError('invalid_request')
    if request.get('arch') not in ('amd64', 'arm64'): raise DeliveryError('invalid_request')
    for key in ('installer_size', 'archive_size', 'artifact_id'):
        if type(request.get(key)) is not int or not 0 < request[key] <= (MAX_BYTES if key != 'artifact_id' else 2**63-1): raise DeliveryError('invalid_request')
    if type(request.get('download_seconds')) is not int or not 30 <= request['download_seconds'] <= 1800: raise DeliveryError('invalid_request')
    if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', request.get('repository', '')): raise DeliveryError('invalid_request')
    return request


def digest(path):
    value = hashlib.sha256()
    with path.open('rb') as source:
        for chunk in iter(lambda: source.read(CHUNK), b''): value.update(chunk)
    return value.hexdigest()


class Redirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        check_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def opener():
    # No inherited proxy configuration, credentials, TLS override or HTTP fallback.
    return urllib.request.build_opener(urllib.request.ProxyHandler({}), Redirects(), urllib.request.HTTPSHandler(context=ssl.create_default_context()))


def read_ticket(stage):
    try:
        path = stage / 'download.json'
        if path.is_symlink(): raise ValueError()
        value = json.loads(path.read_text())
        if type(value['revision']) is not int or value['revision'] < 1: raise ValueError()
        check_url(value['url'])
        return value
    except (OSError, KeyError, ValueError, TypeError):
        raise DeliveryError('download_ticket_missing') from None


def download(stage, request, report, *, clock=time.monotonic, sleep=time.sleep, client=None):
    validate(request)
    end = clock() + request['download_seconds']
    client = client or opener()
    path = stage / 'artifact.zip.part'
    if path.exists() or path.is_symlink(): raise DeliveryError('unexpected_download_file')
    ticket = read_ticket(stage)
    failures = 0
    refreshes = 0
    last_report = 0
    while True:
        if clock() >= end: raise DeliveryError('download_timeout')
        offset = path.stat().st_size if path.exists() else 0
        if offset == request['archive_size']: break
        report('downloading', downloaded_bytes=offset, total_bytes=request['archive_size'])
        headers = {'Range': f'bytes={offset}-', 'Accept-Encoding': 'identity'}
        try:
            with client.open(urllib.request.Request(ticket['url'], headers=headers), timeout=min(15, max(0.1, end-clock()))) as response:
                if response.status == 206:
                    content_range = response.headers.get('Content-Range', '')
                    match = re.fullmatch(r'bytes (\d+)-(\d+)/(\d+)', content_range)
                    if not match or int(match[1]) != offset or int(match[3]) != request['archive_size'] or int(match[2]) != request['archive_size']-1:
                        raise DeliveryError('invalid_content_range')
                elif response.status == 200:
                    offset = 0  # A server ignoring Range must replace, never append.
                else: raise DeliveryError('unexpected_download_status')
                length = response.headers.get('Content-Length')
                if length is not None and (not length.isdecimal() or int(length) != request['archive_size']-offset):
                    raise DeliveryError('archive_size_mismatch')
                with path.open('ab' if offset else 'wb') as target:
                    os.chmod(path, 0o600)
                    while True:
                        if clock() >= end: raise DeliveryError('download_timeout')
                        chunk = response.read1(min(CHUNK, request['archive_size']-offset+1))
                        if not chunk: break
                        offset += len(chunk)
                        if offset > request['archive_size']: raise DeliveryError('archive_size_mismatch')
                        target.write(chunk)
                        if clock()-last_report >= 5:
                            target.flush()
                            report('downloading', downloaded_bytes=offset, total_bytes=request['archive_size'])
                            last_report = clock()
                if offset != request['archive_size']: raise OSError('short download')
            break
        except urllib.error.HTTPError as error:
            if error.code in (401, 403, 410):
                refreshes += 1
                if refreshes > 3: raise DeliveryError('download_authorization_failed') from None
                report('awaiting_url', downloaded_bytes=offset, total_bytes=request['archive_size'], url_revision=ticket['revision'])
                refresh_end = min(end, clock()+90)
                while clock() < refresh_end:
                    candidate = read_ticket(stage)
                    if candidate['revision'] > ticket['revision']:
                        ticket = candidate
                        break
                    sleep(min(1, max(0, refresh_end-clock())))
                else: raise DeliveryError('download_url_refresh_timeout') from None
                continue
            if error.code not in (408, 429, 500, 502, 503, 504): raise DeliveryError('download_http_error') from None
            failures += 1
        except ssl.SSLCertVerificationError:
            raise DeliveryError('download_tls_failed') from None
        except urllib.error.URLError as error:
            if isinstance(error.reason, ssl.SSLCertVerificationError): raise DeliveryError('download_tls_failed') from None
            failures += 1
        except (OSError, http.client.HTTPException, socket.timeout):
            failures += 1
        if failures >= 4: raise DeliveryError('download_retry_exhausted')
        sleep(min(2**failures, max(0, end-clock())))
    if digest(path) != request['archive_sha256']: raise DeliveryError('archive_digest_mismatch')
    report('checking', downloaded_bytes=request['archive_size'], total_bytes=request['archive_size'])
    extract(path, stage, request)
    path.unlink()


def extract(source, stage, request):
    name = f"deploy-platform-{request['version']}-linux-{request['arch']}"
    expected = {name+'.run', name+'.sha256'}
    try:
        with zipfile.ZipFile(source) as archive:
            members = archive.infolist()
            if len(members) != 2 or {m.filename for m in members} != expected:
                raise DeliveryError('invalid_artifact_inventory')
            for member in members:
                mode = member.external_attr >> 16
                if member.is_dir() or stat.S_ISLNK(mode) or stat.S_IFMT(mode) not in (0, stat.S_IFREG) or member.flag_bits & 1:
                    raise DeliveryError('invalid_artifact_entry')
            installer = archive.getinfo(name+'.run')
            checksum = archive.getinfo(name+'.sha256')
            if installer.file_size != request['installer_size'] or checksum.file_size > 16384:
                raise DeliveryError('installer_size_mismatch')
            text = archive.read(checksum).decode('ascii')
            matches = [line for line in text.splitlines() if re.fullmatch(r'[a-f0-9]{64}  '+re.escape(name+'.run'), line)]
            if matches != [request['installer_sha256']+'  '+name+'.run']:
                raise DeliveryError('installer_checksum_mismatch')
            target = stage / 'installer.run'
            with archive.open(installer) as data, target.open('xb') as output:
                os.chmod(target, 0o600)
                for chunk in iter(lambda: data.read(CHUNK), b''): output.write(chunk)
            if digest(target) != request['installer_sha256']: raise DeliveryError('installer_digest_mismatch')
            (stage/'SHA256SUMS').write_text(request['installer_sha256']+'  installer.run\n')
            os.chmod(stage/'SHA256SUMS', 0o600)
    except (zipfile.BadZipFile, UnicodeError, RuntimeError, OSError):
        raise DeliveryError('invalid_artifact_archive') from None


if __name__ == '__main__':
    os.umask(0o077)
    try:
        if len(sys.argv) != 2: raise DeliveryError('invalid_download_invocation')
        stage = Path(sys.argv[1])
        request = validate(json.loads((stage/'request.json').read_text()))
        state = {'commit': request['commit'], 'version': request['version'], 'artifact_id': request['artifact_id']}
        def report(phase, **fields):
            state.update(fields, phase=phase)
            record(stage/'result.json', state)
        try: download(stage, request, report)
        except Exception as error:
            report('failed', error_code=error.code if isinstance(error, DeliveryError) else 'download_failed')
            raise
    except Exception:
        raise SystemExit('HTTPS download failed; inspect the deployment result code') from None
