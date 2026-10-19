"""Independent server protocol and offline bundle boundary tests."""
from contextlib import nullcontext
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'scripts'))
import service
import install as installer
import package as package_builder
from delivery import DeliveryError

spec = importlib.util.spec_from_file_location('server_control', ROOT/'scripts/remote-control.py')
control = importlib.util.module_from_spec(spec); spec.loader.exec_module(control)


def request():
    return {'schema_version': 1, 'repository': 'owner/repo', 'run_id': '123', 'attempt': '1',
            'commit': 'a'*40, 'version': '0.5.3', 'arch': 'amd64', 'artifact_id': 1,
            'archive_sha256': 'b'*64, 'archive_size': 20, 'installer_sha256': 'c'*64,
            'installer_size': 10, 'download_seconds': 30}


def envelope():
    return {'protocol': 1, 'request': request(), 'ticket': {'url': 'https://test.blob.core.windows.net/file?SECRET', 'revision': 1}}


class ControlTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.stage = Path(self.temp.name)/'job'
        patches = [patch.object(service, 'config', return_value={'protocol': 1, 'repository': 'owner/repo', 'arch': 'amd64'}),
                   patch.object(service, 'stage', return_value=self.stage),
                   patch.object(service, 'locked', return_value=nullcontext()),
                   patch.object(service, 'trusted', side_effect=lambda p, **kw: p)]
        for item in patches: item.start(); self.addCleanup(item.stop)

    def test_start_uses_installed_code_and_writes_no_executable_source(self):
        with patch.object(control.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0)) as run:
            control.control(envelope(), 'start')
        self.assertEqual({p.name for p in self.stage.iterdir()}, {'request.json', 'download.json', 'result.json'})
        command = run.call_args.args[0]
        self.assertIn(str(control.SCRIPTS/'remote-install.py'), command)
        self.assertEqual(command[-3:], ['123', '1', 'a'*40])
        self.assertIn('-I', command)
        self.assertIn('--no-block', command)
        self.assertEqual((self.stage/'download.json').stat().st_mode & 0o077, 0)
        with self.assertRaises(FileExistsError): control.control(envelope(), 'start')

    def test_code_protocol_repository_arch_url_and_revision_rejected_before_start(self):
        values = []
        for change in ({'files': {'remote-install.py': 'evil'}}, {'protocol': 2}, {'ticket': {'url': 'http://bad.invalid/x', 'revision': 1}}, {'ticket': {'url': envelope()['ticket']['url'], 'revision': 0}}):
            value = envelope(); value.update(change); values.append(value)
        for key, value in [('repository', 'other/repo'), ('arch', 'arm64')]:
            item = envelope(); item['request'][key] = value; values.append(item)
        with patch.object(control.subprocess, 'run') as run:
            for value in values:
                with self.subTest(value=value), self.assertRaises((ValueError, DeliveryError)):
                    control.control(value, 'start')
            run.assert_not_called()
        self.assertFalse(self.stage.exists())

    def test_launch_failure_removes_download_authorization(self):
        with patch.object(control.subprocess, 'run', return_value=subprocess.CompletedProcess([], 1)):
            with self.assertRaises(ValueError): control.control(envelope(), 'start')
        self.assertFalse((self.stage/'download.json').exists())
        self.assertEqual(json.loads((self.stage/'result.json').read_text())['error_code'], 'unit_start_failed')

    def test_refresh_requires_unchanged_request_and_increasing_ticket(self):
        with patch.object(control.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0)):
            control.control(envelope(), 'start')
        (self.stage/'result.json').write_text('{"phase":"awaiting_url"}')
        next_ticket = envelope(); next_ticket['ticket']['revision'] = 2
        control.control(next_ticket, 'refresh')
        control.control(envelope(), 'refresh')
        self.assertEqual(json.loads((self.stage/'download.json').read_text())['revision'], 2)
        next_ticket['request']['commit'] = 'd'*40
        with self.assertRaises(ValueError): control.control(next_ticket, 'refresh')

    def test_status_filters_unknown_credential_fields(self):
        self.stage.mkdir()
        (self.stage/'result.json').write_text(json.dumps({'phase': 'downloading', 'url': 'SECRET', 'ticket': 'SECRET', 'downloaded_bytes': 1}))
        with patch.object(sys, 'argv', ['deploy-delivery', 'status', '123', '1']), patch.object(control.os, 'geteuid', return_value=0), patch.object(sys, 'stdout', new_callable=io.StringIO) as stdout:
            control.main()
            self.assertEqual(json.loads(stdout.getvalue()), {'phase': 'downloading', 'downloaded_bytes': 1})


