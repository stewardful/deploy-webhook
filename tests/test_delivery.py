"""HTTPS, immutable artifact identity, bounded extraction and safe failure tests."""
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import ssl
import stat
import sys
import tempfile
import unittest
from unittest.mock import patch
import urllib.error
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'scripts'))
import delivery
spec = importlib.util.spec_from_file_location('artifact_request', ROOT/'scripts/artifact-request.py')
provider = importlib.util.module_from_spec(spec);spec.loader.exec_module(provider)
SHA='a'*40
URL='https://test.blob.core.windows.net/archive?SIGNED_SECRET'
STEM='deploy-platform-0.5.3-linux-amd64'


def archive_bytes(installer=b'installer', names=None):
    data=io.BytesIO()
    with zipfile.ZipFile(data,'w') as archive:
        if names is None:
            archive.writestr(STEM+'.run',installer)
            archive.writestr(STEM+'.sha256',hashlib.sha256(installer).hexdigest()+'  '+STEM+'.run\n')
        else:
            for name in names:archive.writestr(name,b'content')
    return data.getvalue()


def request(data, installer=b'installer'):
    return dict(schema_version=1,repository='owner/repo',run_id='123',attempt='1',commit=SHA,version='0.5.3',arch='amd64',artifact_id=99,archive_sha256=hashlib.sha256(data).hexdigest(),archive_size=len(data),installer_sha256=hashlib.sha256(installer).hexdigest(),installer_size=len(installer),download_seconds=30)


class Response(io.BytesIO):
    def __init__(self,data,offset=0,total=None,status=206):
        super().__init__(data)
        self.status=status
        self.headers={'Content-Length':str(len(data)), 'Content-Range':f'bytes {offset}-{(total or len(data))-1}/{total or len(data)}'}


class Client:
    def __init__(self,*responses):self.responses=list(responses);self.requests=[]
    def open(self,req,timeout):
        self.requests.append(req)
        value=self.responses.pop(0)
        if isinstance(value,Exception):raise value
        return value


