#!/usr/bin/env python3
"""CI-only artifact identity pinning and short-lived URL issuance. Never sends a GitHub token."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys

sys.dont_write_bytecode = True
import urllib.error
import urllib.request

from delivery import DeliveryError, check_url, validate


def api(path):
    result = subprocess.run(['gh', 'api', path], capture_output=True, text=True)
    if result.returncode: raise DeliveryError('github_metadata_failed')
    return json.loads(result.stdout)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs): return None


def signed_url(repository, artifact_id):
    token = os.environ.get('GH_TOKEN', '')
    if not token: raise DeliveryError('github_token_missing')
    request = urllib.request.Request(f'https://api.github.com/repos/{repository}/actions/artifacts/{artifact_id}/zip', headers={'Authorization': 'Bearer '+token, 'Accept': 'application/vnd.github+json'})
    try:
        urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect()).open(request, timeout=20)
    except urllib.error.HTTPError as error:
        if error.code == 302: return check_url(error.headers.get('Location', ''))
    except (OSError, ValueError): pass
    raise DeliveryError('github_download_authorization_failed')


def metadata(repository, run_id, commit, arch, *, branch='dev'):
    if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repository): raise DeliveryError('invalid_repository')
    if not re.fullmatch(r'[0-9]{1,20}', run_id): raise DeliveryError('invalid_run')
    items = api(f'repos/{repository}/actions/runs/{run_id}/artifacts?per_page=100')['artifacts']
    matches = [a for a in items if a['name'] == 'deploy-platform-linux-'+arch]
    if len(matches) != 1: raise DeliveryError('artifact_missing_or_ambiguous')
    value = matches[0]
    run = value.get('workflow_run', {})
    if value.get('expired') is not False or str(run.get('id')) != run_id or run.get('head_branch') != branch or run.get('head_sha') != commit or type(run.get('repository_id')) is not int or run.get('repository_id') != run.get('head_repository_id'):
        raise DeliveryError('artifact_source_mismatch')
    digest = value.get('digest', '')
    if not re.fullmatch(r'sha256:[a-f0-9]{64}', digest): raise DeliveryError('artifact_digest_missing')
    return value


def prepare(directory, *, commit=None, branch='dev'):
    repository = os.environ['GITHUB_REPOSITORY']
    run_id = os.environ['GITHUB_RUN_ID']
    commit = commit or os.environ['GITHUB_SHA']
    arch = os.environ.get('TEST_SERVER_ARCH', 'amd64')
    version = os.environ['RELEASE_VERSION']
    if arch not in ('amd64', 'arm64') or not re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+', version): raise DeliveryError('invalid_release')
    stem = f'deploy-platform-{version}-linux-{arch}'
    installer = Path(directory)/(stem+'.run')
    checksum = Path(directory)/(stem+'.sha256')
    if installer.is_symlink() or not installer.is_file() or checksum.is_symlink(): raise DeliveryError('installer_missing_or_linked')
    digest = hashlib.sha256()
    with installer.open('rb') as source:
        for chunk in iter(lambda: source.read(256*1024), b''): digest.update(chunk)
    matches = [line for line in checksum.read_text().splitlines() if re.fullmatch(r'[a-f0-9]{64}  '+re.escape(installer.name), line)]
    if matches != [digest.hexdigest()+'  '+installer.name]: raise DeliveryError('installer_checksum_mismatch')
    item = metadata(repository, run_id, commit, arch, branch=branch)
    result = {'schema_version': 1, 'repository': repository, 'run_id': run_id, 'attempt': os.environ['GITHUB_RUN_ATTEMPT'], 'commit': commit, 'version': version, 'arch': arch, 'artifact_id': item['id'], 'archive_sha256': item['digest'][7:], 'archive_size': item['size_in_bytes'], 'installer_size': installer.stat().st_size, 'installer_sha256': digest.hexdigest(), 'download_seconds': int(os.environ.get('DOWNLOAD_SECONDS', '900'))}
    validate(result)
    return result


def authorize(request):
    item = metadata(request['repository'], request['run_id'], request['commit'], request['arch'])
    if (item['id'], item['digest'][7:], item['size_in_bytes']) != (request['artifact_id'], request['archive_sha256'], request['archive_size']):
        raise DeliveryError('artifact_identity_changed')
    return signed_url(request['repository'], request['artifact_id'])


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['prepare', 'authorize'])
    parser.add_argument('path')
    args = parser.parse_args()
    if args.action == 'prepare':
        request = prepare(os.environ['CI_ARTIFACT_DIR'])
        Path(args.path).write_text(json.dumps(request))
        Path(args.path).chmod(0o600)
    else:
        request = validate(json.loads(Path(args.path).read_text()))
        result = {'request': request, 'ticket': {'url': authorize(request), 'revision': int(os.environ.get('URL_REVISION', '1'))}, 'files': {}}
        for name in ('remote-install.py', 'delivery.py'):
            result['files'][name] = (Path(__file__).parent/name).read_text()
        # Consumed only by SSH stdin, never console output or command arguments.
        json.dump(result, sys.stdout)


if __name__ == '__main__':
    try: main()
    except Exception:
        raise SystemExit('Artifact metadata/authorization failed; verify Actions read permission and pinned artifact identity') from None
