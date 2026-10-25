#!/usr/bin/env python3
"""Actual non-root HTTP process persists a signed inbox; never executes cloudctl."""
import hashlib
import hmac
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
import urllib.error


def main():
    if os.geteuid()==0:raise SystemExit('Run this HTTP smoke test as non-root')
    with tempfile.TemporaryDirectory() as directory:
        root=Path(directory);secret=b'x'*32;path=root/'secret';path.write_bytes(secret);path.chmod(0o600)
        with socket.socket() as sock:sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
        args=[str(Path(sys.argv[1]).resolve()),'--listen',f'127.0.0.1:{port}','--secret-file',str(path),'--state-dir',str(root/'inbox')]
        process=subprocess.Popen(args,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        try:
            base=f'http://127.0.0.1:{port}'
            for _ in range(100):
                try:
                    with urllib.request.urlopen(base+'/healthz',timeout=1) as response:assert response.status==200
                    break
                except OSError:
                    if process.poll() is not None:raise RuntimeError('HTTP receiver exited')
                    time.sleep(.05)
            else:raise RuntimeError('startup timeout')
            body=json.dumps({'action':'published','repository':{'full_name':'stewardful/deploy-platform','id':1},'release':{'id':42,'tag_name':'dev-42','target_commitish':'a'*40,'draft':False,'prerelease':True,'published_at':'2026-10-26T00:00:00Z'}}).encode()
            signature='sha256='+hmac.new(secret,body,hashlib.sha256).hexdigest()
            headers={'Content-Type':'application/json','X-GitHub-Event':'release','X-GitHub-Delivery':'00000000-0000-0000-0000-000000000001','X-Hub-Signature-256':signature}
            with urllib.request.urlopen(urllib.request.Request(base+'/hooks/github/releases',data=body,headers=headers),timeout=2) as response:assert response.status==200
            inbox=root/'inbox/inbox.json';original=inbox.read_bytes()
            headers['X-Hub-Signature-256']='sha256='+'0'*64
            try:urllib.request.urlopen(urllib.request.Request(base+'/hooks/github/releases',data=body,headers=headers));raise AssertionError('forgery accepted')
            except urllib.error.HTTPError as error:assert error.code==401
            assert inbox.read_bytes()==original
            for removed in ('/v1/events','/v1/releases/latest'):
                try:urllib.request.urlopen(base+removed);raise AssertionError('obsolete endpoint retained')
                except urllib.error.HTTPError as error:assert error.code==404
            assert not (root/'ledger').exists()
            print('PASS: non-root receiver persisted valid input, rejected forgery, no CLI execution or obsolete endpoint')
        finally:process.terminate();process.wait(timeout=8)


if __name__=='__main__':main()
