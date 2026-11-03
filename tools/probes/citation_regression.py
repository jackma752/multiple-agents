"""Public cached PDF regression and opt-in bounded real-model acceptance."""
from __future__ import annotations
import argparse
import copy
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
for sub in ('packages/contracts', 'packages/adapters', 'apps/worker', 'apps/api', '.'):
    sys.path.insert(0, str(ROOT / sub))

from tools.probes.strict_online import approve_invocation, profile, run_summary, write, once
from agenthub_kernel.papers.citations import CitationError, apply_source_patch
from agenthub_kernel.papers.guidance import STUDY, SOURCE_REPAIR, payload, pick, study_result
from agenthub_kernel.papers.parse import chunk, parse_bytes, quote_in
from agenthub_kernel.papers.templates import render
from agenthub_kernel.papers.validate import FormatError, parse_json


def fixture():
    source = ROOT.parent / 'p6-online-080/var/online-080'
    with sqlite3.connect((source / 'hub.db').as_uri()+'?mode=ro', uri=True) as db:
        prompt = json.loads(db.execute('select snapshot from invocations where id=?', ('inv-8ce25b88af45',)).fetchone()[0])['task_input']
        rel = db.execute('select rel_path from paper_documents where id=?', ('doc-8abbe6ce36a7',)).fetchone()[0]
    data = json.loads(prompt.split('<<<DATA (untrusted material)\n')[1].split('\nDATA>>>')[0])
    assert data['paper']['title'] == 'Ragas: Automated Evaluation of Retrieval Augmented Generation'
    pdf = (source / 'papers' / rel).resolve()
    assert pdf.is_relative_to(source / 'papers')
    raw = pdf.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    assert digest == '6c24762a3f277fd120ed1adf34b01420ecf92f18ff6d1b5435fa0a09cb804ecb'
    oldrun = ROOT.parent / 'online-strict-082/var/online-082'
    with sqlite3.connect((oldrun / 'paper-executions.db').as_uri()+'?mode=ro', uri=True) as db:
        old = json.loads(db.execute('select payload from deepseek_executions where invocation_id=?', ('inv-3b1508c9b78a',)).fetchone()[0])
    oldout = parse_json(old.get('final_message') or old.get('reply'))
    oldchunks = [SimpleNamespace(id=c['chunk_id'], seq=int(c['chunk_id'].rsplit('-',1)[1]),
        page=c['pdf_page'], text=c['text'], document_id='public-ragas') for c in data['chunks']]
    parsed = parse_bytes(raw, name='public-ragas.pdf')
    assert parsed.status == 'ok'
    cs = [SimpleNamespace(id=f'chk-8abbe6ce36a7-{c.seq}', seq=c.seq, page=c.page, text=c.text,
                         document_id='public-ragas') for c in chunk(parsed)]
    return digest, data, oldout, oldchunks, parsed, cs


def offline(run):
    digest, data, oldout, oldchunks, parsed, cs = fixture()
    report = {'pdf_sha256': digest, 'new_extractor': parsed.extractor, 'layout_provenance': parsed.provenance,
        'old_task_and_approved_inputs_unchanged': True, 'online_requests': 0}
    try:
        study_result(oldout, oldchunks)
    except CitationError as exc:
        report.update(old_source_remaining_errors=exc.issues,
                      old_source_exact_relocations=len(exc.candidate['citation_audit']))
    checked = study_result(oldout, cs)
    refs = [r for u in checked['units'] for r in u.get('sources', [])]
    report.update(new_parse_recheck_passed=True, units=len(checked['units']), references=len(refs),
        quotes_matched=sum(quote_in(r['quote'], next(c.text for c in cs if c.id == r['chunk_id'])) for r in refs),
        new_source_relocations=len(checked['citation_audit']))
    write(run / 'offline-regression.json', report)
    print(json.dumps(report, ensure_ascii=False), flush=True)


