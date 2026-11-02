"""Real loopback HTTP for bounded compressed GET; no models or external network."""
import gzip
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from agenthub_kernel.papers.net import HttpFetcher


@pytest.fixture
def server(monkeypatch):
    monkeypatch.setenv('NO_PROXY','127.0.0.1,localhost')
    raw=json.dumps({'papers':['论文 metadata']*2000},ensure_ascii=False).encode()
    calls=[]
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def do_GET(self):
            calls.append((self.path,self.headers.get('Accept-Encoding')))
            if self.path=='/redirect':
                self.send_response(302);self.send_header('Location','/forbidden');self.end_headers();return
            body=gzip.compress(raw)
            encoding='gzip'
            if self.path=='/bomb':body=gzip.compress(b'x'*100000)
            elif self.path=='/bad':body=b'not gzip'
            elif self.path=='/truncated':body=body[:-4]
            elif self.path=='/unknown':encoding='br'
            elif self.path=='/identity':body=raw;encoding='identity'
            self.send_response(200)
            self.send_header('Content-Type','application/json')
            self.send_header('Content-Encoding',encoding)
            self.send_header('Content-Length',str(len(body)))
            self.end_headers();self.wfile.write(body)
    http=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    thread=threading.Thread(target=http.serve_forever,daemon=True);thread.start()
    try:yield f'http://127.0.0.1:{http.server_port}',raw,calls
    finally:http.shutdown();http.server_close();thread.join(2)


def test_gzip_and_identity_return_identical_bounded_content(server):
    base,raw,calls=server
    fetcher=HttpFetcher(timeout=2)
    gz=fetcher.fetch(base+'/gzip',allowed=lambda u:(True,''),max_bytes=len(raw))
    plain=fetcher.fetch(base+'/identity',allowed=lambda u:(True,''),max_bytes=len(raw))
    assert gz.ok and plain.ok and gz.body==plain.body==raw
    assert gz.encoding=='gzip' and gz.wire_bytes<len(raw)//10
    assert plain.wire_bytes==len(raw)
    assert all(encoding=='gzip' for _,encoding in calls)


@pytest.mark.parametrize('path,error',[('/bomb','too_large'),('/bad','bad_content_encoding'),
    ('/truncated','bad_content_encoding'),('/unknown','unsupported_content_encoding')])
def test_compressed_errors_and_expansion_never_reach_parser(server,path,error):
    base,raw,_=server
    got=HttpFetcher(timeout=2).fetch(base+path,allowed=lambda u:(True,''),max_bytes=512 if path=='/bomb' else len(raw))
    assert not got.ok and got.error==error and not got.body


def test_each_redirect_is_still_checked_before_a_request(server):
    base,raw,calls=server
    got=HttpFetcher(timeout=2).fetch(base+'/redirect',
        allowed=lambda u:(not u.endswith('/forbidden'),'host_not_allowed'),max_bytes=len(raw))
    assert not got.ok and got.error=='host_not_allowed'
    assert [path for path,_ in calls]==['/redirect']
