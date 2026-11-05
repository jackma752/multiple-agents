"""Offline incidents: too many references must not regenerate paid mathematics."""
import copy

import pytest
from sqlalchemy import select

from agenthub_kernel.db import StageGrantRow, UsageLedgerRow, InvocationRow, ApprovalRow, StageRunRow
from agenthub_kernel.service import PolicyService, QuotaError
from agenthub_kernel.papers.citations import ReferenceLimitError, apply_source_selection
from agenthub_kernel.papers.guidance import patch_result, study_result
from agenthub_kernel.papers.validate import FormatError
from tests.papers.test_guidance import body, complete, sample


def excessive_patch():
    chunk, raw = sample()
    study = study_result(raw, [chunk])
    unit = copy.deepcopy(study['units'][4])
    unit['sources'] = [{'chunk_id': chunk.id, 'quote': chunk.text[i:i+40]} for i in range(20)]
    patch = {'replacements': [unit], 'additions': [], 'math_state': 'complete', 'missing': []}
    return chunk, study, patch


def test_count_error_preserves_patch_and_names_the_actual_limit():
    chunk, study, patch = excessive_patch()
    with pytest.raises(ReferenceLimitError) as caught:
        patch_result(patch, study, [chunk], {'setting1'})
    exc = caught.value
    assert 'setting1: 20 > 12' in str(exc) and 'require locations' not in str(exc)
    assert exc.patch == patch
    selected = apply_source_selection({'selections': [{'unit_id': 'setting1', 'source_indices': list(range(12))}]},
                                      exc.candidate, exc.issues)
    checked = study_result(selected, [chunk])
    for before, after in zip(exc.candidate['units'], checked['units']):
        assert {k: v for k, v in before.items() if k != 'sources'} == {k: v for k, v in after.items() if k != 'sources'}
    assert len(checked['units'][4]['sources']) == 12 and len(patch['replacements'][0]['sources']) == 20


@pytest.mark.parametrize('indices', [[0]*12, [True], [-1], [20], list(range(13)), []])
def test_source_selection_cannot_bypass_reference_limit_or_scope(indices):
    c, s, p = excessive_patch()
    with pytest.raises(ReferenceLimitError) as caught:
        patch_result(p, s, [c], {'setting1'})
    with pytest.raises(FormatError):
        apply_source_selection({'selections': [{'unit_id': 'setting1', 'source_indices': indices}]},
                               caught.value.candidate, caught.value.issues)


def test_selection_cannot_edit_text_or_admit_unverifiable_quotes():
    c, s, p = excessive_patch()
    p['replacements'][0]['sources'][0]['quote'] = 'An authored absent quotation for an offline test.'
    with pytest.raises(ReferenceLimitError) as caught:
        patch_result(p, s, [c], {'setting1'})
    exc = caught.value
    with pytest.raises(FormatError):
        apply_source_selection({'selections': [{'unit_id': 'setting1', 'source_indices': [0], 'text': 'injected'}]},
                               exc.candidate, exc.issues)
    selected = apply_source_selection({'selections': [{'unit_id': 'setting1', 'source_indices': [0]}]},
                                      exc.candidate, exc.issues)
    with pytest.raises(FormatError):
        study_result(selected, [c])