class BundleTests(unittest.TestCase):
    def test_offline_bundle_is_reproducible_and_installer_rejects_tampering(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            first = package_builder.build(ROOT, root/'first')
            second = package_builder.build(ROOT, root/'second')
            self.assertEqual(first.read_bytes(), second.read_bytes())
            self.assertEqual(first.with_suffix('.gz.sha256').read_text().split()[0], hashlib.sha256(first.read_bytes()).hexdigest())
            with tarfile.open(first) as bundle:
                self.assertTrue(all(p.name.startswith('deploy-delivery-0.1.0/') and p.isfile() for p in bundle))
                bundle.extractall(root/'unpacked')
            source = root/'unpacked/deploy-delivery-0.1.0'
            self.assertEqual(set(installer.inventory(source)), set(installer.BUNDLE_FILES))
            (source/'scripts/delivery.py').write_text('corrupt')
            with self.assertRaisesRegex(ValueError, 'checksum'): installer.inventory(source)

    def test_installer_refuses_linked_code_and_extra_manifest_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            archive = package_builder.build(ROOT, root/'build')
            with tarfile.open(archive) as bundle: bundle.extractall(root)
            source = root/'deploy-delivery-0.1.0'
            path = source/'scripts/service.py'
            data = path.read_bytes(); path.unlink(); path.symlink_to('delivery.py')
            with self.assertRaisesRegex(ValueError, 'linked'): installer.inventory(source)
            path.unlink(); path.write_bytes(data)
            manifest = source/'manifest.json'; value = json.loads(manifest.read_text()); value['files']['../escape'] = 'a'*64
            manifest.write_text(json.dumps(value))
            with self.assertRaisesRegex(ValueError, 'inventory'): installer.inventory(source)

    def test_invalid_stage_identifiers_cannot_escape_storage(self):
        for run, attempt in [('../escape', '1'), ('1', 'x'), ('1', '1/../../tmp'), ('-1', '1')]:
            with self.assertRaises(ValueError): service.stage(run, attempt)

    def test_version_is_available_without_root_and_ignores_pythonpath(self):
        import os
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory)/'service.py').write_text('raise RuntimeError("injected")')
            env = dict(os.environ, PYTHONPATH=directory)
            result = subprocess.run([sys.executable, '-I', '-B', str(ROOT/'scripts/remote-control.py'), 'version'], env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout), {'protocol': 1, 'version': '0.1.0'})


class WorkerBoundaryTests(unittest.TestCase):
    def test_terminal_cleanup_is_serialized_with_refresh(self):
        spec = importlib.util.spec_from_file_location('installed_worker', ROOT/'scripts/remote-install.py')
        worker = importlib.util.module_from_spec(spec); spec.loader.exec_module(worker)
        for busy in (False, True):
            with self.subTest(busy=busy), tempfile.TemporaryDirectory() as directory:
                stage = Path(directory)
                (stage/'request.json').write_text(json.dumps(request()))
                (stage/'download.json').write_text(json.dumps(envelope()['ticket']))
                (stage/'result.json').write_text('{"phase":"accepted"}')
                locks = []
                def locked(path, **kwargs):
                    locks.append(path)
                    if busy and path == service.WORKER_LOCK: raise BlockingIOError()
                    return nullcontext()
                def installed(*args):
                    (stage/'result.json').write_text('{"phase":"succeeded"}')
                    # Represents a last credential refresh before final cleanup.
                    (stage/'download.json').write_text('last refreshed ticket')
                with patch.object(sys, 'argv', ['worker', '123', '1', 'a'*40]), patch.object(worker.os, 'geteuid', return_value=0), patch.object(service, 'stage', return_value=stage), patch.object(service, 'trusted', side_effect=lambda p, **kw: p), patch.object(service, 'config', return_value={'repository': 'owner/repo', 'arch': 'amd64'}), patch.object(service, 'locked', side_effect=locked), patch.object(worker, 'install', side_effect=installed):
                    if busy:
                        with self.assertRaises(BlockingIOError): worker.main()
                    else: worker.main()
                self.assertFalse((stage/'download.json').exists())
                self.assertEqual(locks[-1], service.LAUNCH_LOCK)
                self.assertEqual(json.loads((stage/'result.json').read_text())['phase'], 'failed' if busy else 'succeeded')

    def test_server_path_policy_rejects_links_permissions_and_nonroot_owner(self):
        from types import SimpleNamespace
        from unittest.mock import Mock
        import stat
        path = Mock()
        for mode, uid in [(stat.S_IFLNK | 0o777, 0), (stat.S_IFREG | 0o644, 0), (stat.S_IFREG | 0o600, 1000), (stat.S_IFREG | 0o620, 0)]:
            path.lstat.return_value = SimpleNamespace(st_mode=mode, st_uid=uid)
            with self.assertRaises(ValueError): service.trusted(path)
        path.lstat.return_value = SimpleNamespace(st_mode=stat.S_IFREG | 0o600, st_uid=0)
        self.assertIs(service.trusted(path), path)

    def test_real_authorization_envelope_contains_only_data(self):
        import os
        spec = importlib.util.spec_from_file_location('request_provider', ROOT/'scripts/artifact-request.py')
        provider = importlib.util.module_from_spec(spec); spec.loader.exec_module(provider)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'request.json'; path.write_text(json.dumps(request()))
            with patch.object(sys, 'argv', ['provider', 'authorize', str(path)]), patch.object(provider, 'authorize', return_value=envelope()['ticket']['url']), patch.dict(os.environ, URL_REVISION='1'), patch.object(sys, 'stdout', new_callable=io.StringIO) as output:
                provider.main()
                value = json.loads(output.getvalue())
            self.assertEqual(set(value), {'protocol', 'request', 'ticket'})
            self.assertEqual(value, envelope())
