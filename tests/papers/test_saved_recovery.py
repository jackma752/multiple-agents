"""No provider: restore original paid replies; preserve grants, budgets and usage."""
import copy
import json

import pytest
from sqlalchemy import select

from agenthub_kernel.db import ApprovalRow, ChatMessageRow, InvocationRow, StageRunRow, UsageLedgerRow
from agenthub_kernel.papers.db import PaperChunkRow, PaperStepRow, ResearchTaskRow
from tests.papers.test_guidance import body


def strand(hub, monkeypatch, *, bad_quote=False):
    from tools.demo import guidance_stub
    original = guidance_stub.respond
    observed, material = [], []
    def respond(template, data):
        observed.append(template)
        out = original(template, data)
        if template == 'research.study.v2':
            material.extend(copy.deepcopy(data['chunks']))
        if template == 'research.study-patch.v1':
            c = material[0]
            out['replacements'][0]['sources'] = [{'chunk_id':c['chunk_id'], 'quote':c['text'][:100]} for _ in range(20)]
            if bad_quote:
                out['replacements'][0]['sources'][0]['quote'] = 'This authored missing quote must be corrected only in its original chunk.'
        if template == 'research.source-select.v1':
            return {'selections':[]}
        return out
    monkeypatch.setattr(guidance_stub, 'respond', respond)
    hub.prove()
    tid = hub.api('POST', '/api/papers', json=body(hub, 'study')).json()['task_id']
    view = hub.run(tid)
    assert view['status'] == 'blocked', view
    offers = hub.api('GET', f'/api/papers/{tid}/recovery')
    assert offers.status_code == 200, offers.text
    assert len(offers.json()['candidates']) == 1, offers.text
    offer = offers.json()['candidates'][0]
    return tid, offer, material, observed


def request(offer, **extra):
    return {k:offer[k] for k in ('step_id','invocation_id','fingerprint')} | {'client_request_id':'recovery-1','corrections':[]} | extra


def snapshot(hub, tid):
    with hub.client.app.state.db.session() as s:
        task = s.get(ResearchTaskRow, tid)
        stage = s.get(StageRunRow, task.stage_id)
        return {
            'stage':(stage.status, stage.rules, stage.rules_hash, stage.checkpoint),
            'invocations':[(i.id,i.status,i.payload_hash,i.snapshot) for i in s.scalars(select(InvocationRow).order_by(InvocationRow.id))],
            'approvals':[(a.id,a.status,a.payload_hash) for a in s.scalars(select(ApprovalRow).order_by(ApprovalRow.id))],
            'usage':[{c.name:getattr(u,c.name) for c in u.__table__.columns} for u in s.scalars(select(UsageLedgerRow).order_by(UsageLedgerRow.invocation_id))]}


def test_restore_into_original_task_without_calls_then_resume_at_review(hub, monkeypatch):
    tid, offer, material, observed = strand(hub, monkeypatch)
    assert offer['valid']
    before, calls, old_view = snapshot(hub, tid), hub.model_calls(), hub.view(tid)
    r = hub.api('POST', f'/api/papers/{tid}/recovery', json=request(offer))
    assert r.status_code == 200, r.text
    assert r.json()['model_calls'] == 0 and not r.json()['math_reviewed']
    assert snapshot(hub, tid) == before and hub.model_calls() == calls
    view = hub.view(tid)
    assert view['status'] == 'blocked' and view['stage']['stage_id'] == old_view['stage']['stage_id']
    study = next(iter(view['outcome']['studies'].values()))
    assert study['units'] and not study['finished'] and study['review_status'] == 'needs_review'
    with hub.client.app.state.db.session() as s:
        step = s.get(PaperStepRow, offer['step_id'])
        assert step.status == 'succeeded' and step.invocation_ids == next(x['invocation_ids'] for x in old_view['steps'] if x['name']=='study_patch')
        receipt = s.get(PaperStepRow, r.json()['receipt_id'])
        assert receipt.result['before']['status'] != 'succeeded' and receipt.invocation_ids == []
    duplicate = hub.api('POST', f'/api/papers/{tid}/recovery', json=request(offer))
    assert duplicate.status_code == 200 and duplicate.json()['duplicate']
    assert duplicate.json()['receipt_id'] == r.json()['receipt_id']
    assert hub.model_calls() == calls
    count = len(observed)
    assert hub.api('POST', f'/api/papers/{tid}/reauthorize', json={}).status_code == 200
    view = hub.run(tid)
    assert view['status'] == 'completed', view
    assert observed[count] == 'research.study-review.v1'
    assert observed.count('research.study.v2') == 1 and observed.count('research.study-patch.v1') == 1


def test_correct_only_failed_quote_using_original_approved_material(hub, monkeypatch):
    tid, offer, material, _ = strand(hub, monkeypatch, bad_quote=True)
    assert not offer['valid'] and len(offer['failed_sources']) == 1
    slot = offer['failed_sources'][0]
    fix = {k:slot[k] for k in ('unit_id','source_index','chunk_id')} | {'quote':material[0]['text'][:100]}
    calls, before = hub.model_calls(), snapshot(hub, tid)
    r = hub.api('POST', f'/api/papers/{tid}/recovery', json=request(offer, corrections=[fix]))
    assert r.status_code == 200, r.text
    assert hub.model_calls() == calls and snapshot(hub, tid) == before
    with hub.client.app.state.db.session() as s:
        row = s.get(PaperStepRow, offer['step_id'])
        assert len(row.result['units'][8]['sources']) == 1
        receipt = s.get(PaperStepRow, r.json()['receipt_id'])
        assert receipt.result['corrections'][0]['old_source']['quote'].startswith('This authored missing quote')