@pytest.mark.parametrize('broken_selection', [False, True])
def test_over_limit_patch_uses_one_small_selection_and_never_full_regeneration(hub, monkeypatch, broken_selection):
    from tools.demo import guidance_stub

    original, observed, source = guidance_stub.respond, [], []
    def response(template, data):
        observed.append((template, copy.deepcopy(data)))
        out = original(template, data)
        if template == 'research.study.v2':
            source.append(copy.deepcopy(data['chunks'][0]))
        if template == 'research.study-patch.v1':
            c = source[0]
            out['replacements'][0]['sources'] = [{'chunk_id': c['chunk_id'], 'quote': c['text'][i:i+60]}
                                                 for i in range(20)]
        if template == 'research.source-select.v1' and broken_selection:
            return {'selections': []}
        return out
    monkeypatch.setattr(guidance_stub, 'respond', response)
    hub.prove()
    r = hub.api('POST', '/api/papers', json=body(hub, 'study'))
    assert r.status_code == 200, r.text
    tid = r.json()['task_id']
    view = hub.run(tid)
    names = [t for t, _ in observed]
    assert names.count('research.study.v2') == 1 and names.count('research.study-patch.v1') == 1
    assert names.count('research.source-select.v1') == 1
    selection = next(d for t, d in observed if t == 'research.source-select.v1')
    assert 'chunks' not in selection and len(selection['units']) == 1
    if broken_selection:
        assert view['status'] == 'blocked' and view['stage']['summary']['reason'] == 'paper_repair_review'
        before = hub.model_calls()
        hub.client.app.state.autopilot.tick()
        assert hub.model_calls() == before
    else:
        assert view['status'] == 'completed' and view['quality_status'] == 'passed'
        assert '修复后' in next(iter(view['outcome']['studies'].values()))['units'][8]['text']


def test_high_reported_output_stops_before_next_model_creation(hub):
    hub.prove()
    tid = hub.api('POST', '/api/papers', json=body(hub, 'study')).json()['task_id']
    view = hub.run(tid, until=lambda v: any(st['name'] == 'study' and st['invocation_ids'] for st in v['steps']))
    inv = next(st['invocation_ids'][-1] for st in view['steps'] if st['name'] == 'study')
    with hub.client.app.state.db.session() as s:
        ledger = s.get(UsageLedgerRow, inv)
        ledger.output_total, ledger.reasoning = 70000, 60000
    before = hub.model_calls()
    view = hub.run(tid)
    assert view['status'] == 'blocked' and view['stage']['summary']['reason'] == 'paper_output_review'
    assert hub.model_calls() == before
    assert not any(st['name'] == 'study_review' and st['invocation_ids'] for st in view['steps'])
    assert view['usage']['reasoning'] == 60000 and view['usage']['output'] >= 70000
    assert '包含思考' in hub.api('GET', '/papers').text
    # Only an explicit user action can create the next stage; merely polling cannot.
    stage = view['stage']['stage_id']
    for _ in range(2):
        hub.client.app.state.autopilot.tick()
    assert hub.model_calls() == before
    assert hub.api('POST', f'/api/papers/{tid}/reauthorize', json={}).status_code == 200
    assert hub.view(tid)['stage']['stage_id'] != stage and hub.model_calls() == before


def test_paper_snapshot_binds_32k_and_old_unbounded_approval_cannot_start(hub):
    from agenthub.contracts import InvocationSnapshot
    from agenthub_kernel.autopilot import _event
    hub.prove()
    tid = hub.api('POST', '/api/papers', json=body(hub, 'study')).json()['task_id']
    view = hub.view(tid)
    created = hub.client.app.state.autopilot._advance_one(view['stage']['stage_id'])
    assert len(created) == 1
    before = hub.model_calls()
    with hub.client.app.state.db.session() as s:
        inv = s.get(InvocationRow, created[0])
        assert inv.snapshot['resources']['max_output_tokens'] == 32768
        approval = s.scalar(select(ApprovalRow).where(ApprovalRow.invocation_id == inv.id))
        # Reproduce an internally consistent legacy approval: the consume gate
        # still rejects it against today's paper output limit before launch.
        inv.snapshot = {**inv.snapshot, 'resources': {}}
        inv.payload_hash = InvocationSnapshot(**inv.snapshot).payload_hash()
        approval.payload_hash = inv.payload_hash
        with pytest.raises(QuotaError) as caught:
            PolicyService(s).consume(inv.id)
        assert caught.value.code == 'stage_output_limit_mismatch'
    assert hub.model_calls() == before
