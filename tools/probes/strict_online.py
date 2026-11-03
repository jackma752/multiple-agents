"""Opt-in online tests, isolated state, existing credential paths only.

Never prints or copies credentials. Raw outputs stay under ignored var.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import socket
import sqlite3
import sys
import threading
import time
import uuid
from urllib.parse import urlsplit
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
for sub in ('packages/contracts', 'packages/adapters', 'apps/worker', 'apps/api'):
    sys.path.insert(0, str(ROOT / sub))


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')


def profile():
    from agenthub_kernel import desktop
    directory = Path(os.environ.get('LOCALAPPDATA', '')) / 'AgentHub' / 'desktop'
    os.environ['AGENT_HUB_DESKTOP_HOME'] = str(directory)
    return desktop.load(), directory


def inspect(settings, directory):
    from agenthub_adapters.claude_code import STRICT_VERIFIED_SHA256
    proxy = urlsplit(settings.get('proxy') or '')
    reachable = None
    if proxy.hostname:
        try:
            with socket.create_connection((proxy.hostname, proxy.port or 80), timeout=2):
                reachable = True
        except OSError:
            reachable = False
    agents = {}
    for name, item in settings.get('agents', {}).items():
        agents[name] = {'enabled': bool(item.get('enabled')), 'version': item.get('version'),
            'executable_present': bool(item.get('path')) and Path(item['path']).is_file(),
            'config_directory_present': bool(item.get('config_dir')) and Path(item['config_dir']).is_dir()}
        if name == 'claude-code':
            agents[name]['known_strict_profile'] = item.get('sha256') in STRICT_VERIFIED_SHA256
            agents[name]['login_file_present'] = any((Path(item.get('config_dir') or '.') / x).is_file()
                for x in ('.credentials.json', 'credentials.json', 'auth.json'))
        if name == 'deepseek':
            agents[name].update(model=item.get('model'), endpoint_host=urlsplit(item.get('base_url') or '').hostname,
                credentials_present=(directory / 'credentials/deepseek.json').is_file())
    return {'agents': agents, 'profile_present': (directory / 'profile.json').is_file(),
            'proxy_configured': bool(proxy.hostname), 'proxy_reachable': reachable,
            'ca_file_present': bool(settings.get('ca_bundle')) and Path(settings['ca_bundle']).is_file(),
            'online_model_calls': 0}


def once(run, label, value):
    """Persist intent before launch; even an interrupted run cannot be replayed."""
    with (run / (label + '-started.json')).open('x', encoding='utf-8') as f:
        json.dump(value, f, ensure_ascii=False, indent=2)


def allow_file(event, workspace, *, child=False):
    """Only the fixed files from this explicitly authorized acceptance fixture."""
    args = event.get('tool_input') or {}
    if bool(event.get('agent_id')) != child:
        return False
    target = args.get('file_path')
    if not isinstance(target, str):
        return False
    target = os.path.normcase(os.path.abspath(target))
    expected = lambda name: os.path.normcase(str(workspace / name))
    if event.get('tool_name') == 'Read':
        return target == expected('fixture.txt') and not set(args) - {'file_path', 'offset', 'limit'}
    name = 'child.txt' if child else 'allowed.txt'
    content = 'ONLINE_ACCEPTANCE_CHILD\n' if child else 'ONLINE_ACCEPTANCE_ALLOWED\n'
    return (event.get('tool_name') == 'Write' and target == expected(name)
            and args.get('content') == content and set(args) == {'file_path', 'content'})


def approve_invocation(db, adapter, agent, prompt, *, policy=None, task_id=None,
                       permissions=None, model=None, effort=None):
    from agenthub.contracts import InvocationSnapshot
    from agenthub_kernel.db import TaskRow
    from agenthub_kernel.service import PolicyService
    with db.session() as s:
        svc = PolicyService(s, policy) if policy else PolicyService(s)
        root = s.get(TaskRow, task_id) if task_id else svc.create_task(
            agent_id=agent, goal='User-authorized isolated online acceptance')
        snap = InvocationSnapshot(agent_id=agent, task_input=prompt, parent_task_id=None,
            depth=0, permissions=permissions or {}, policy_version=root.policy_version,
            adapter_version=adapter.binding_identity(), execution_mode='strict',
            model=model, reasoning_effort=effort)
        inv = svc.create_invocation(task_id=root.id, snapshot=snap)
        appr = svc.create_approval(invocation_id=inv.id, user_id='online-test-user')
        svc.approve(appr.id)
        return inv.id, root.id


def run_summary(db, adapter, iid):
    from sqlalchemy import select
    from agenthub_kernel.db import ApprovalRow, InvocationRow, UsageLedgerRow
    with db.session() as s:
        inv = s.get(InvocationRow, iid)
        appr = s.scalar(select(ApprovalRow).where(ApprovalRow.invocation_id == iid))
        u = s.get(UsageLedgerRow, iid)
        result = {'id': iid, 'status': inv.status, 'approval': appr.status,
            'started': inv.started_at is not None,
            'usage': None if not u else {k: getattr(u, k) for k in (
                'input_total', 'output_total', 'completeness', 'notes', 'model')}}
    record = adapter.store.get(iid)
    payload = (record or {}).get('payload') or {}
    # Full fixed-fixture output remains ignored. Stdout has no credential/config data.
    result.update({k: payload.get(k) for k in ('exit_code', 'subtype', 'is_error',
        'cost_usd_estimate', 'elapsed_seconds', 'num_turns', 'models') if k in payload})
    return result


def claude_case_passed(case, *, child):
    """Tool cards retain the decision; delivery records one-shot consumption."""
    delivered = lambda c: c['status'] == 'approved' and c['delivery'] == 'sent'
    passed = (case['status'] == 'succeeded' and case['approval'] == 'consumed'
        and not case['responder_errors'] and case['expected_file_exact'] and case['denied_file_absent']
        and any(c['tool'] == 'Read' and delivered(c) for c in case['cards'])
        and any(c['tool'] == 'Write' and delivered(c) for c in case['cards']))
    if child:
        return bool(passed and case['native'] == [{'state': 'finished', 'identified': True, 'depth': 1}]
            and case['quota'] == {'launched': 1, 'concurrent': 0}
            and sum(c['tool'] == 'Agent' and delivered(c) for c in case['cards']) == 1
            and all(c['child'] for c in case['cards'] if c['tool'] != 'Agent'))
    return bool(passed and not case['native'] and any(c['tool'] == 'Write'
        and c['status'] == 'declined' and c['delivery'] == 'sent' for c in case['cards']))


def claude_check(run, settings, directory, *, child_only=False):
    from sqlalchemy import select
    from agenthub.contracts import Policy
    from agenthub_adapters.claude_code import ClaudeCodeLauncher, ProcessRunner
    from agenthub_kernel.adapters import _build_claude_code
    from agenthub_kernel.db import Database, NativeAgentRow, ToolApprovalRow, TreeQuotaRow
    from agenthub_kernel.tool_approvals import card_hash, decide
    from agenthub_kernel.worker import Worker

    item = settings.get('agents', {}).get('claude-code', {})
    if not item.get('enabled'):
        raise ValueError('configured_claude_disabled')
    previous = json.loads((run / 'claude-result.json').read_text(encoding='utf-8')) if child_only else None
    if child_only:
        assert len(previous['cases']) == 1 and not (run / 'claude-child-started.json').exists()
        scratch = Path(previous['scratch'])
        assert scratch.is_relative_to(Path('E:/plug/.online-strict-082'))
    else:
        scratch = Path('E:/plug/.online-strict-082') / uuid.uuid4().hex
        scratch.mkdir(parents=True)
    adapter = _build_claude_code({'adopted': item, 'config_dir': item['config_dir'],
        'proxy': settings.get('proxy') or '', 'ca_bundle': settings.get('ca_bundle') or '',
        'timeout_seconds': 240, 'workdir_root': str(scratch / 'runs'),
        'store_path': str(run / 'claude-executions.db')})
    assert type(adapter.launcher) is ClaudeCodeLauncher and type(adapter.runner) is ProcessRunner
    admission = adapter.execution_admission()
    if not admission.allowed or not adapter.identity_matches()[0]:
        raise ValueError('real_strict_admission_refused')
    dbpath = run / 'claude-hub.db'
    if dbpath.exists() != child_only:
        raise ValueError('claude acceptance is one-shot; existing database retained')
    db = Database('sqlite:///' + dbpath.as_posix())
    db.create_all()
    report = previous or {'real_provider': True, 'strict': True, 'scratch': str(scratch), 'cases': []}
    if child_only:
        # A driver-only verdict correction, never a replay of the finished CLI.
        case = report['cases'][0]
        assert (run / 'claude-files-started.json').is_file()
        live = run_summary(db, adapter, case['id'])
        assert live['status'] == case['status'] == 'succeeded' and live['approval'] == 'consumed'
        workspace = scratch / 'claude-files'
        assert (workspace / 'allowed.txt').read_text(encoding='utf-8') == 'ONLINE_ACCEPTANCE_ALLOWED\n'
        assert not (workspace / 'denied.txt').exists()
        with db.session() as s:
            cards = list(s.scalars(select(ToolApprovalRow)))
            assert len(cards) == 3 and all(r.invocation_id == case['id'] for r in cards)
            assert not list(s.scalars(select(NativeAgentRow)))
            assert all(q.concurrent == 0 for q in s.scalars(select(TreeQuotaRow)))
            case['cards'] = [{'tool': r.details['native_hook']['tool_name'],
                'child': bool(r.details['native_hook'].get('agent_id')), 'status': r.status,
                'delivery': r.delivery} for r in cards]
            case['native'] = []
        case['expected_file_exact'] = True
        case['denied_file_absent'] = True
        assert claude_case_passed(case, child=False)
        case['passed'] = True
        report.pop('stopped_after_failure', None)
        write(run / 'claude-result.json', report)
    for child in ((True,) if child_only else (False, True)):
        label = 'claude-child' if child else 'claude-files'
        workspace = scratch / label
        workspace.mkdir()
        (workspace / 'fixture.txt').write_text('ONLINE_ACCEPTANCE_FIXTURE\n', encoding='utf-8')
        if child:
            child_prompt = (f'Read exactly {workspace / "fixture.txt"} once, then Write exactly '
                f'{workspace / "child.txt"} with content ONLINE_ACCEPTANCE_CHILD followed by one newline. '
                'Use Read and Write only, no other files or tools. Stop immediately on denial; do not retry. '
                'Finish with CHILD_DONE.')
            prompt = ('This is an isolated online acceptance. Invoke Agent exactly once with subagent_type '
                'general-purpose, description "online acceptance child", and the exact prompt below. '
                'Do not pass model, background, resume, fork or isolation parameters. '
                'Do not use file tools yourself. Wait for the child and finish with PARENT_DONE. '
                'Stop on any denial without retrying. Child prompt:\n' + child_prompt)
        else:
            prompt = (f'This is an isolated online acceptance. In order: Read {workspace / "fixture.txt"} once; '
                f'Write {workspace / "allowed.txt"} with content ONLINE_ACCEPTANCE_ALLOWED followed by one newline; '
                f'then attempt Write {workspace / "denied.txt"} with content ONLINE_ACCEPTANCE_DENIED followed by one newline. '
                'The final Write will be denied as a test. Do not retry or bypass it. Use Read and Write only, '
                'no agents, searches or other files. Finish with FILE_TEST_DONE.')
        policy = Policy(collaboration_enabled=child, max_subagents_total=1,
            max_subagents_concurrent=1 if child else 0, max_depth=1 if child else 0,
            max_agent_invocations=2, run_time_limit_seconds=240)
        permissions = {'workspace': str(workspace), 'workspace_access': 'workspace-write',
            'native_subagents': 'approved' if child else 'disabled'}
        iid, rootid = approve_invocation(db, adapter, 'claude-code', prompt, policy=policy,
            permissions=permissions, model='sonnet', effort='low')
        once(run, label, {'authorized_by': 'user 2026-11-03 在线模型测试做一下',
            'invocation': iid, 'scope': permissions, 'prompt_sha256': hashlib.sha256(prompt.encode()).hexdigest(),
            'timeout_seconds': 240, 'production_client_estimate_limit_usd': 1.0})
        stopped = threading.Event()
        errors = []
        agent_approved = False
        def answer():
            nonlocal agent_approved
            while not stopped.wait(.05):
                try:
                    with db.session() as s:
                        rows = list(s.scalars(select(ToolApprovalRow).where(
                            ToolApprovalRow.invocation_id == iid, ToolApprovalRow.status == 'pending')))
                        for row in rows:
                            ev = (row.details or {}).get('native_hook') or {}
                            args = ev.get('tool_input') or {}
                            allow = allow_file(ev, workspace, child=child)
                            if child and ev.get('tool_name') == 'Agent':
                                allow = (not agent_approved and not ev.get('agent_id')
                                    and args == {'prompt': child_prompt, 'description': 'online acceptance child',
                                                 'subagent_type': 'general-purpose'})
                                if allow:
                                    agent_approved = True
                            decide(s, row.id, decision='approved' if allow else 'declined',
                                actor='online-test-user', presented_hash=card_hash(row))
                except Exception as exc:
                    errors.append(type(exc).__name__)
                    stopped.set()  # fail closed; pending cards expire, never blindly allow
        responder = threading.Thread(target=answer, daemon=True)
        responder.start()
        started = time.monotonic()
        try:
            Worker(db, adapter, tool_approval_ttl=20).run_one(iid)
        finally:
            stopped.set()
            responder.join(3)
        case = run_summary(db, adapter, iid)
        case.update(label=label, elapsed_seconds=round(time.monotonic()-started, 2), responder_errors=errors)
        with db.session() as s:
            cards = list(s.scalars(select(ToolApprovalRow).where(ToolApprovalRow.invocation_id == iid)))
            case['cards'] = [{'tool': r.details['native_hook']['tool_name'],
                'child': bool(r.details['native_hook'].get('agent_id')), 'status': r.status,
                'delivery': r.delivery} for r in cards]
            natives = list(s.scalars(select(NativeAgentRow).where(NativeAgentRow.invocation_id == iid)))
            case['native'] = [{'state': n.state, 'identified': bool(n.provider_agent_id), 'depth': n.depth} for n in natives]
            quota = s.get(TreeQuotaRow, rootid)
            case['quota'] = {'launched': quota.launched_total, 'concurrent': quota.concurrent}
        expected = workspace / ('child.txt' if child else 'allowed.txt')
        content = 'ONLINE_ACCEPTANCE_CHILD\n' if child else 'ONLINE_ACCEPTANCE_ALLOWED\n'
        case['expected_file_exact'] = expected.is_file() and expected.read_text(encoding='utf-8') == content
        case['denied_file_absent'] = not (workspace / 'denied.txt').exists()
        passed = claude_case_passed(case, child=child)
        case['passed'] = bool(passed)
        report['cases'].append(case)
        write(run / 'claude-result.json', report)
        print(json.dumps(case, ensure_ascii=False), flush=True)
        if not passed:
            report['stopped_after_failure'] = True
            break
    report['passed'] = len(report['cases']) == 2 and all(c['passed'] for c in report['cases'])
    write(run / 'claude-result.json', report)
    db.engine.dispose()
    return report


def cached_public_cases(source):
    """Read-only, known public acceptance fixtures; never resume the old ledger."""
    from agenthub_kernel.papers.guidance import study_result
    from agenthub_kernel.papers.validate import FormatError, parse_json, screen
    cases = []
    with sqlite3.connect((source / 'hub.db').as_uri() + '?mode=ro', uri=True) as hub, \
            sqlite3.connect((source / 'deepseek-executions.db').as_uri() + '?mode=ro', uri=True) as executions:
        for label, iid in (('screen', 'inv-e2f7bc7f58c5'), ('study', 'inv-8ce25b88af45')):
            snap = json.loads(hub.execute('select snapshot from invocations where id=?', (iid,)).fetchone()[0])
            prompt = snap['task_input']
            assert prompt.startswith('AGENTHUB-TEMPLATE: research.' + label + '.v1\n')
            data = json.loads(prompt.split('<<<DATA (untrusted material)\n', 1)[1].split('\nDATA>>>', 1)[0])
            if label == 'screen':
                assert len(data['candidates']) == 20 and data['topic'].startswith('Retrieval augmented generation evaluation:')
                ids = [p['paper_id'] for p in data['candidates']]
                validator = lambda out, ids=ids: screen(out, ids)
            else:
                assert data['paper']['title'] == 'Ragas: Automated Evaluation of Retrieval Augmented Generation'
                assert len(data['chunks']) == 19 and data['coverage']['total'] == 25
                chunks = [SimpleNamespace(id=c['chunk_id'], text=c['text'], page=c['pdf_page']) for c in data['chunks']]
                validator = lambda out, chunks=chunks: study_result(out, chunks)
            old_payload = json.loads(executions.execute('select payload from deepseek_executions where invocation_id=?', (iid,)).fetchone()[0])
            old_output = old_payload.get('final_message') or old_payload.get('reply')
            try:
                validator(parse_json(old_output))
            except FormatError as exc:
                error = str(exc)
            else:
                raise ValueError('expected failed public regression no longer reproduces')
            # New diagnostic, original input and production validators. No old output
            # excerpt: keep this focused regeneration below its approved reservation.
            revised = prompt + (f'\nRepair attempt 1 of 1. Previous outcome: format_invalid: {error[:200]}. '
                'First identify the cause and choose a different approach; then verify the repair. '
                'Do not repeat operations already completed. If no viable repair exists, explain why.')
            cases.append({'label': label, 'source_invocation': iid, 'prompt': revised, 'data': data,
                'validator': validator, 'old_validation_error': error,
                'source_prompt_sha256': hashlib.sha256(prompt.encode()).hexdigest()})
    return cases


def paper_check(run, settings, directory, source):
    from sqlalchemy import select
    from agenthub.budget import estimate_visible_tokens
    from agenthub.contracts import Policy
    from agenthub_adapters.deepseek import DeepSeekAdapter
    from agenthub_adapters.store import ExecutionStore
    from agenthub_kernel.db import Database, UsageLedgerRow
    from agenthub_kernel.papers.parse import quote_in
    from agenthub_kernel.papers.validate import FormatError, parse_json
    from agenthub_kernel.worker import Worker
    item = settings.get('agents', {}).get('deepseek', {})
    assert item.get('enabled') and urlsplit(item['base_url']).hostname == 'api.deepseek.com'
    cases = cached_public_cases(source)
    dbpath = run / 'paper-hub.db'
    if dbpath.exists():
        raise ValueError('paper acceptance is one-shot; old state must be retained')
    adapter = DeepSeekAdapter(item['base_url'], item['model'],
        credentials_path=str(directory / 'credentials/deepseek.json'),
        store=ExecutionStore(str(run / 'paper-executions.db'), table='deepseek_executions'),
        proxy=settings.get('proxy') or '', ca_bundle=settings.get('ca_bundle') or '',
        timeout_seconds=120, repo_root=str(ROOT), stub='')
    assert not adapter.stub and adapter._transport is None
    db = Database('sqlite:///' + dbpath.as_posix())
    db.create_all()
    policy = Policy(max_agent_invocations=3, max_depth=0, max_subagents_concurrent=0,
        run_time_limit_seconds=120, token_budget_total=20000, token_budget_per_call=8000)
    report = {'real_provider': True, 'endpoint_host': 'api.deepseek.com', 'model': item['model'],
        'test_kind': 'targeted cached public input regeneration, not whole-pipeline acceptance',
        'limits': {'new_total_tokens': 20000, 'per_call_reservation': 8000, 'max_calls': 3},
        'old_52866_token_ledger_unchanged': True, 'cases': []}
    task_id = None
    for case in cases:
        label, prompt = case['label'], case['prompt']
        assert estimate_visible_tokens(prompt) <= 8000
        iid, task_id = approve_invocation(db, adapter, 'deepseek', prompt, policy=policy,
            task_id=task_id, model=item['model'], effort='none')
        once(run, 'paper-' + label, {'authorized_by': 'user 2026-11-03 在线模型测试做一下; 继续',
            'invocation': iid, 'source_invocation': case['source_invocation'],
            'source_prompt_sha256': case['source_prompt_sha256'],
            'prompt_sha256': hashlib.sha256(prompt.encode()).hexdigest(),
            'estimate_visible_tokens': estimate_visible_tokens(prompt),
            'no_private_project_or_chat_material': True, 'prior_output_excerpt_sent': False,
            'old_validation_error': case['old_validation_error']})
        start = time.monotonic()
        outcome = Worker(db, adapter).run_one(iid)
        row = run_summary(db, adapter, iid)
        row.update(label=label, elapsed_seconds=round(time.monotonic()-start, 2),
            old_validation_error=case['old_validation_error'])
        last = adapter.last_result(iid)
        row['validation_passed'] = False
        parsed = None
        if row['status'] == 'succeeded' and last:
            try:
                parsed = parse_json(last['final_message'])
                validated = case['validator'](parsed)
                row['validation_passed'] = True
                if label == 'screen':
                    row['expected_candidates'] = len(case['data']['candidates'])
                    row['returned_candidates'] = len(validated)
                else:
                    chunks = {c['chunk_id']: c['text'] for c in case['data']['chunks']}
                    refs = [r for u in parsed['units'] for r in u.get('sources', [])]
                    row.update(units=len(parsed['units']), references=len(refs),
                        quotes_matched=sum(r.get('chunk_id') in chunks and quote_in(r.get('quote', ''), chunks[r['chunk_id']]) for r in refs),
                        math_state=parsed.get('math_state'))
            except FormatError as exc:
                row['validation_error'] = str(exc)
                if label == 'study' and isinstance(parsed, dict):
                    chunks = {c['chunk_id']: c['text'] for c in case['data']['chunks']}
                    refs = [r for u in parsed.get('units', []) for r in u.get('sources', [])]
                    row.update(references=len(refs), quotes_matched=sum(r.get('chunk_id') in chunks
                        and quote_in(r.get('quote', ''), chunks[r['chunk_id']]) for r in refs))
        if not row['started']:
            row['refusal_code'] = outcome.get('code')
        report['cases'].append(row)
        with db.session() as s:
            usage = list(s.scalars(select(UsageLedgerRow)))
            report['usage_complete'] = all(u.completeness == 'complete' for u in usage)
            report['input_total'] = sum(u.input_total or 0 for u in usage)
            report['output_total'] = sum(u.output_total or 0 for u in usage)
            report['observed_total_tokens'] = report['input_total'] + report['output_total']
        write(run / 'paper-result.json', report)
        print(json.dumps(row, ensure_ascii=False), flush=True)
        if row['status'] != 'succeeded' or not report['usage_complete'] or report['observed_total_tokens'] >= 20000:
            report['stopped_without_replay'] = True
            break
    report['passed'] = len(report['cases']) == 2 and all(r['validation_passed'] for r in report['cases'])
    if not report['passed']:
        report['remaining_tokens'] = 20000 - report.get('observed_total_tokens', 0)
        report['repair_not_launched'] = ('insufficient_remaining_for_8000_reservation'
            if report['remaining_tokens'] < 8000 else 'bounded_test_complete_no_automatic_replay')
    write(run / 'paper-result.json', report)
    db.engine.dispose()
    return report


def audit_existing(run, source):
    """Reproduce the final evidence summary without profile access or model calls."""
    from agenthub_kernel.papers.parse import quote_in
    from agenthub_kernel.papers.validate import parse_json
    claude = json.loads((run / 'claude-result.json').read_text(encoding='utf-8'))
    paper = json.loads((run / 'paper-result.json').read_text(encoding='utf-8'))
    summary = {'claude_passed': claude['passed'], 'claude_root_cli_launches': len(claude['cases']),
        'claude_native_children': sum(len(c['native']) for c in claude['cases']),
        'claude_cli_reported_cost_usd_estimate': sum(c['cost_usd_estimate'] for c in claude['cases']),
        'claude_cli_reported_input_tokens': sum(c['usage']['input_total'] for c in claude['cases']),
        'claude_cli_reported_output_tokens': sum(c['usage']['output_total'] for c in claude['cases']),
        'claude_child_usage_coverage_unverified': True, 'paper_passed': paper['passed'],
        'deepseek_input_total': paper['input_total'], 'deepseek_output_total': paper['output_total'],
        'deepseek_total': paper['observed_total_tokens'], 'deepseek_remaining': 20000-paper['observed_total_tokens'],
        'codex_online_not_run_known_strict_block': True, 'full_pipeline_online_pass': False}
    for name in ('claude', 'paper'):
        with sqlite3.connect((run / (name + '-hub.db')).as_uri() + '?mode=ro', uri=True) as conn:
            summary[name + '_invocation_statuses'] = dict(conn.execute(
                'select status,count(*) from invocations group by status').fetchall())
            summary[name + '_active_invocations'] = conn.execute(
                "select count(*) from invocations where status in ('starting','running','unknown')").fetchone()[0]
            summary[name + '_concurrent_held'] = conn.execute('select sum(concurrent) from tree_quota').fetchone()[0]
    study = cached_public_cases(source)[1]
    iid = next(c['id'] for c in paper['cases'] if c['label'] == 'study')
    with sqlite3.connect((run / 'paper-executions.db').as_uri() + '?mode=ro', uri=True) as conn:
        payload = json.loads(conn.execute('select payload from deepseek_executions where invocation_id=?', (iid,)).fetchone()[0])
    result = parse_json(payload.get('final_message') or payload.get('reply'))
    chunks = {c['chunk_id']: c['text'] for c in study['data']['chunks']}
    invalid = []
    for unit in result['units']:
        for ref in unit.get('sources', []):
            if ref['chunk_id'] in chunks and quote_in(ref['quote'], chunks[ref['chunk_id']]):
                continue
            invalid.append({'unit': unit['id'], 'chunk_id': ref['chunk_id'], 'quote': ref['quote'],
                'exact_in_other_supplied_chunks': [k for k, text in chunks.items() if quote_in(ref['quote'], text)]})
    write(run / 'quote-audit.json', {'validation_unchanged': True, 'invalid': invalid})
    summary['bad_quotes'] = len(invalid)
    summary['bad_quotes_exact_in_other_chunk'] = sum(bool(x['exact_in_other_supplied_chunks']) for x in invalid)
    write(run / 'online-summary.json', summary)
    return summary


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--approve-online-test', action='store_true', required=True)
    parser.add_argument('--phase', choices=['inspect', 'claude', 'claude-child', 'paper', 'audit'], required=True)
    parser.add_argument('--source-run', default=str(ROOT.parent / 'p6-online-080/var/online-080'))
    parser.add_argument('--run', required=True)
    args = parser.parse_args()
    run = Path(args.run).resolve()
    if not run.is_relative_to(ROOT / 'var'):
        parser.error('run must be inside this worktree var')
    run.mkdir(parents=True, exist_ok=True)
    if args.phase == 'audit':
        print(json.dumps(audit_existing(run, Path(args.source_run).resolve()), ensure_ascii=False))
        return 0
    settings, directory = profile()
    os.environ.pop('AGENT_HUB_DESKTOP', None)
    if args.phase == 'inspect':
        report = inspect(settings, directory)
        write(run / 'environment.json', report)
        print(json.dumps(report, ensure_ascii=False))
        return 0
    if args.phase == 'paper':
        report = paper_check(run, settings, directory, Path(args.source_run).resolve())
    else:
        report = claude_check(run, settings, directory, child_only=args.phase == 'claude-child')
    return 0 if report.get('passed') else 2


if __name__ == '__main__':
    raise SystemExit(main())