class Clock:
    def __init__(self):self.value=0
    def __call__(self):return self.value
    def sleep(self,seconds):self.value+=seconds


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.stage=Path(self.temp.name)
        (self.stage/'download.json').write_text(json.dumps({'url':URL,'revision':1}))
        self.data=archive_bytes();self.request=request(self.data);self.events=[];self.clock=Clock()
    def run_download(self,client,sleep=None):
        delivery.download(self.stage,self.request,lambda phase,**fields:self.events.append(dict(phase=phase,**fields)),client=client,clock=self.clock,sleep=sleep or self.clock.sleep)
    def test_success_verifies_archive_and_installer_and_keeps_urls_out_of_progress(self):
        client=Client(Response(self.data))
        self.run_download(client)
        self.assertTrue(all(req.get_header('Authorization') is None for req in client.requests))
        self.assertEqual((self.stage/'installer.run').read_bytes(),b'installer')
        self.assertFalse((self.stage/'artifact.zip.part').exists())
        self.assertEqual((self.stage/'installer.run').stat().st_mode & 0o777,0o600)
        self.assertNotIn('SIGNED_SECRET',json.dumps(self.events))
    def test_interruption_resumes_at_actual_bytes_written(self):
        class Broken(Response):
            def read1(inner,n):
                if inner.tell():raise OSError('connection lost')
                return super().read1(37)
        first=Broken(self.data)
        client=Client(first,Response(self.data[37:],offset=37,total=len(self.data)))
        self.run_download(client)
        self.assertEqual(client.requests[1].get_header('Range'),'bytes=37-')
        self.assertEqual((self.stage/'installer.run').read_bytes(),b'installer')
    def test_ignored_range_replaces_partial_file(self):
        class Broken(Response):
            def read1(inner,n):
                if inner.tell():raise OSError()
                return super().read1(37)
        self.run_download(Client(Broken(self.data),Response(self.data,status=200)))
        self.assertEqual((self.stage/'installer.run').read_bytes(),b'installer')
    def test_expired_url_requests_new_ticket_without_github_token(self):
        expired=urllib.error.HTTPError(URL,403,'expired',{},None)
        client=Client(expired,Response(self.data))
        def sleep(seconds):
            self.clock.sleep(seconds)
            (self.stage/'download.json').write_text(json.dumps({'url':URL+'-new','revision':2}))
        self.run_download(client,sleep)
        self.assertTrue(any(e['phase']=='awaiting_url' for e in self.events))
        self.assertTrue(client.requests[1].full_url.endswith('-new'))
    def test_expired_url_without_runner_refresh_fails_within_deadline(self):
        with self.assertRaisesRegex(delivery.DeliveryError,'download_url_refresh_timeout'):
            self.run_download(Client(urllib.error.HTTPError(URL,403,'expired',{},None)))
        self.assertLessEqual(self.clock.value,30)
        self.assertFalse((self.stage/'installer.run').exists())
    def test_timeout_and_retry_exhaustion_never_produce_installer(self):
        with self.assertRaisesRegex(delivery.DeliveryError,'download_retry_exhausted'):
            self.run_download(Client(*[OSError('SECRET_URL') for _ in range(4)]))
        self.assertFalse((self.stage/'installer.run').exists())
    def test_stream_deadline_is_enforced_during_transfer(self):
        clock=self.clock
        class Slow(Response):
            def read1(inner,n):clock.value+=31;return super().read1(1)
        with self.assertRaisesRegex(delivery.DeliveryError,'download_timeout'):
            self.run_download(Client(Slow(self.data)))
    def test_archive_corruption_is_rejected_before_extraction(self):
        corrupt=bytes([self.data[0]^1])+self.data[1:]
        with self.assertRaisesRegex(delivery.DeliveryError,'archive_digest_mismatch'):
            self.run_download(Client(Response(corrupt)))
        self.assertFalse((self.stage/'installer.run').exists())
    def test_wrong_length_and_range_are_rejected(self):
        for headers,code in [({'Content-Length':'999'},'archive_size_mismatch'),({'Content-Range':f'bytes 1-{len(self.data)-1}/{len(self.data)}'},'invalid_content_range')]:
            with self.subTest(code=code):
                response=Response(self.data);response.headers.update(headers)
                with self.assertRaisesRegex(delivery.DeliveryError,code):self.run_download(Client(response))
    def test_url_and_redirect_allowlist_rejects_http_credentials_and_other_hosts(self):
        for value in ['http://test.blob.core.windows.net/a','https://127.0.0.1/a','https://evil.example/a','https://user:pass@test.blob.core.windows.net/a','https://test.blob.core.windows.net:123/a','https://blob.core.windows.net.evil.example/a']:
            with self.subTest(value=value),self.assertRaises(delivery.DeliveryError):delivery.check_url(value)
        with self.assertRaises(delivery.DeliveryError):
            delivery.Redirects().redirect_request(urllib.request.Request(URL),None,302,'',{},'http://evil.example/a')
    def test_tls_failures_are_not_downgraded(self):
        with self.assertRaisesRegex(delivery.DeliveryError,'download_tls_failed'):
            self.run_download(Client(ssl.SSLCertVerificationError('untrusted CA')))
    def test_zip_inventory_links_and_installer_hash(self):
        for names in [['../escape',STEM+'.sha256'],[STEM+'.run',STEM+'.run'],[STEM+'.run',STEM+'.sha256','extra']]:
            with self.subTest(names=names):
                source=self.stage/'test.zip';source.write_bytes(archive_bytes(names=names))
                with self.assertRaises(delivery.DeliveryError):delivery.extract(source,self.stage,self.request)
                self.assertFalse((self.stage.parent/'escape').exists())
        source=self.stage/'test.zip';data=io.BytesIO()
        with zipfile.ZipFile(data,'w') as archive:
            link=zipfile.ZipInfo(STEM+'.run');link.external_attr=(stat.S_IFLNK|0o777)<<16
            archive.writestr(link,b'/etc/passwd');archive.writestr(STEM+'.sha256',b'checksum')
        source.write_bytes(data.getvalue())
        with self.assertRaisesRegex(delivery.DeliveryError,'invalid_artifact_entry'):delivery.extract(source,self.stage,self.request)
        data=archive_bytes(b'corrupted');source.write_bytes(data)
        with self.assertRaises(delivery.DeliveryError):delivery.extract(source,self.stage,self.request)


