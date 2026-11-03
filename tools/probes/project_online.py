"""Opt-in bounded real project acceptance. Imports never launch models or network."""
from __future__ import annotations
import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import time
import uuid
import zipfile

ROOT = Path(__file__).resolve().parents[2]
for sub in ('packages/contracts', 'packages/adapters', 'apps/worker', 'apps/api', '.'):
    sys.path.insert(0, str(ROOT/sub))

TOTAL_TOKENS = 100000
STAGE_TOKENS = 98000
PER_CALL = 20000
STAGE_CALLS = 15


def acceptance(report):
    """No empty/vacuous success and no implied scientific certification."""
    return bool(report.get('status') == 'completed' and report.get('phase') == 'done'
        and report.get('quality_status') == 'passed' and report.get('proof_passed')
        and report.get('fulltext_documents', 0) >= 1 and report.get('study_units', 0) >= 10
        and report.get('references', 0) > 0 and report.get('references') == report.get('quotes_matched')
        and report.get('candidate_count', 0) >= 2 and report.get('candidate_novelty_unverified')
        and report.get('all_launched_approved') and report.get('all_stage_calls_granted')
        and report.get('usage_complete') and report.get('active_or_unknown') == 0
        and 0 < report.get('tokens', 0) <= TOTAL_TOKENS and report.get('model_calls', 100) <= 16
        and report.get('bundle_verified') and report.get('project_saved')
        and report.get('experiments_saved') and report.get('restart_no_extra_calls')
        and report.get('pause_no_new_calls'))


