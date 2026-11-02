"""Explicit opt-in online acceptance; no model/network runs on import.

Uses isolated state, existing configured endpoint and credential *path* only.
Never copies or prints secrets. Raw public sources and model research outputs
stay in the ignored run directory; Git receives only metadata and hashes.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import socket
import sys
import time
import uuid
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[2]
for sub in ('packages/contracts','packages/adapters','apps/worker','apps/api'):
    sys.path.insert(0,str(ROOT/sub))


def sha(data):
    return hashlib.sha256(data).hexdigest()


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value,ensure_ascii=False,indent=2),encoding='utf-8')


def configured_profile():
    from agenthub_kernel import desktop
    # Use the already adopted desktop profile, never a repository credential.
    desktop_root=Path(os.environ.get('LOCALAPPDATA',''))/'AgentHub'/'desktop'
    os.environ['AGENT_HUB_DESKTOP_HOME']=str(desktop_root)
    return desktop.load(),desktop_root


def inspect(profile, desktop_root):
    proxy=profile.get('proxy') or ''
    reachable=None
    if proxy:
        u=urlsplit(proxy)
        try:
            with socket.create_connection((u.hostname,u.port or 80),timeout=2):reachable=True
        except OSError:reachable=False
    return {'profile_exists':(desktop_root/'profile.json').is_file(),
            'agents':list(profile.get('agents',{})),
            'deepseek_model':profile.get('agents',{}).get('deepseek',{}).get('model'),
            'deepseek_credentials_present':(desktop_root/'credentials/deepseek.json').is_file(),
            'proxy_configured':bool(proxy),'proxy_reachable':reachable,
            'ca_bundle_present':bool(profile.get('ca_bundle')) and Path(profile['ca_bundle']).is_file(),
            'real_model_calls':0}


def network(profile, mode):
    proxy=(profile.get('proxy') or '') if mode=='configured' else ''
    for key in ('HTTP_PROXY','HTTPS_PROXY','http_proxy','https_proxy','ALL_PROXY','all_proxy'):
        if proxy:os.environ[key]=proxy
        else:os.environ.pop(key,None)
    os.environ['NO_PROXY']='localhost,127.0.0.1'
    if profile.get('ca_bundle'):
        if not Path(profile['ca_bundle']).is_file():raise ValueError('configured_ca_missing')
        os.environ['SSL_CERT_FILE']=profile['ca_bundle']
    return proxy


def source_check(run, profile, mode):
    from agenthub.papers import SOURCES, SOURCE_HOSTS, ResourceRules, ACTION_SEARCH, resource_decision, check_url
    from agenthub_kernel.papers.net import HttpFetcher
    from agenthub_kernel.papers import sources
    network(profile,mode)
    fetcher=HttpFetcher(timeout=18)
    rules=ResourceRules(sources=SOURCES,send_to=('deepseek',))
    results=[]
    for source in SOURCES:
        assert resource_decision(rules,ACTION_SEARCH,source)[0]
        url=sources.query_url(source,['retrieval augmented generation evaluation'],year_from=2020,year_to=2026,max_results=5)
        start=time.monotonic()
        got=fetcher.fetch(url,max_bytes=5*1024*1024,accept='*/*',
                          allowed=lambda u:check_url(u,hosts=SOURCE_HOSTS[source],resolve=fetcher.resolve))
        row={'source':source,'network_mode':mode,'ok':got.ok,'http_status':got.status,'error':got.error,
             'elapsed_seconds':round(time.monotonic()-start,2),'bytes':len(got.body),'sha256':sha(got.body)}
        if got.ok:
            (run/(source+'.raw')).write_bytes(got.body)
            try:
                papers=sources.parse_results(source,got.body)
                row.update(parsed=True,count=len(papers),abstracts=sum(bool(p['abstract']) for p in papers),
                           pdf_links=sum(bool(p['pdf_url']) for p in papers))
                write(run/(source+'-metadata.json'),papers)
            except Exception as exc:row.update(parsed=False,error='parse_'+type(exc).__name__)
        results.append(row)
        write(run/'sources.json',{'results':results,'real_model_calls':0})
        print(json.dumps(row),flush=True)
        time.sleep(3)
    return {'results':results,'real_model_calls':0}


def research_check(run, profile, desktop_root, mode, resume=False):
    """One proof call, then one bounded stage; real adapters and real fetcher."""
    import io
    import zipfile
    from fastapi.testclient import TestClient
    from sqlalchemy import select, func
    from agenthub_adapters.deepseek import DeepSeekAdapter
    from agenthub_adapters.routing import RoutingAdapter
    from agenthub_adapters.store import ExecutionStore
    from agenthub_kernel.papers.net import HttpFetcher
    from agenthub_kernel.papers.parse import quote_in
    from agenthub_kernel.papers.db import PaperChunkRow, PaperDocumentRow, EvidenceRow, ResearchTaskRow
    from agenthub_kernel import config
    from agenthub_kernel.db import Database, InvocationRow, ApprovalRow, ChatMessageRow, UsageLedgerRow

    if (run/'hub.db').exists() != resume:
        raise ValueError('fresh database required, or explicit --resume for the unlaunched budget refusal')
    source_report=json.loads((run/'sources.json').read_text(encoding='utf-8'))
    if not any(r.get('count') for r in source_report['results']):raise ValueError('no live source passed')
    item=profile.get('agents',{}).get('deepseek',{})
    if not item.get('enabled'):raise ValueError('configured_deepseek_not_enabled')
    proxy=network(profile,mode)
    os.environ.pop('AGENT_HUB_DESKTOP',None)
    os.environ['AGENT_HUB_DATA_DIR']=str(run)
    os.environ['AGENT_HUB_USER_TOKEN']=uuid.uuid4().hex
    config.DATA_DIR=str(run)
    headers={'Authorization':'Bearer '+os.environ['AGENT_HUB_USER_TOKEN']}
    adapter=DeepSeekAdapter(item['base_url'],item['model'],
        credentials_path=str(desktop_root/'credentials/deepseek.json'),
        store=ExecutionStore(str(run/'deepseek-executions.db'),table='deepseek_executions'),
        proxy=proxy,ca_bundle=profile.get('ca_bundle') or '',timeout_seconds=120,
        repo_root=str(ROOT),stub='')
    assert not adapter.stub and adapter._transport is None
    router=RoutingAdapter({'deepseek':adapter})
    db=Database('sqlite:///'+(run/'hub.db').as_posix())
    fetcher=HttpFetcher(timeout=20)
    assert not fetcher.simulated
    from agenthub_rt.api import create_app

    def new_app():
        return create_app(db=db,adapter=router,files_root=None,paper_fetcher=fetcher,paper_root=str(run/'papers'))

    def calls():
        with db.session() as s:return s.scalar(select(func.count()).select_from(InvocationRow))

    def api(client,method,path,**kwargs):
        response=client.request(method,path,headers=headers,**kwargs)
        if response.status_code>=400:
            # Route errors are deliberately reduced to status/path in stdout.
            write(run/'last-api-error.json',{'status':response.status_code,'path':path,'detail':response.json()})
            raise RuntimeError(f'HTTP {response.status_code} {path}')
        return response

    started=time.monotonic()
    report={'network_mode':mode,'model':item['model'],'endpoint':item['base_url'],
            'simulated':False,'limits':{'stage_calls':16,'stage_tokens':50000,'per_call_reservation':15000,'proof_calls':1},
            'same_model_reviewer':True,'private_file_access':False}
    app=new_app()
    with TestClient(app) as client:
        if resume:
            previous=json.loads((run/'research-result.json').read_text(encoding='utf-8'))
            old_view=api(client,'GET',f"/api/papers/{previous['task_id']}").json()
            assert old_view['status']=='blocked' and old_view['phase']=='plan'
            assert 'budget_needs_per_call' in old_view['stage']['summary']['message']
            with db.session() as s:
                assert s.get(InvocationRow,previous['proof']['id']).status=='succeeded'
                assert not s.scalar(select(InvocationRow.id).where(InvocationRow.id!=previous['proof']['id'],InvocationRow.started_at.is_not(None)))
            write(run/'pre-fix-task-view.json',old_view)
            report.update(proof=previous['proof'],previous_task_id=previous['task_id'],proof_reused=True)
        else:
            cid=api(client,'POST','/api/conversations',json={'title':'0.8.0 在线验收'}).json()['conversation_id']
            turn=api(client,'POST',f'/api/conversations/{cid}/invocations',json={
                'text':'Reply with exactly READY. No tools, files, or subagents.',
                'agent_id':'deepseek','model':item['model'],'client_request_id':'online-proof','execution_mode':'strict'}).json()
            assert calls()==1
            api(client,'POST',f"/api/approvals/{turn['approval_id']}/approve")
            api(client,'POST',f"/api/invocations/{turn['invocation_id']}/run",params={'wait':'true'})
            proof_deadline=time.monotonic()+180
            while not app.state.worker.wait_idle(1):
                if time.monotonic()>proof_deadline:raise TimeoutError('proof deadline; never auto replay')
            with db.session() as s:
                proof=s.get(InvocationRow,turn['invocation_id'])
                reply=s.scalar(select(ChatMessageRow).where(ChatMessageRow.invocation_id==proof.id,ChatMessageRow.role=='assistant'))
                report['proof']={'id':proof.id,'status':proof.status,'reply_exact':bool(reply and reply.text.strip()=='READY')}
        write(run/'research-result.json',report)
        print(json.dumps({'proof':report['proof']}),flush=True)
        if report['proof']['status']!='succeeded':return report
        body={'client_request_id':'online-project-with-reservation','request':{
            'topic':'Retrieval augmented generation evaluation: faithfulness, answer relevance and context precision',
            'year_from':2020,'year_to':2026,'target_papers':2,'language':'zh','fulltext_policy':'continue_allowed',
            'sources':['arxiv','openalex','crossref','semantic_scholar'],'candidates_per_query':5,
            'sections':2,'mode':'project','core_papers':1,'repair_rounds':1},
            'roles':{'main_agent':'deepseek','review_agent':'deepseek','models':{'deepseek':{'model':item['model']}}},
            'resources':{'allow_pdf_download':True,'allow_public_fulltext':True,'allow_upload':True,
                         'allow_url_supplement':True,'local_roots':[],'max_download_mb':20},
            'execution_mode':'strict','max_calls':16,'token_budget_total':50000,'token_budget_per_call':15000}
        preflight=api(client,'POST','/api/papers/preflight',json=body).json()
        write(run/'preflight.json',preflight)
        if not preflight['ok']:raise ValueError('paper preflight blocked; see local report')
        view=api(client,'POST','/api/papers',json=body).json()
        task=view['task_id']; report['task_id']=task
        write(run/'research-result.json',report)
        deadline=time.monotonic()+20*60
        paused=False;last_signature=None
        while True:
            view=api(client,'GET',f'/api/papers/{task}').json()
            signature=(view['status'],view['phase'],view['usage']['calls'],[(s['key'] if 'key' in s else s['name'],s['status']) for s in view['steps']])
            if signature!=last_signature:
                print(json.dumps({'task':task,'status':view['status'],'phase':view['phase'],
                                  'calls':view['usage']['calls'],'quality':view['quality_status']}),flush=True)
                write(run/'task-view.json',view);last_signature=signature
            if view['status']=='waiting_input':
                write(run/'supplement-observed.json',view['supplement'])
                api(client,'POST',f'/api/papers/{task}/supplement/choice',
                    json={'request_id':view['supplement']['request_id'],'choice':'continue'})
                report['supplement_continue_observed']=True
            elif view['status']!='running':break
            if not paused and view['phase']=='screen':
                api(client,'POST',f'/api/papers/{task}/stage/pause')
                before=calls();app.state.autopilot.tick();app.state.worker.wait_idle(1)
                assert calls()==before
                assert api(client,'GET',f'/api/papers/{task}').json()['status']=='paused'
                api(client,'POST',f'/api/papers/{task}/stage/resume')
                report['pause_no_new_calls']=True;paused=True
            if time.monotonic()>deadline:
                api(client,'POST',f'/api/papers/{task}/stage/pause')
                report['deadline_reached']=True;break
            app.state.autopilot.tick()
            app.state.worker.wait_idle(1)
            time.sleep(.2)
        view=api(client,'GET',f'/api/papers/{task}').json()
        write(run/'task-view.json',view)
        report.update(status=view['status'],phase=view['phase'],quality_status=view['quality_status'],
                      usage=view['usage'],search_sources=view['outcome'].get('search_sources',[]),
                      papers=[{k:p.get(k) for k in ('paper_id','title','source','source_id','doi','included','fulltext_status','fulltext_reason')} for p in view['papers']])
        with db.session() as s:
            invs=list(s.scalars(select(InvocationRow)))
            approvals=list(s.scalars(select(ApprovalRow)))
            usage=list(s.scalars(select(UsageLedgerRow)))
            chunks={c.id:c.text for c in s.scalars(select(PaperChunkRow).join(PaperDocumentRow,
                PaperChunkRow.document_id==PaperDocumentRow.id).where(PaperDocumentRow.task_id==task))}
            evidence=list(s.scalars(select(EvidenceRow).where(EvidenceRow.task_id==task)))
            studies=view['outcome'].get('studies',{})
            refs=[r for study in studies.values() for unit in study.get('units',[]) for r in unit.get('sources',[])]
            report['audit']={'invocations':len(invs),'statuses':{i.id:i.status for i in invs},
                'all_launched_approved':all(any(a.invocation_id==i.id and a.status=='consumed' for a in approvals) for i in invs if i.started_at),
                'usage_records':len(usage),'usage_complete':all(u.completeness=='complete' for u in usage),
                'total_input':sum(u.input_total or 0 for u in usage),'total_output':sum(u.output_total or 0 for u in usage),
                'source_chunks':len(chunks),'study_references':len(refs),
                'study_quotes_located':all(r.get('chunk_id') in chunks and quote_in(r.get('quote',''),chunks[r['chunk_id']]) for r in refs),
                'evidence_items':len(evidence)}
        if view['phase']=='done':
            before=calls()
            records=[{'id':'demo-baseline','label':'流程验收基线（演示）','dataset':'synthetic-only',
                'protocol':'固定数值，不是论文实验','metric':'error','direction':'min','provenance':'demo',
                'samples':[3,4,5],'note':'仅验证录入和描述统计'},
                {'id':'demo-change','label':'流程验收改动（演示）','dataset':'synthetic-only',
                'protocol':'固定数值，不是论文实验','metric':'error','direction':'min','provenance':'demo',
                'samples':[1,2,3],'note':'仅验证录入和描述统计'}]
            measured=api(client,'PUT',f'/api/papers/{task}/experiments',json={'expected_version':0,'records':records}).json()
            assert measured['version']==1 and measured['comparisons'][1]['delta']==-2
            report['experiment_version_saved']=1
            api(client,'POST',f'/api/papers/{task}/reexport')
            current=api(client,'GET',f'/api/papers/{task}').json()
            for artifact in current['artifacts']:
                data=api(client,'GET',f"/api/papers/{task}/artifacts/{artifact['name']}").content
                (run/'downloads').mkdir(exist_ok=True)
                (run/'downloads'/artifact['name']).write_bytes(data)
            with zipfile.ZipFile(run/'downloads/research-bundle.zip') as z:
                manifest=json.loads(z.read('MANIFEST.json'))
                assert all(sha(z.read(name))==meta['sha256'] for name,meta in manifest.items())
                assert json.loads(z.read('experiments.json'))['version']==1
                report['bundle_files_verified']=len(z.namelist())
            assert calls()==before
            report['export_and_experiments_zero_extra_calls']=True
        report['elapsed_seconds']=round(time.monotonic()-started,2)
        write(run/'research-result.json',report)
    if view['phase']=='done':
        before=calls()
        with TestClient(new_app()) as client:
            restored=api(client,'GET',f'/api/papers/{task}').json()
            assert restored['phase']=='done'
            assert api(client,'GET',f'/api/papers/{task}/experiments').json()['version']==1
            api(client,'POST',f'/api/papers/{task}/reexport')
            assert calls()==before
        report['restart_and_reexport_zero_extra_calls']=True
    write(run/'research-result.json',report)
    print(json.dumps({k:v for k,v in report.items() if k not in ('papers','search_sources','audit')},ensure_ascii=False),flush=True)
    return report


def small_study_check(run, profile, desktop_root, mode, broader=False):
    """Separate reduced-scope acceptance, charged only against remaining allowance."""
    import zipfile
    from fastapi.testclient import TestClient
    from sqlalchemy import select
    from agenthub_adapters.deepseek import DeepSeekAdapter
    from agenthub_adapters.routing import RoutingAdapter
    from agenthub_adapters.store import ExecutionStore
    from agenthub_kernel import config
    from agenthub_kernel.db import Database, InvocationRow, UsageLedgerRow
    from agenthub_kernel.papers.db import PaperChunkRow, PaperDocumentRow
    from agenthub_kernel.papers.net import HttpFetcher
    from agenthub_kernel.papers.parse import quote_in
    previous=json.loads((run/'research-result.json').read_text(encoding='utf-8'))
    if previous['status']!='blocked':raise ValueError('retain the original blocked run; do not silently replace it')
    label='small-study-fallback' if broader else 'small-study'
    if (run/(label+'-result.json')).exists():raise ValueError('small study is one-shot; no automatic replay')
    db=Database('sqlite:///'+(run/'hub.db').as_posix())
    with db.session() as s:
        assert not s.scalar(select(InvocationRow.id).where(InvocationRow.status.in_(['starting','running','unknown'])))
        ledger=list(s.scalars(select(UsageLedgerRow)))
        assert ledger and all(u.completeness=='complete' for u in ledger)
        spent=sum(u.input_total+u.output_total for u in ledger)
    remaining=50000-spent
    if remaining<8000:raise ValueError('insufficient remaining allowance; never increase it')
    proxy=network(profile,mode)
    os.environ.pop('AGENT_HUB_DESKTOP',None)
    os.environ['AGENT_HUB_DATA_DIR']=str(run)
    os.environ['AGENT_HUB_USER_TOKEN']=uuid.uuid4().hex
    config.DATA_DIR=str(run)
    headers={'Authorization':'Bearer '+os.environ['AGENT_HUB_USER_TOKEN']}
    item=profile['agents']['deepseek']
    adapter=DeepSeekAdapter(item['base_url'],item['model'],credentials_path=str(desktop_root/'credentials/deepseek.json'),
        store=ExecutionStore(str(run/'deepseek-executions.db'),table='deepseek_executions'),
        proxy=proxy,ca_bundle=profile.get('ca_bundle') or '',timeout_seconds=120,repo_root=str(ROOT),stub='')
    from agenthub_rt.api import create_app
    def app():return create_app(db=db,adapter=RoutingAdapter({'deepseek':adapter}),
        paper_fetcher=HttpFetcher(timeout=20),paper_root=str(run/'papers'))
    def api(client,method,path,**kw):
        r=client.request(method,path,headers=headers,**kw)
        if r.status_code>=400:
            write(run/'small-study-api-error.json',{'path':path,'status':r.status_code,'detail':r.json()})
            raise RuntimeError(f'HTTP {r.status_code} {path}')
        return r
    def count():
        with db.session() as s:return len(list(s.scalars(select(UsageLedgerRow))))
    report={'scope':'single-source single-core study, no reviewer or research proposals',
            'prior_real_tokens':spent,'remaining_token_allowance':remaining,'max_calls':7,
            'per_call_reservation':8000,'reasoning_effort':'none','simulated':False}
    started=time.monotonic()
    instance=app()
    with TestClient(instance) as client:
        body={'client_request_id':'online-'+label,'request':{
            'topic':'RAGAS' if broader else 'RAGAS automated evaluation of retrieval augmented generation and faithfulness metric',
            'year_from':2023,'year_to':2024,'target_papers':1,'language':'zh','fulltext_policy':'continue_allowed',
            'sources':['arxiv'],'candidates_per_query':5,'sections':2,'mode':'study','core_papers':1,'repair_rounds':0},
            'roles':{'main_agent':'deepseek','review_agent':None,
                'models':{'deepseek':{'model':item['model'],'reasoning_effort':'none'}}},
            'resources':{'allow_pdf_download':True,'allow_public_fulltext':False,'local_roots':[]},
            'execution_mode':'strict','max_calls':7,'token_budget_total':remaining,'token_budget_per_call':8000}
        check=api(client,'POST','/api/papers/preflight',json=body).json()
        write(run/(label+'-preflight.json'),check)
        assert check['ok'],check['blockers']
        view=api(client,'POST','/api/papers',json=body).json();task=view['task_id'];report['task_id']=task
        write(run/(label+'-result.json'),report)
        last=None
        while True:
            view=api(client,'GET',f'/api/papers/{task}').json()
            sig=(view['status'],view['phase'],view['usage']['calls'])
            if sig!=last:
                print(json.dumps({'small_study':task,'status':sig[0],'phase':sig[1],'calls':sig[2]}),flush=True)
                write(run/(label+'-view.json'),view);last=sig
            if view['status']=='waiting_input':
                report['fulltext_gate_observed']=True
                api(client,'POST',f'/api/papers/{task}/supplement/choice',
                    json={'request_id':view['supplement']['request_id'],'choice':'continue'})
            elif view['status']!='running':break
            if time.monotonic()-started>10*60:
                api(client,'POST',f'/api/papers/{task}/stage/pause');report['deadline']=True;break
            instance.state.autopilot.tick();instance.state.worker.wait_idle(1);time.sleep(.2)
        report.update(status=view['status'],phase=view['phase'],quality_status=view['quality_status'],usage=view['usage'])
        write(run/(label+'-view.json'),view)
        if view['phase']=='done':
            before=count()
            row={'id':'demo','label':'在线流程验收（演示数值）','dataset':'synthetic-only','protocol':'非论文实验',
                'metric':'error','direction':'min','provenance':'demo','samples':[3,4,5],'note':'仅测试保存与导出'}
            measured=api(client,'PUT',f'/api/papers/{task}/experiments',json={'expected_version':0,'records':[row]}).json()
            assert measured['comparisons'][0]['mean']==4
            api(client,'POST',f'/api/papers/{task}/reexport')
            final=api(client,'GET',f'/api/papers/{task}').json()
            for artifact in final['artifacts']:
                data=api(client,'GET',f"/api/papers/{task}/artifacts/{artifact['name']}").content
                (run/(label+'-downloads')).mkdir(exist_ok=True)
                (run/(label+'-downloads')/artifact['name']).write_bytes(data)
            with zipfile.ZipFile(run/(label+'-downloads')/'research-bundle.zip') as z:
                manifest=json.loads(z.read('MANIFEST.json'))
                assert all(sha(z.read(name))==meta['sha256'] for name,meta in manifest.items())
                report['bundle_files_verified']=len(z.namelist())
            assert count()==before
            report['experiments_and_reexport_zero_extra_calls']=True
        with db.session() as s:
            chunks={c.id:c.text for c in s.scalars(select(PaperChunkRow).join(PaperDocumentRow,
                PaperChunkRow.document_id==PaperDocumentRow.id).where(PaperDocumentRow.task_id==task))}
            refs=[r for st in view['outcome'].get('studies',{}).values() for u in st.get('units',[]) for r in u.get('sources',[])]
            report.update(source_chunks=len(chunks),study_references=len(refs),
                source_quotes_located=bool(refs) and all(r['chunk_id'] in chunks and quote_in(r['quote'],chunks[r['chunk_id']]) for r in refs))
            ledger=list(s.scalars(select(UsageLedgerRow)))
            report['all_runs_real_tokens']=sum((u.input_total or 0)+(u.output_total or 0) for u in ledger)
            report['all_runs_model_calls']=len(ledger)
        write(run/(label+'-result.json'),report)
    if view['phase']=='done':
        before=count()
        with TestClient(app()) as client:
            assert api(client,'GET',f'/api/papers/{task}/experiments').json()['version']==1
            api(client,'POST',f'/api/papers/{task}/reexport')
            assert count()==before
        report['restart_and_reexport_zero_extra_calls']=True
    report['elapsed_seconds']=round(time.monotonic()-started,2)
    write(run/(label+'-result.json'),report)
    print(json.dumps(report),flush=True)
    return report


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--phase',choices=['inspect','sources','research','small-study'],required=True)
    parser.add_argument('--network',choices=['configured','direct'],default='configured')
    parser.add_argument('--run',required=True)
    parser.add_argument('--approve-online-test',action='store_true')
    parser.add_argument('--resume',action='store_true',help='reuse the proven channel after the recorded zero-launch budget refusal only')
    parser.add_argument('--broader',action='store_true',help='separate one-shot small-study acceptance of the bounded empty-query fallback')
    args=parser.parse_args()
    if not args.approve_online_test:parser.error('explicit --approve-online-test required')
    run=Path(args.run).resolve()
    if not run.is_relative_to(ROOT/'var'):parser.error('run must be within this worktree var')
    run.mkdir(parents=True,exist_ok=True)
    profile,desktop_root=configured_profile()
    if args.phase=='inspect':
        report=inspect(profile,desktop_root)
        write(run/'environment.json',report)
        print(json.dumps(report))
        return 0 if report['profile_exists'] and report['deepseek_credentials_present'] else 2
    if args.phase=='sources':
        result=source_check(run,profile,args.network)
        return 0 if all(r.get('ok') and r.get('parsed') for r in result['results']) else 2
    if args.phase=='research':result=research_check(run,profile,desktop_root,args.network,args.resume)
    else:result=small_study_check(run,profile,desktop_root,args.network,args.broader)
    # A valid run record or an empty export is not a successful research result.
    return 0 if result.get('status')=='completed' and result.get('source_quotes_located') else 2


if __name__=='__main__':raise SystemExit(main())