class ProviderTests(unittest.TestCase):
    def metadata(self):
        return {'id':99,'name':'deploy-platform-linux-amd64','expired':False,'size_in_bytes':300,'digest':'sha256:'+'b'*64,'workflow_run':{'id':123,'head_branch':'dev','head_sha':SHA,'repository_id':1,'head_repository_id':1}}
    def test_only_exact_run_commit_branch_and_artifact_identity_are_accepted(self):
        item=self.metadata()
        with patch.object(provider,'api',return_value={'artifacts':[item]}):
            self.assertEqual(provider.metadata('owner/repo','123',SHA,'amd64')['id'],99)
        for key,value in [('head_branch','main'),('head_sha','c'*40),('id',456),('head_repository_id',2)]:
            item=self.metadata();item['workflow_run'][key]=value
            with self.subTest(key=key),patch.object(provider,'api',return_value={'artifacts':[item]}),self.assertRaises(delivery.DeliveryError):provider.metadata('owner/repo','123',SHA,'amd64')
        item=self.metadata();item['expired']=True
        with patch.object(provider,'api',return_value={'artifacts':[item]}),self.assertRaises(delivery.DeliveryError):provider.metadata('owner/repo','123',SHA,'amd64')
    def test_refresh_cannot_change_artifact_digest_size_or_id(self):
        original=request(archive_bytes())
        for key,value in [('id',100),('size_in_bytes',999),('digest','sha256:'+'c'*64)]:
            item=self.metadata();item[key]=value
            with self.subTest(key=key),patch.object(provider,'metadata',return_value=item),self.assertRaises(delivery.DeliveryError):provider.authorize(original)
    def test_private_artifact_redirect_never_forwards_authorization(self):
        error=urllib.error.HTTPError('https://api.github.com',302,'redirect',{'Location':URL},None)
        with patch.dict(os.environ,GH_TOKEN='GH_SECRET'),patch.object(provider.urllib.request,'build_opener') as factory:
            factory.return_value.open.side_effect=error
            self.assertEqual(provider.signed_url('owner/repo',99),URL)
            req=factory.return_value.open.call_args.args[0]
            self.assertEqual(req.get_header('Authorization'),'Bearer GH_SECRET')

class WorkerBoundaryTests(unittest.TestCase):
    def test_wall_clock_timeout_only_wraps_downloader_and_never_starts_installer(self):
        spec=importlib.util.spec_from_file_location('https_ci_worker',ROOT/'scripts/remote-install.py')
        worker=importlib.util.module_from_spec(spec);spec.loader.exec_module(worker)
        import subprocess
        with tempfile.TemporaryDirectory() as directory:
            stage=Path(directory);value=request(archive_bytes())
            (stage/'request.json').write_text(json.dumps(value))
            (stage/'download.json').write_text(json.dumps({'url':URL,'revision':1}))
            with patch.object(worker.subprocess,'run',side_effect=subprocess.TimeoutExpired('download',30)) as run:
                with self.assertRaisesRegex(delivery.DeliveryError,'download_timeout'):worker.install(stage,SHA)
            self.assertEqual(run.call_count,1)
            self.assertTrue(run.call_args.args[0][1].endswith('delivery.py'))
            self.assertEqual(run.call_args.kwargs['timeout'],30)
            self.assertEqual(json.loads((stage/'result.json').read_text())['error_code'],'download_timeout')
            self.assertFalse((stage/'download.json').exists())
            self.assertNotIn('SIGNED_SECRET',(stage/'install.log').read_text())