def run_project(run, *, resume=False, supplement_public_url=False):
    from fastapi.testclient import TestClient
    from sqlalchemy import select
    from agenthub_adapters.deepseek import DeepSeekAdapter
    from agenthub_adapters.routing import RoutingAdapter
    from agenthub_adapters.store import ExecutionStore
    from agenthub_kernel import config
    from agenthub_kernel.db import ApprovalRow, Database, InvocationRow, StageGrantRow, UsageLedgerRow
    from agenthub_kernel.papers.db import PaperChunkRow, PaperDocumentRow
    from agenthub_kernel.papers.net import HttpFetcher
    from agenthub_kernel.papers.parse import quote_in
    from agenthub_rt.api import create_app
    from tools.probes.p6_online import configured_profile, network
    from tools.probes.strict_online import once, write

    if not resume and any(run.iterdir()):
        raise ValueError('fresh empty run directory required; no automatic replay')
    settings, desktop_root = configured_profile()
    proxy = network(settings, 'configured')
    item = settings['agents']['deepseek']
    assert item['enabled'] and item['model'] == 'deepseek-flash'
    os.environ.pop('AGENT_HUB_DESKTOP', None)
    os.environ['AGENT_HUB_DATA_DIR'] = str(run)
    os.environ['AGENT_HUB_USER_TOKEN'] = uuid.uuid4().hex
    config.DATA_DIR = str(run)
    headers = {'Authorization': 'Bearer '+os.environ['AGENT_HUB_USER_TOKEN']}
    adapter = DeepSeekAdapter(item['base_url'], item['model'],
        credentials_path=str(desktop_root/'credentials/deepseek.json'),
        store=ExecutionStore(str(run/'executions.db'), table='deepseek_executions'),
        proxy=proxy, ca_bundle=settings.get('ca_bundle') or '', timeout_seconds=150,
        repo_root=str(ROOT), stub='')
    assert not adapter.stub and adapter._transport is None
    router = RoutingAdapter({'deepseek': adapter})
    fetcher = HttpFetcher(timeout=20)
    assert not fetcher.simulated
    db = Database('sqlite:///'+(run/'hub.db').as_posix())
    def app():
        return create_app(db=db, adapter=router, paper_fetcher=fetcher, paper_root=str(run/'papers'))
    def api(client, method, path, **kwargs):
        response = client.request(method, path, headers=headers, **kwargs)
        if response.status_code >= 400:
            write(run/'api-error.json', {'path': path, 'status': response.status_code, 'detail': response.json()})
            raise RuntimeError(f'HTTP {response.status_code} {path}')
        return response
    def started_calls():
        with db.session() as session:
            return len(list(session.scalars(select(InvocationRow.id).where(InvocationRow.started_at.is_not(None)))))
    report = {'same_model_reviewer': True, 'simulated': False,
              'scientific_correctness_certified': False, 'tokens_limit': TOTAL_TOKENS}
    if resume:
        report = json.loads((run/'result.json').read_text(encoding='utf-8'))
        assert report['tokens_limit'] == TOTAL_TOKENS and report['proof_passed']
        number = len(list(run.glob('resume-*-started.json')))+1
        once(run, f'resume-{number}', {'reason': 'explicit resume within original limits',
            'reuse_task': report['task_id'], 'supplement_public_url': supplement_public_url})
    else:
        once(run, 'project', {'authorized_by': 'user 2026-11-03 在线通过后合并 main 更新 GitHub',
            'limits': {'total_tokens': TOTAL_TOKENS, 'stage_tokens': STAGE_TOKENS,
                       'per_call_reservation': PER_CALL, 'stage_calls': STAGE_CALLS, 'proof_calls': 1}})
    instance = app()
    prior_elapsed = max(0, time.time()-(run/'project-started.json').stat().st_mtime) if resume else 0
    started = time.monotonic()-prior_elapsed
    task_id = None
    with TestClient(instance) as client:
        if resume:
            task_id = report['task_id']
            view = api(client, 'GET', f'/api/papers/{task_id}').json()
            assert view['status']=='paused'
            with db.session() as session:
                assert not session.scalar(select(InvocationRow.id).where(InvocationRow.status.in_(['starting','running','unknown','queued'])))
                grants = {g.invocation_id for g in session.scalars(select(StageGrantRow))}
                proofs = [i for i in session.scalars(select(InvocationRow)) if i.started_at and i.id not in grants]
                assert len(proofs)==1 and proofs[0].status=='succeeded'
                proof = {'invocation_id': proofs[0].id}
            before = started_calls(); instance.state.autopilot.tick()
            report['pause_no_new_calls'] = before==started_calls()
            if supplement_public_url:
                selected = [p for p in view['papers'] if p['included']]
                assert len(selected)==1 and selected[0]['source_id'].split('v')[0]=='2309.15217'
                assert selected[0]['title'].lower()=='ragas: automated evaluation of retrieval augmented generation'
                req = view['supplement']
                assert req['status']=='awaiting_choice' and req['remaining']==1
                api(client, 'POST', f'/api/papers/{task_id}/supplement/choice',
                    json={'request_id':req['request_id'], 'choice':'supplement'})
                added = api(client, 'POST', f'/api/papers/{task_id}/supplement/refs', json={
                    'request_id':req['request_id'], 'kind':'url', 'value':'https://arxiv.org/pdf/2309.15217',
                    'target_paper_id':selected[0]['paper_id']}).json()
                api(client, 'POST', f'/api/papers/{task_id}/supplement/submit',
                    json={'request_id':req['request_id'], 'batch_id':added['batch_id']})
                report['supplement_public_url'] = 'https://arxiv.org/pdf/2309.15217'
                report['initial_fulltext_reason'] = selected[0]['fulltext_reason']
            api(client, 'POST', f'/api/papers/{task_id}/stage/resume')
        else:
            cid = api(client, 'POST', '/api/conversations', json={'title': '0.8.3 isolated project acceptance'}).json()['conversation_id']
            message = {'text': 'Reply exactly READY. No tools, files or subagents.', 'agent_id': 'deepseek',
                       'model': item['model'], 'reasoning_effort': 'none', 'execution_mode': 'strict',
                       'client_request_id': 'proof-budget-draft'}
            draft = api(client, 'POST', f'/api/conversations/{cid}/invocations', json=message).json()
            # A user budget change revokes the first draft; approve a fresh snapshot.
            api(client, 'POST', f"/api/tasks/{draft['task_id']}/policy", json={
                'max_subagents_total': 0, 'max_subagents_concurrent': 0, 'max_depth': 0,
                'max_agent_invocations': 1, 'token_budget_total': 2000, 'token_budget_per_call': 2000})
            message['client_request_id'] = 'proof-budget-approved'
            proof = api(client, 'POST', f'/api/conversations/{cid}/invocations', json=message).json()
            api(client, 'POST', f"/api/approvals/{proof['approval_id']}/approve")
            result = api(client, 'POST', f"/api/invocations/{proof['invocation_id']}/run", params={'wait': 'true'}).json()
            assert instance.state.worker.wait_idle(180)
            stored = adapter.last_result(proof['invocation_id']) or {}
            report['proof_passed'] = result['status'] == 'succeeded' and stored.get('final_message', '').strip() == 'READY'
            write(run/'result.json', report)
            print(json.dumps({'proof_passed': report['proof_passed']}), flush=True)
            if not report['proof_passed']:
                return report
            body = {'client_request_id': 'project-real-083', 'request': {
                'topic': 'RAGAS: automated evaluation of retrieval augmented generation, faithfulness, answer relevance and context relevance',
                'year_from': 2023, 'year_to': 2026, 'target_papers': 1, 'language': 'zh',
                'fulltext_policy': 'continue_allowed', 'sources': ['arxiv', 'openalex', 'crossref'],
                'candidates_per_query': 5, 'sections': 2, 'mode': 'project', 'core_papers': 1, 'repair_rounds': 1},
                'roles': {'main_agent': 'deepseek', 'review_agent': 'deepseek',
                          'models': {'deepseek': {'model': item['model'], 'reasoning_effort': 'none'}}},
                'resources': {'allow_pdf_download': True, 'allow_public_fulltext': True, 'allow_upload': True,
                              'allow_url_supplement': True, 'local_roots': [], 'max_download_mb': 20},
                'execution_mode': 'strict', 'max_calls': STAGE_CALLS,
                'token_budget_total': STAGE_TOKENS, 'token_budget_per_call': PER_CALL}
            check = api(client, 'POST', '/api/papers/preflight', json=body).json()
            write(run/'preflight.json', check)
            assert check['ok'], 'preflight blocked; see ignored report'
            view = api(client, 'POST', '/api/papers', json=body).json()
            task_id = view['task_id']; report['task_id'] = task_id
            write(run/'result.json', report)
        previous, paused = None, resume
        while True:
            view = api(client, 'GET', f'/api/papers/{task_id}').json()
            signature = (view['status'], view['phase'], view['usage']['calls'])
            if signature != previous:
                print(json.dumps({'status': signature[0], 'phase': signature[1], 'calls': signature[2]}), flush=True)
                write(run/'task-view.json', view); previous = signature
            if view['status'] == 'waiting_input':
                # This test requires full text. Never silently accept abstract-only.
                api(client, 'POST', f'/api/papers/{task_id}/stage/pause')
                report['fulltext_missing'] = True
                break
            if view['status'] != 'running':
                break
            if view['phase'] == 'screen' and not paused:
                api(client, 'POST', f'/api/papers/{task_id}/stage/pause')
                before = started_calls(); instance.state.autopilot.tick()
                assert instance.state.worker.wait_idle(180)
                report['pause_no_new_calls'] = started_calls() == before
                api(client, 'POST', f'/api/papers/{task_id}/stage/resume')
                paused = True
            if time.monotonic()-started > 25*60:
                api(client, 'POST', f'/api/papers/{task_id}/stage/pause')
                report['deadline'] = True
                break
            instance.state.autopilot.tick()
            instance.state.worker.wait_idle(1)
            time.sleep(.1)
        view = api(client, 'GET', f'/api/papers/{task_id}').json()
        write(run/'task-view.json', view)
        studies = view['outcome'].get('studies', {})
        units = [unit for study in studies.values() for unit in study.get('units', [])]
        refs = [ref for unit in units for ref in unit.get('sources', [])]
        candidates = view['outcome'].get('directions', {}).get('candidates', [])
        report.update(status=view['status'], phase=view['phase'], quality_status=view['quality_status'],
            study_units=len(units), references=len(refs), candidate_count=len(candidates),
            candidate_novelty_unverified=bool(candidates) and all(c['novelty']=='unverified' for c in candidates),
            source_results=view['outcome'].get('search_sources', []))
        with db.session() as session:
            documents = list(session.scalars(select(PaperDocumentRow).where(PaperDocumentRow.task_id==task_id)))
            ids = [d.id for d in documents]
            chunks = {c.id: c.text for c in session.scalars(select(PaperChunkRow).where(PaperChunkRow.document_id.in_(ids)))}
            report['fulltext_documents'] = sum(d.parse_status=='ok' and (d.chars or 0)>1000 for d in documents)
            report['quotes_matched'] = sum(r['chunk_id'] in chunks and quote_in(r['quote'], chunks[r['chunk_id']]) for r in refs)
            invs = list(session.scalars(select(InvocationRow).where(InvocationRow.started_at.is_not(None))))
            approvals = list(session.scalars(select(ApprovalRow)))
            grants = {g.invocation_id for g in session.scalars(select(StageGrantRow))}
            usage = list(session.scalars(select(UsageLedgerRow)))
            report.update(model_calls=len(invs),
                all_launched_approved=all(any(a.invocation_id==i.id and a.status=='consumed' for a in approvals) for i in invs),
                all_stage_calls_granted=all(i.id in grants for i in invs if i.id!=proof['invocation_id']),
                active_or_unknown=sum(i.status in ('starting','running','unknown','queued') for i in invs),
                usage_complete=len(usage)==len(invs) and all(u.completeness=='complete' for u in usage),
                input_tokens=sum(u.input_total or 0 for u in usage), output_tokens=sum(u.output_total or 0 for u in usage))
            report['tokens'] = report['input_tokens']+report['output_tokens']
        if view['phase']=='done' and candidates:
            before = started_calls()
            project = api(client, 'PUT', f'/api/papers/{task_id}/project', json={
                'expected_version': 0, 'selected_candidate': candidates[0]['id'],
                'notes': {'implementation': 'Authored acceptance note: implementation and experiments remain future work.'}}).json()
            report['project_saved'] = project['version']==1
            records = [{'id':'authored-demo', 'label':'Acceptance fixture (demo)', 'dataset':'synthetic-only',
                        'protocol':'Authored values; no paper experiment executed', 'metric':'error', 'direction':'min',
                        'provenance':'demo', 'samples':[3,4,5], 'note':'Storage and descriptive comparison test only'}]
            experiments = api(client, 'PUT', f'/api/papers/{task_id}/experiments', json={'expected_version':0,'records':records}).json()
            report['experiments_saved'] = experiments['version']==1 and experiments['comparisons'][0]['mean']==4
            api(client, 'POST', f'/api/papers/{task_id}/reexport')
            view = api(client, 'GET', f'/api/papers/{task_id}').json()
            downloads = run/'downloads'; downloads.mkdir()
            for artifact in view['artifacts']:
                raw = api(client, 'GET', f"/api/papers/{task_id}/artifacts/{artifact['name']}").content
                assert hashlib.sha256(raw).hexdigest()==artifact['sha256']
                (downloads/artifact['name']).write_bytes(raw)
            with zipfile.ZipFile(downloads/'research-bundle.zip') as bundle:
                manifest = json.loads(bundle.read('MANIFEST.json'))
                assert all(hashlib.sha256(bundle.read(n)).hexdigest()==m['sha256'] and len(bundle.read(n))==m['size'] for n,m in manifest.items())
                assert len(bundle.read('study.md'))>1500 and len(bundle.read('research-plan.md'))>1000
                assert json.loads(bundle.read('project.json'))['version']==1
                assert json.loads(bundle.read('experiments.json'))['version']==1
                report['bundle_files'] = len(bundle.namelist())
                report['bundle_verified'] = True
            assert started_calls()==before
        write(run/'result.json', report)
    if report.get('bundle_verified'):
        before = started_calls()
        with TestClient(app()) as client:
            assert api(client, 'GET', f'/api/papers/{task_id}').json()['phase']=='done'
            assert api(client, 'GET', f'/api/papers/{task_id}/project').json()['version']==1
            assert api(client, 'GET', f'/api/papers/{task_id}/experiments').json()['version']==1
            api(client, 'POST', f'/api/papers/{task_id}/reexport')
        report['restart_no_extra_calls'] = before==started_calls()
    report['passed'] = acceptance(report)
    report['elapsed_seconds'] = round(time.monotonic()-started, 2)
    write(run/'result.json', report)
    db.engine.dispose()
    print(json.dumps({k:v for k,v in report.items() if k!='source_results'}), flush=True)
    return report


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--approve-online-test', action='store_true', required=True)
    parser.add_argument('--run', required=True)
    parser.add_argument('--resume', action='store_true', help='resume the retained paused task; never recreate model calls')
    parser.add_argument('--supplement-public-url', action='store_true', help='for the retained RAGAS task only: submit its official canonical PDF URL')
    args = parser.parse_args()
    run = Path(args.run).resolve()
    if not run.is_relative_to(ROOT/'var'):
        parser.error('run must be inside this worktree var')
    run.mkdir(parents=True, exist_ok=True)
    if args.supplement_public_url and not args.resume:
        parser.error('supplement requires explicit resume of the retained task')
    report = run_project(run, resume=args.resume, supplement_public_url=args.supplement_public_url)
    return 0 if report.get('passed') else 2


if __name__=='__main__':
    raise SystemExit(main())
