#!/usr/bin/env python3
"""Non-root receiver → fixed local HTTP task API; no server deployment."""
import hashlib
import hmac
import http.server
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import urllib.error


def main():
    if os.geteuid()==0:raise SystemExit('Run as non-root')
    seen=[]
    class Target(http.server.BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def do_POST(self):
            body=self.rfile.read(int(self.headers['Content-Length']))
            seen.append((self.path,body,self.headers.get('X-Hub-Signature-256')))
            self.send_response(200);self.end_headers()
    target=http.server.ThreadingHTTPServer(('127.0.0.1',0),Target)
    threading.Thread(target=target.serve_forever,daemon=True).start()
    with tempfile.TemporaryDirectory() as directory:
        secret=b'x'*32;path=Path(directory)/'secret';path.write_bytes(secret);path.chmod(0o600)
        with socket.socket() as sock:sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
        args=[str(Path(sys.argv[1]).resolve()),'--listen',f'127.0.0.1:{port}','--secret-file',str(path),'--target',f'http://127.0.0.1:{target.server_port}/hooks/github/releases']
        process=subprocess.Popen(args,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        try:
            base=f'http://127.0.0.1:{port}'
            body=json.dumps({'action':'published','repository':{'full_name':'stewardful/deploy-platform','id':1},'release':{'id':42,'tag_name':'dev-42','draft':False,'prerelease':True,'published_at':'2026-10-25T00:00:00Z'}}).encode()
            signature='sha256='+hmac.new(secret,body,hashlib.sha256).hexdigest()
            headers={'Content-Type':'application/json','X-GitHub-Event':'release','X-GitHub-Delivery':'00000000-0000-0000-0000-000000000001','X-Hub-Signature-256':signature}
            for attempt in range(100):
                try:
                    req=urllib.request.Request(base+'/hooks/github/releases',data=body,headers=headers)
                    with urllib.request.urlopen(req,timeout=2) as response:assert response.status==200
                    break
                except OSError:
                    if process.poll() is not None:raise RuntimeError('receiver exited')
                    time.sleep(.05)
            else:raise RuntimeError('receiver startup timeout')
            assert seen==[('/hooks/github/releases',body,signature)]
            headers['X-Hub-Signature-256']='sha256='+'0'*64
            try:urllib.request.urlopen(urllib.request.Request(base+'/hooks/github/releases',data=body,headers=headers));raise AssertionError('forgery accepted')
            except urllib.error.HTTPError as error:assert error.code==401
            assert len(seen)==1
            for removed in ('/v1/events','/v1/releases/latest'):
                try:urllib.request.urlopen(base+removed);raise AssertionError('obsolete endpoint retained')
                except urllib.error.HTTPError as error:assert error.code==404
            print('PASS: signed original body forwarded, forged payload rejected, SSE endpoints absent')
        finally:
            process.terminate();process.wait(timeout=8);target.shutdown();target.server_close()


if __name__=='__main__':main()
