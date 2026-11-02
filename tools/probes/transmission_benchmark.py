"""Compare two explicit, disposable loopback dev servers; no Agent calls.

Only creates fixture conversations/messages and reads their SSE streams. A
desktop/production server is refused. Caller must explicitly opt in to writes.
"""
import argparse
import json
from pathlib import Path
import queue
import statistics
import threading
import time
import urllib.request
from urllib.parse import urlsplit


def measure(origin, repeats):
    u=urlsplit(origin)
    if u.scheme!='http' or u.hostname!='127.0.0.1' or not u.port or u.path or u.query or u.username:
        raise ValueError('explicit bare loopback origin required')
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
    def request(path,body=None,token=None):
        headers={'Content-Type':'application/json'}
        if token:headers['Authorization']='Bearer '+token
        req=urllib.request.Request(origin+path,data=json.dumps(body).encode() if body is not None else None,headers=headers)
        return opener.open(req,timeout=5)
    with request('/api/client-config') as r:config=json.load(r)
    if config.get('desktop') or not config.get('development_token'):raise ValueError('disposable dev server required')
    token=config['development_token']
    samples=[]
    for _ in range(repeats):
        with request('/api/conversations',{'title':'SSE transport fixture'},token) as r:cid=json.load(r)['conversation_id']
        ready=threading.Event();result=queue.Queue()
        def read():
            try:
                with request(f'/api/conversations/{cid}/events/stream?after_seq=1&timeout=2',token=token) as r:
                    for raw in r:
                        if raw.startswith(b'retry:'):ready.set()
                        if raw.startswith(b'data:'):
                            result.put(time.perf_counter());return
            except Exception as exc:result.put(exc)
        thread=threading.Thread(target=read);thread.start()
        if not ready.wait(3):raise TimeoutError('SSE did not start')
        time.sleep(.05)
        start=time.perf_counter()
        with request(f'/api/conversations/{cid}/messages',{'text':'fixed synthetic transport fixture'},token) as r:
            assert r.status==200
        received=result.get(timeout=3);thread.join(3)
        if isinstance(received,Exception):raise received
        samples.append(round((received-start)*1000,3))
    return {'samples_ms':samples,'median_ms':round(statistics.median(samples),3),
            'scope':'local message POST start to new SSE event, no model','real_model_calls':0}


def main():
    p=argparse.ArgumentParser();p.add_argument('--before',required=True);p.add_argument('--after',required=True)
    p.add_argument('--output',required=True);p.add_argument('--repeats',type=int,default=5)
    p.add_argument('--approve-fixture-writes',action='store_true');args=p.parse_args()
    if not args.approve_fixture_writes or not 1<=args.repeats<=10:p.error('explicit fixture opt-in and 1..10 samples required')
    report={'before':measure(args.before,args.repeats),'after':measure(args.after,args.repeats)}
    Path(args.output).write_text(json.dumps(report,indent=2),encoding='utf8');print(json.dumps(report))


if __name__=='__main__':main()
