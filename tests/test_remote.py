"""CI delivery policy, pinned releases and root wrapper failure reporting."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'scripts'))
spec = importlib.util.spec_from_file_location('ci_remote',ROOT/'scripts/remote-install.py')
remote = importlib.util.module_from_spec(spec)
spec.loader.exec_module(remote)
SHA = 'a'*40


class RemoteWrapperTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.stage=Path(self.temp.name)
        (self.stage/'installer.run').write_bytes(b'installer bytes')
        digest=hashlib.sha256(b'installer bytes').hexdigest()
        (self.stage/'SHA256SUMS').write_text(digest+'  installer.run\n')

    def test_success_uses_existing_installer_and_records_commit(self):
        with patch.object(remote.subprocess,'run') as run:
            remote.install(self.stage,SHA)
        self.assertIn('--check',run.call_args_list[0].args[0])
        self.assertIn('--upgrade',run.call_args_list[1].args[0])
        result=json.loads((self.stage/'result.json').read_text())
        self.assertEqual((result['phase'],result['commit']),('succeeded',SHA))
        self.assertFalse((self.stage/'installer.run').exists())
        self.assertEqual((self.stage/'result.json').stat().st_mode&0o077,0)

    def test_failed_upgrade_records_failure_and_keeps_recovery_package(self):
        with patch.object(remote.subprocess,'run',side_effect=subprocess.CalledProcessError(1,['installer'])):
            with self.assertRaises(subprocess.CalledProcessError):remote.install(self.stage,SHA)
        self.assertEqual(json.loads((self.stage/'result.json').read_text())['phase'],'failed')
        self.assertTrue((self.stage/'installer.run').exists())

    def test_corrupt_upload_never_executes_and_cannot_escape_checksum_path(self):
        for text in ['0'*64+'  installer.run\n','0'*64+'  ../../other\n']:
            (self.stage/'SHA256SUMS').write_text(text)
            with patch.object(remote.subprocess,'run') as run:
                with self.assertRaises(ValueError):remote.install(self.stage,SHA)
                run.assert_not_called()

    def test_linked_installer_is_rejected(self):
        (self.stage/'installer.run').unlink()
        (self.stage/'installer.run').symlink_to(self.stage/'SHA256SUMS')
        with self.assertRaises(ValueError):remote.verify(self.stage)


class SSHTransportTests(unittest.TestCase):
    def test_invalid_host_is_rejected_before_any_ssh_or_key_execution(self):
        env=dict(os.environ,GITHUB_REPOSITORY='owner/repo',TEST_SSH_PRIVATE_KEY='test-only',TEST_SSH_KNOWN_HOSTS='test-only',TEST_SERVER_HOST='example.com;touch /tmp/unsafe',TEST_SERVER_USER='ubuntu',GITHUB_RUN_ID='123',GITHUB_RUN_ATTEMPT='1',GITHUB_SHA=SHA,RELEASE_VERSION='0.5.2')
        result=subprocess.run(['bash',str(ROOT/'scripts/deploy-test.sh')],env=env,capture_output=True,text=True)
        self.assertNotEqual(result.returncode,0)
        self.assertIn('invalid TEST_SERVER_HOST',result.stderr)
        self.assertNotIn('test-only',result.stdout+result.stderr)

    def test_transport_uses_ssh_only_for_controls_and_private_stdin(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);bindir=root/'bin';bindir.mkdir()
            trace=root/'trace.jsonl';start=root/'start.json'
            stub='''#!/usr/bin/env python3
import json,os,sys
assert 'TEST_SSH_PRIVATE_KEY' not in os.environ
if os.path.basename(sys.argv[0])=='ssh':assert 'GH_TOKEN' not in os.environ
with open(os.environ['CI_TEST_TRACE'],'a') as f:f.write(json.dumps([os.path.basename(sys.argv[0]),*sys.argv[1:]])+'\\n')
if os.path.basename(sys.argv[0])=='scp':raise SystemExit('SCP must never be used')
if os.path.basename(sys.argv[0])=='ssh':
    if 'status' in sys.argv:print('{"phase":"succeeded"}')
    elif 'preflight' in sys.argv:
        open(os.environ['CI_TEST_TRACE']+'.preflight','w').write(' '.join(sys.argv))
        if os.environ.get('CI_TEST_REJECT_HOST')=='1':sys.exit(42)
        print(json.dumps({'protocol':1,'phase':'ready','arch':'amd64','repository':'owner/repo'}))
    else:
        payload=json.load(sys.stdin)
        assert payload['ticket']['url'].endswith('URL_SECRET_SENTINEL')
        open(os.environ['CI_TEST_START'],'w').write(json.dumps(payload))
'''
            for name in ('ssh','scp','ssh-keygen'):
                (bindir/name).write_text(stub);(bindir/name).chmod(0o755)
            real_python=sys.executable
            wrapper='#!'+real_python+'''
import json,os,sys
if len(sys.argv)>1 and sys.argv[1].endswith('artifact-request.py'):
    if sys.argv[2]=='prepare':open(sys.argv[3],'w').write('{"commit":"a"}')
    else:print(json.dumps({'request':{'commit':'a'},'ticket':{'url':'https://test.blob.core.windows.net/file?URL_SECRET_SENTINEL'},'protocol':1}))
else:os.execv('''+repr(real_python)+''', ['''+repr(real_python)+''', *sys.argv[1:]])
'''
            (bindir/'python3').write_text(wrapper);(bindir/'python3').chmod(0o755)
            env=dict(os.environ,PATH=str(bindir)+os.pathsep+os.environ['PATH'],CI_TEST_TRACE=str(trace),CI_TEST_START=str(start),GITHUB_REPOSITORY='owner/repo',CI_ARTIFACT_DIR=str(root),TEST_SSH_PRIVATE_KEY='test-only',TEST_SSH_KNOWN_HOSTS='test-only',GH_TOKEN='test-only-gh',TEST_SERVER_HOST='127.0.0.1',TEST_SERVER_USER='ubuntu',TEST_SERVER_ARCH='amd64',GITHUB_RUN_ID='123',GITHUB_RUN_ATTEMPT='1',GITHUB_SHA=SHA,RELEASE_VERSION='0.5.3')
            result=subprocess.run(['bash',str(ROOT/'scripts/deploy-test.sh')],env=env,capture_output=True,text=True)
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertEqual(json.loads(start.read_text())['request']['commit'],'a')
            commands=[json.loads(line) for line in trace.read_text().splitlines()]
            for args in commands:
                self.assertNotEqual(args[0],'scp')
                if args[0]=='ssh':
                    self.assertIn('StrictHostKeyChecking=yes',args)
                    self.assertIn('IdentitiesOnly=yes',args)
            self.assertNotIn('URL_SECRET_SENTINEL',trace.read_text()+result.stdout+result.stderr)
            self.assertNotIn('test-only',trace.read_text()+result.stdout+result.stderr)
            preflight=Path(str(trace)+'.preflight').read_text()
            self.assertIn('/usr/local/bin/deploy-delivery preflight amd64',preflight)
            self.assertNotIn('python3 -c', trace.read_text())
            self.assertNotIn('bash -s', trace.read_text())
            for command in commands:
                if command[0] == 'ssh': self.assertIn('/usr/local/bin/deploy-delivery', command)
            self.assertEqual(set(json.loads(start.read_text())), {'protocol', 'request', 'ticket'})
            trace.unlink();start.unlink()
            rejected=subprocess.run(['bash',str(ROOT/'scripts/deploy-test.sh')],env=dict(env,CI_TEST_REJECT_HOST='1'),capture_output=True,text=True)
            self.assertEqual(rejected.returncode,1,rejected.stderr)
            self.assertIn('Install/configure deploy-delivery', rejected.stderr)
            self.assertFalse(start.exists())


class SourceSnapshotTests(unittest.TestCase):
    def test_entrypoints_do_not_modify_readonly_source_inventory(self):
        import shutil
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            shutil.copytree(ROOT/'scripts', root/'scripts')
            env = dict(os.environ); env.pop('PYTHONDONTWRITEBYTECODE', None)
            result = subprocess.run([sys.executable, str(root/'scripts/artifact-request.py'), '--help'], env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            # No arguments: exits before opening any privileged host file.
            result = subprocess.run([sys.executable, str(root/'scripts/remote-install.py')], env=env, capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(list(root.rglob('*.pyc')))
