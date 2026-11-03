"""Two explicitly authorized follow-up calls on the recorded public study only."""
import argparse
import copy
import json
from pathlib import Path
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[2]
for sub in ('packages/contracts','packages/adapters','apps/worker','apps/api','.'):
    sys.path.insert(0,str(ROOT/sub))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--approve-online-test', action='store_true', required=True)
    parser.add_argument('--run', required=True)
    parser.add_argument('--review-only', action='store_true', help='consume only the unstarted second call from the same 40000-token ledger')
    args = parser.parse_args()
    run = Path(args.run).resolve()
    if not run.is_relative_to(ROOT/'var') or run.exists()!=args.review_only:
        parser.error('fresh directory inside this worktree var required')
    run.mkdir(parents=True,exist_ok=args.review_only)
    source = ROOT/'var/project-online-083'
    from types import SimpleNamespace
    from sqlalchemy import select
    from agenthub.contracts import Policy
    from agenthub_adapters.deepseek import DeepSeekAdapter
    from agenthub_adapters.store import ExecutionStore
    from agenthub_kernel.db import Database, UsageLedgerRow
    from agenthub_kernel.worker import Worker
    from agenthub_kernel.papers.guidance import PATCH, CHECK, payload, unit_payload, patch_result, review_result
    from agenthub_kernel.papers.parse import quote_in
    from agenthub_kernel.papers.templates import render
    from agenthub_kernel.papers.validate import FormatError, parse_json
    from tools.probes.strict_online import approve_invocation, profile, run_summary, once, write
    with sqlite3.connect((source/'hub.db').as_uri()+'?mode=ro',uri=True) as old:
        outcome = json.loads(old.execute('select outcome from paper_tasks').fetchone()[0])
        seed = next(iter(outcome['studies'].values()))
        old.row_factory = sqlite3.Row
        chunks = [SimpleNamespace(**dict(row)) for row in old.execute('select * from paper_chunks order by seq')]
    issues = seed['issues']
    editable = {i['unit_id'] for i in issues}
    dependencies = set(editable)
    for unit in reversed(seed['units']):
        if unit['id'] in dependencies:
            dependencies.update(unit.get('depends_on',[]))
    ref_ids = {r['chunk_id'] for u in seed['units'] if u['id'] in dependencies for r in u.get('sources',[])}
    chosen = [c for c in chunks if c.id in ref_ids or c.page in (3,4,5)]
    data = {'topic':'RAGAS study correction; no new search', 'issues':issues, 'missing':seed['missing'],
        'editable_ids':sorted(editable), 'allow_additions':False,
        'units':[u for u in seed['units'] if u['id'] in dependencies], 'chunks':payload(chosen),
        'material_constraint':('Correct the reported numerical interpretation, symbol and formula domain errors. '
            'All sources here are existing public PDF chunks. No new paper or source was obtained. '
            'For empty denominators state the source does not specify an author convention; any proposed N/A convention '
            'must be labeled teaching reconstruction. Keep missing items and math_state=incomplete. '
            'Distinguish human-human agreement from metric-human agreement. Use ordinary Chinese, not literal Unicode escape sequences.')}
    settings, directory = profile()
    item = settings['agents']['deepseek']
    adapter = DeepSeekAdapter(item['base_url'],item['model'],credentials_path=str(directory/'credentials/deepseek.json'),
        store=ExecutionStore(str(run/'executions.db'),table='deepseek_executions'),
        proxy=settings.get('proxy') or '',ca_bundle=settings.get('ca_bundle') or '',
        timeout_seconds=180,repo_root=str(ROOT),stub='')
    assert not adapter.stub and adapter._transport is None
    db = Database('sqlite:///'+(run/'hub.db').as_posix());db.create_all()
    policy = Policy(max_agent_invocations=2,max_subagents_concurrent=0,max_depth=0,
        token_budget_total=40000,token_budget_per_call=20000)
    report = {'authorized_extra_tokens':40000,'max_calls':2,'same_model_reviewer':True,
        'original_result_unchanged':True,'calls':[],'passed':False}
    task = None; corrected = None
    if args.review_only:
        report = json.loads((run/'result.json').read_text(encoding='utf-8'))
        assert len(report['calls'])==1 and report['calls'][0]['status']=='succeeded'
        assert report['tokens']<40000 and report['usage_complete']
        from agenthub_kernel.db import InvocationRow
        with db.session() as session:
            calls=list(session.scalars(select(InvocationRow)))
            assert len(calls)==1 and calls[0].status=='succeeded'
            task=calls[0].task_id
        corrected=json.loads((run/'corrected-study.json').read_text(encoding='utf-8'))
        report.pop('stopped',None)
    for number, template in enumerate((PATCH,CHECK)):
        if args.review_only and number==0:continue
        if number:
            if corrected is None:break
            with db.session() as session:
                used = list(session.scalars(select(UsageLedgerRow)))
                spent = sum((r.input_total or 0)+(r.output_total or 0) for r in used)
                if any(r.completeness!='complete' for r in used) or 40000-spent<=0:
                    report['stopped']='insufficient remaining reservation';break
            # Exact quoted windows, including nearby context, avoid retransmitting
            # whole chunks and backend-only hashes/offsets for the review.
            review_units=[u for u in corrected['units'] if u['id'] in editable or u['kind'] in ('step','result')]
            intervals={c.id:[] for c in chosen}
            for u in review_units:
                for r in u.get('sources',[]):
                    c=next(c for c in chosen if c.id==r['chunk_id'])
                    loc=r['source_location']
                    intervals[c.id].append((max(0,loc['chunk_start']-80),min(len(c.text),loc['chunk_end']+80)))
            windows=[]
            for c in chosen:
                merged=[]
                for a,b in sorted(intervals[c.id]):
                    if merged and a<=merged[-1][1]:merged[-1][1]=max(b,merged[-1][1])
                    else:merged.append([a,b])
                if merged:windows.append({'chunk_id':c.id,'pdf_page':c.page,
                    'spans':[{'start':a,'end':b,'text':c.text[a:b]} for a,b in merged]})
            data = {'units':unit_payload(review_units),'chunks':windows,
                'dependency_context':[{k:u[k] for k in ('id','kind','title','latex','depends_on') if k in u}
                    for u in corrected['units'] if u not in review_units],
                'previous_issues':[{k:i[k] for k in ('id','unit_id','severity','kind','closure')} for i in issues],
                'changed_ids':sorted(editable), 'scope_note':('Do not demand invented author conventions or unseen prompt templates. '
                    'Retained missing materials are explicit limitations, not closed proofs. Still report real errors and unsupported claims. '
                    'Review the changed units and the supplied dependency equations; introductory units are summarized. '
                    'List unresolved issues concisely without restating resolved issues. '
                    'Use normal Chinese characters rather than literal backslash-u sequences.')}
        prompt = render(template,data)
        if number:
            from agenthub.budget import estimate_visible_tokens
            from agenthub_kernel.service import PolicyService
            reserve=min(20000,40000-spent)
            report['review_prompt_characters']=len(prompt)
            report['review_prompt_estimate']=estimate_visible_tokens(prompt)
            if estimate_visible_tokens(prompt)>reserve:
                report['stopped']='review prompt exceeds remaining reservation';break
            policy=Policy(max_agent_invocations=2,max_subagents_concurrent=0,max_depth=0,
                token_budget_total=40000,token_budget_per_call=reserve)
            with db.session() as session:
                PolicyService(session).update_policy(task,policy,updated_by='online-test-user')
            report['review_reservation']=reserve
            report['review_prompt_characters']=len(prompt)
        iid,task = approve_invocation(db,adapter,'deepseek',prompt,policy=policy,task_id=task,model=item['model'],effort='none')
        once(run,f'call-{number}',{'invocation_id':iid,'template':template.meta(),
            'authorized_by':'user 2026-11-03 追加4万，继续在线复测'})
        Worker(db,adapter).run_one(iid)
        result = run_summary(db,adapter,iid);report['calls'].append(result)
        raw = adapter.last_result(iid) or {}
        if result['status']!='succeeded':break
        try:
            parsed = parse_json(raw['final_message'])
            if not number:
                corrected = patch_result(parsed,seed,chosen,editable,False,preserve_material_gaps=True)
                unchanged = [u for u in seed['units'] if u['id'] not in editable]
                assert unchanged==[u for u in corrected['units'] if u['id'] not in editable]
                assert corrected['missing']==seed['missing'] and corrected['math_state']=='incomplete'
                refs = [r for u in corrected['units'] for r in u.get('sources',[])]
                report.update(reference_count=len(refs),quotes_matched=sum(r['chunk_id'] in {c.id for c in chosen} and
                    quote_in(r['quote'],next(c.text for c in chosen if c.id==r['chunk_id'])) for r in refs),
                    unchanged_units_preserved=True,material_gaps_preserved=True)
                write(run/'corrected-study.json',corrected)
            else:
                review = review_result(parsed,{u['id'] for u in corrected['units']})
                write(run/'review.json',review)
                report['unresolved_issues'] = review['issues']
                report['substantive_issues'] = sum(i['severity']!='minor' and i['kind']!='style' for i in review['issues'])
                report['passed'] = report['reference_count']==report['quotes_matched'] and report['substantive_issues']==0
        except FormatError as exc:
            report['validation_error'] = str(exc)[:300]
            break
        write(run/'result.json',report)
        print(json.dumps(result),flush=True)
    with db.session() as session:
        ledger = list(session.scalars(select(UsageLedgerRow)))
        report['tokens'] = sum((r.input_total or 0)+(r.output_total or 0) for r in ledger)
        report['usage_complete'] = bool(ledger) and all(r.completeness=='complete' for r in ledger)
    report['passed'] = report['passed'] and report['tokens']<=40000 and report['usage_complete']
    write(run/'result.json',report);db.engine.dispose()
    print(json.dumps({k:v for k,v in report.items() if k!='unresolved_issues'}),flush=True)
    return 0 if report['passed'] else 2


if __name__=='__main__':
    raise SystemExit(main())
