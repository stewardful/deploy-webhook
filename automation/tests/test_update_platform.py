import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / 'update-platform.py'
spec = importlib.util.spec_from_file_location('update_platform', SCRIPT)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class UpdateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source, self.target = self.root / 'source', self.root / 'target'
        for directory in (self.source, self.target):
            directory.mkdir()
            self.git(directory, 'init', '-b', 'main')
            self.git(directory, 'config', 'user.name', 'Test')
            self.git(directory, 'config', 'user.email', 'test@example.invalid')
        (self.source / 'scripts').mkdir()
        (self.source / 'scripts/deploy-test.sh').write_text('#!/bin/bash\n')
        (self.source / 'tests').mkdir()
        (self.source / 'tests/test_demo.py').write_text('import unittest\nclass Test(unittest.TestCase):\n def test_ok(self): self.assertTrue(True)\n')
        self.git(self.source, 'add', '.')
        self.git(self.source, 'commit', '-m', 'initial')
        self.old = self.git(self.source, 'rev-parse', 'HEAD')
        self.git(self.target, 'checkout', '-b', 'dev')
        sync = self.target / 'distribution/ci/vendor-delivery.py'
        sync.parent.mkdir(parents=True)
        # Exercise the actual consumer protocol; supplied by the test invocation.
        fixture = Path(__file__).with_name('vendor-delivery.fixture')
        shutil.copyfile(fixture, sync)
        subprocess.run(['python3', '-B', str(sync), 'sync', '--source', str(self.source), '--revision', self.old], check=True, capture_output=True)
        self.git(self.target, 'add', '.')
        self.git(self.target, 'commit', '-m', 'consumer')
        self.remote = self.root / 'remote.git'
        subprocess.run(['git', 'init', '--bare', str(self.remote)], check=True, capture_output=True)
        self.git(self.target, 'remote', 'add', 'origin', str(self.remote))
        self.git(self.target, 'push', 'origin', 'dev')
        (self.source / 'scripts/deploy-test.sh').write_text('#!/bin/bash\n# updated\n')
        self.git(self.source, 'add', '.')
        self.git(self.source, 'commit', '-m', 'update')
        self.new = self.git(self.source, 'rev-parse', 'HEAD')
        self.prs = []
        self.calls = []
        self.original_run = module.run
        self.patcher = patch.object(module, 'run', self.transport)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)

    def git(self, cwd, *args):
        return subprocess.run(['git', *args], cwd=cwd, check=True, text=True, capture_output=True).stdout.strip()

    def transport(self, args, cwd, check=True):
        self.calls.append(args)
        if args[:3] == ['git', 'remote', 'get-url']:
            return subprocess.CompletedProcess(args, 0, 'https://github.com/' + module.REPOSITORY + '\n', '')
        if args[:3] == ['gh', 'pr', 'list']:
            return subprocess.CompletedProcess(args, 0, json.dumps(self.prs), '')
        if args[:3] == ['gh', 'pr', 'create']:
            body = Path(args[args.index('--body-file') + 1]).read_text()
            self.assertIn(self.new, body)
            self.assertEqual(args[args.index('--base') + 1], 'dev')
            self.prs.append({'url': 'https://example.invalid/pr/1', 'state': 'OPEN', 'baseRefName': 'dev'})
            return subprocess.CompletedProcess(args, 0, self.prs[0]['url'], '')
        return self.original_run(args, cwd, check)

    def test_update_and_retry_are_idempotent(self):
        module.update(self.source, self.target, self.new)
        branch = 'codex/deploy-delivery-' + self.new
        paths = self.git(self.target, 'diff', '--name-only', 'origin/dev', 'HEAD').splitlines()
        self.assertTrue(module.allowed(paths))
        self.assertEqual(json.loads((self.target / module.LOCK).read_text())['commit'], self.new)
        self.git(self.target, 'checkout', '--detach', 'origin/dev')
        self.git(self.target, 'branch', '-D', branch)
        module.update(self.source, self.target, self.new)
        self.assertEqual(len(self.prs), 1)
        self.assertFalse(any('--force' in arg for call in self.calls for arg in call))
        self.assertFalse(any('merge' in call or 'workflow' in call for call in self.calls))

    def test_already_pinned_does_not_push(self):
        module.update(self.source, self.target, self.new)
        self.git(self.target, 'push', 'origin', 'HEAD:dev')
        self.calls.clear()
        module.update(self.source, self.target, self.new)
        self.assertFalse(any(call[:2] == ['git', 'push'] for call in self.calls))

    def test_wrong_sha_and_dirty_checkout_rejected(self):
        for sha in ('main', self.old):
            with self.assertRaises(ValueError): module.update(self.source, self.target, sha)
        (self.target / 'dirty').touch()
        with self.assertRaises(ValueError): module.update(self.source, self.target, self.new)

    def test_missing_protocol_waits_without_push(self):
        self.git(self.target, 'rm', 'distribution/ci/vendor-delivery.py')
        self.git(self.target, 'commit', '-m', 'legacy')
        self.git(self.target, 'push', 'origin', 'HEAD:dev')
        module.update(self.source, self.target, self.new)
        self.assertEqual(self.prs, [])

    def test_unrelated_revision_cannot_downgrade(self):
        self.git(self.source, 'checkout', '--orphan', 'other')
        self.git(self.source, 'commit', '-m', 'unrelated')
        revision = self.git(self.source, 'rev-parse', 'HEAD')
        with self.assertRaisesRegex(ValueError, 'descend'): module.update(self.source, self.target, revision)

    def test_existing_remote_branch_is_not_overwritten(self):
        self.git(self.target, 'push', 'origin', 'HEAD:refs/heads/codex/deploy-delivery-' + self.new)
        with self.assertRaisesRegex(ValueError, 'refusing to overwrite'):
            module.update(self.source, self.target, self.new)
        self.assertEqual(self.prs, [])

    def test_failed_component_tests_prevent_push_and_pr(self):
        (self.source / 'tests/test_demo.py').write_text('import unittest\nclass Test(unittest.TestCase):\n def test_failure(self): self.fail("broken")\n')
        self.git(self.source, 'add', '.')
        self.git(self.source, 'commit', '-m', 'broken')
        revision = self.git(self.source, 'rev-parse', 'HEAD')
        with self.assertRaises(subprocess.CalledProcessError):
            module.update(self.source, self.target, revision)
        self.assertFalse(any(call[:2] == ['git', 'push'] for call in self.calls))
        self.assertEqual(self.prs, [])

    def test_dirty_source_is_rejected(self):
        (self.source / 'untracked').touch()
        with self.assertRaisesRegex(ValueError, 'source checkout must be clean'):
            module.update(self.source, self.target, self.new)

    def test_wrong_remote_is_rejected(self):
        with patch.object(module, 'run', self.original_run):
            with self.assertRaisesRegex(ValueError, 'unexpected target remote'):
                module.update(self.source, self.target, self.new)

    def test_path_scope(self):
        self.assertFalse(module.allowed([]))
        self.assertFalse(module.allowed(['.github/workflows/dev-deploy.yml']))
        self.assertFalse(module.allowed([module.LOCK, 'README.md']))
        self.assertTrue(module.allowed([module.LOCK, module.VENDOR + '/scripts/x.py']))