@pytest.mark.parametrize('bad', ['text','cross_chunk','absent_quote','valid_slot','repeated','foreign_inv','stale','extra_field'])
def test_invalid_recovery_cannot_mutate_checkpoint_or_launch(hub, monkeypatch, bad):
    tid, offer, material, _ = strand(hub, monkeypatch, bad_quote=True)
    slot = offer['failed_sources'][0]
    fix = {k:slot[k] for k in ('unit_id','source_index','chunk_id')} | {'quote':material[0]['text'][:100]}
    payload = request(offer, corrections=[fix])
    if bad == 'text':payload['corrections'][0]['text'] = 'injected teaching'
    if bad == 'extra_field':payload['units'] = []
    if bad == 'cross_chunk':fix['chunk_id'] = 'unapproved-chunk'
    if bad == 'absent_quote':fix['quote'] = 'A different absent source quotation with sufficient length.'
    if bad == 'valid_slot':fix['source_index'] = 1
    if bad == 'repeated':payload['corrections'] = [fix,fix]
    if bad == 'foreign_inv':payload['invocation_id'] = 'inv-unrelated'
    if bad == 'stale':payload['fingerprint'] = '0'*64
    calls, before = hub.model_calls(), snapshot(hub, tid)
    r = hub.api('POST', f'/api/papers/{tid}/recovery', json=payload)
    assert r.status_code in (409,422), r.text
    assert hub.model_calls() == calls and snapshot(hub, tid) == before
    with hub.client.app.state.db.session() as s:
        assert s.get(PaperStepRow, offer['step_id']).status != 'succeeded'
        assert not list(s.scalars(select(PaperStepRow).where(PaperStepRow.name=='recover_saved')))


@pytest.mark.parametrize('mutation', ['unknown','running','queued','active_stage','snapshot','material','material_hash','base','result'])
def test_changed_or_inflight_inputs_are_refused_without_relaunch(hub, monkeypatch, mutation):
    tid, offer, material, _ = strand(hub, monkeypatch)
    with hub.client.app.state.db.session() as s:
        task = s.get(ResearchTaskRow, tid)
        inv = s.get(InvocationRow, offer['invocation_id'])
        if mutation in ('unknown','running','queued'):inv.status = mutation
        if mutation == 'active_stage':s.get(StageRunRow, task.stage_id).status = 'active'
        if mutation == 'snapshot':inv.snapshot = {**inv.snapshot, 'task_input':inv.snapshot['task_input']+'tampered'}
        if mutation == 'material':
            chunk = s.get(PaperChunkRow, material[0]['chunk_id']);chunk.text += ' unauthorized material'
        if mutation == 'material_hash':s.get(PaperChunkRow, material[0]['chunk_id']).sha256 = '0'*64
        if mutation == 'base':
            base = s.scalar(select(PaperStepRow).where(PaperStepRow.task_id==tid,PaperStepRow.name=='study'))
            data = copy.deepcopy(base.result);data['units'][0]['text'] += ' changed';base.result = data
        if mutation == 'result':
            step = s.get(PaperStepRow, offer['step_id']);step.result = {**step.result,'changed_since_preview':True}
    before, calls = snapshot(hub, tid), hub.model_calls()
    r = hub.api('POST', f'/api/papers/{tid}/recovery', json=request(offer))
    assert r.status_code == 409, r.text
    assert snapshot(hub, tid) == before and hub.model_calls() == calls


def test_permissions_and_unresolved_references_do_not_write(hub, monkeypatch):
    tid, offer, _, _ = strand(hub, monkeypatch, bad_quote=True)
    calls = hub.model_calls()
    assert hub.client.get(f'/api/papers/{tid}/recovery').status_code == 401
    assert hub.client.get(f'/api/papers/{tid}/recovery',headers={'Authorization':'Bearer dev-agent-token'}).status_code == 403
    assert hub.client.post(f'/api/papers/{tid}/recovery',headers={'Authorization':'Bearer dev-agent-token'},json=request(offer)).status_code == 403
    r = hub.api('POST', f'/api/papers/{tid}/recovery', json=request(offer))
    assert r.status_code == 409 and r.json()['detail']['code'] == 'recovery_validation_failed'
    with hub.client.app.state.db.session() as s:
        s.get(ResearchTaskRow,tid).user_id = 'another-user'
    assert hub.api('GET',f'/api/papers/{tid}/recovery').status_code == 404
    assert hub.api('POST',f'/api/papers/{tid}/recovery',json=request(offer)).status_code == 404
    assert hub.model_calls() == calls


def test_concurrent_same_recovery_is_one_checkpoint_write(hub,monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    tid,offer,_,_ = strand(hub,monkeypatch)
    payload = request(offer)
    before,calls = snapshot(hub,tid),hub.model_calls()
    with ThreadPoolExecutor(max_workers=2) as pool:
        replies = list(pool.map(lambda _:hub.api('POST',f'/api/papers/{tid}/recovery',json=payload),range(2)))
    assert all(r.status_code==200 for r in replies),[r.text for r in replies]
    assert len({r.json()['receipt_id'] for r in replies})==1
    assert sum(r.json()['duplicate'] for r in replies)==1
    assert before==snapshot(hub,tid) and hub.model_calls()==calls
    conflict = hub.api('POST',f'/api/papers/{tid}/recovery',json={**payload,'fingerprint':'0'*64})
    assert conflict.status_code==409 and conflict.json()['detail']['code']=='recovery_idempotency_conflict'