def online(run):
    from sqlalchemy import select
    from agenthub.budget import estimate_visible_tokens
    from agenthub.contracts import Policy
    from agenthub_adapters.deepseek import DeepSeekAdapter
    from agenthub_adapters.store import ExecutionStore
    from agenthub_kernel.db import Database, UsageLedgerRow
    from agenthub_kernel.worker import Worker
    import os
    if (run / 'online-hub.db').exists():
        raise ValueError('one-shot run already exists; never replay')
    digest, original, oldout, oldchunks, parsed, allchunks = fixture()
    cs = pick(allchunks)
    data = {'topic': 'RAGAS', 'paper': original['paper'], 'chunks': payload(cs),
        'coverage': {'selected': len(cs), 'total': len(allchunks), 'selection': 'bounded methods and adjacent source chunks'}}
    settings, directory = profile()
    os.environ.pop('AGENT_HUB_DESKTOP', None)
    item = settings['agents']['deepseek']
    assert item['enabled'] and item['model'] == 'deepseek-flash'
    adapter = DeepSeekAdapter(item['base_url'], item['model'], credentials_path=str(directory/'credentials/deepseek.json'),
        store=ExecutionStore(str(run/'online-executions.db'), table='deepseek_executions'),
        proxy=settings.get('proxy') or '', ca_bundle=settings.get('ca_bundle') or '', timeout_seconds=120,
        repo_root=str(ROOT), stub='')
    assert not adapter.stub and adapter._transport is None
    db = Database('sqlite:///'+(run/'online-hub.db').as_posix()); db.create_all()
    policy = Policy(max_agent_invocations=3, max_subagents_concurrent=0, max_depth=0,
        token_budget_total=24000, token_budget_per_call=12000)
    report = {'pdf_sha256': digest, 'extractor': parsed.extractor, 'scope': 'targeted citation acceptance, not full research quality',
        'limits': {'total_tokens': 24000, 'per_call_reservation': 12000, 'max_calls': 3}, 'cases': []}
    task = None; repair = None; history = []
    for attempt in range(3):
        template, request = STUDY, data
        if repair:
            template = SOURCE_REPAIR
            bad = {i['unit_id'] for i in repair.issues}
            request = {'issues': repair.issues, 'chunks': data['chunks'],
                'units': [{k: u[k] for k in ('id','title','text','sources')} for u in repair.candidate['units'] if u['id'] in bad]}
        prompt = render(template, request)
        assert estimate_visible_tokens(prompt) <= 12000
        iid, task = approve_invocation(db, adapter, 'deepseek', prompt, policy=policy, task_id=task,
                                     model=item['model'], effort='none')
        once(run, f'online-{attempt}', {'invocation': iid, 'authorized_by': 'user 2026-11-03 按调研方案执行修复及复测',
            'pdf_sha256': digest, 'prompt_sha256': hashlib.sha256(prompt.encode()).hexdigest(),
            'template': template.meta(), 'estimate_visible_tokens': estimate_visible_tokens(prompt)})
        outcome = Worker(db, adapter).run_one(iid)
        row = run_summary(db, adapter, iid); row['template'] = template.id
        last = adapter.last_result(iid); row['validation_passed'] = False
        if row['status'] == 'succeeded' and last:
            try:
                out = parse_json(last['final_message'])
                if repair:
                    out = apply_source_patch(out, repair.candidate, repair.issues)
                result = study_result(out, cs)
                result['citation_audit'] = history + result.get('citation_audit', [])
                refs = [r for u in result['units'] for r in u.get('sources', [])]
                row.update(validation_passed=True, units=len(result['units']), references=len(refs),
                    quotes_matched=sum(quote_in(r['quote'], next(c.text for c in cs if c.id == r['chunk_id'])) for r in refs),
                    backend_relocations=len(result['citation_audit']), math_state=result['math_state'])
                write(run/'validated-study.json', result)
            except CitationError as exc:
                repair = exc
                history.extend(exc.candidate.get('citation_audit', []))
                row['source_errors'] = exc.issues
            except FormatError as exc:
                row['format_error'] = str(exc)
        report['cases'].append(row)
        with db.session() as s:
            ledger = list(s.scalars(select(UsageLedgerRow)))
            report['usage_complete'] = all(u.completeness == 'complete' for u in ledger)
            report['tokens'] = sum((u.input_total or 0)+(u.output_total or 0) for u in ledger)
        write(run/'online-result.json', report); print(json.dumps(row, ensure_ascii=False), flush=True)
        if row['validation_passed'] or row['status'] != 'succeeded' or not report['usage_complete'] or repair is None or 24000-report['tokens'] < 12000:
            break
    report['passed'] = bool(report['cases'][-1]['validation_passed'])
    report['remaining_tokens'] = 24000-report['tokens']
    write(run/'online-result.json', report); db.engine.dispose()
    return report['passed']


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--phase', choices=['offline','online'], required=True)
    parser.add_argument('--run', required=True)
    parser.add_argument('--approve-online-test', action='store_true')
    args = parser.parse_args(); run = Path(args.run).resolve()
    if not run.is_relative_to(ROOT/'var'):
        parser.error('run inside this worktree var required')
    run.mkdir(parents=True, exist_ok=True)
    if args.phase == 'offline':
        offline(run); return 0
    if not args.approve_online_test:
        parser.error('explicit online opt-in required')
    return 0 if online(run) else 2


if __name__ == '__main__':
    raise SystemExit(main())