class WorkflowTests(unittest.TestCase):
    def test_privileged_job_requires_successful_tests_and_main_push(self):
        workflow = (SCRIPT.parents[1] / '.github/workflows/check.yml').read_text()
        self.assertIn('needs: test', workflow)
        self.assertIn("github.ref == 'refs/heads/main'", workflow)
        self.assertIn("github.event_name != 'pull_request'", workflow)
        self.assertNotIn('pull_request_target', workflow)
        self.assertIn('cancel-in-progress: false', workflow)
        self.assertIn('TESTED_SHA: ${{ github.sha }}', workflow)
        self.assertIn('current=$(gh api repos/stewardful/deploy-delivery/git/ref/heads/main', workflow)

    def test_scoped_token_and_no_server_credentials(self):
        workflow = (SCRIPT.parents[1] / '.github/workflows/check.yml').read_text()
        self.assertIn('repositories: deploy-platform', workflow)
        self.assertIn('permission-contents: write', workflow)
        self.assertIn('permission-pull-requests: write', workflow)
        self.assertNotIn('TEST_SSH', workflow)
        self.assertNotIn('deploy-test.sh', workflow)
        self.assertIn('persist-credentials: false', workflow)
        self.assertIn('::warning::Component PR automation is not enabled', workflow)


if __name__ == '__main__': unittest.main()
