"""Bounded field completion must not buy the already generated study again."""
import copy

import pytest
from sqlalchemy import select

from agenthub_kernel.db import StageRunRow
from agenthub_kernel.papers.db import PaperStepRow, ResearchTaskRow
from agenthub_kernel.papers.citations import TeachingFieldError, apply_reason_patch
from agenthub_kernel.papers.guidance import study_result
from agenthub_kernel.papers.validate import FormatError
from tests.papers.test_guidance import sample, body


def test_reason_completion_changes_only_named_reason_and_still_validates_all_sources():
    c, raw = sample()
    raw['units'][5].pop('reason')
    before = copy.deepcopy(raw)
    with pytest.raises(TeachingFieldError) as caught:study_result(raw,[c])
    assert caught.value.issues == [{'unit_id':'assumption1','field':'reason'}]
    fixed = apply_reason_patch({'reasons':[{'unit_id':'assumption1','reason':'Needed to control the geometric tail.'}]},raw,caught.value.issues)
    assert raw == before and fixed['units'][:5] == raw['units'][:5] and fixed['units'][6:] == raw['units'][6:]
    assert {k:v for k,v in fixed['units'][5].items() if k!='reason'} == raw['units'][5]
    study_result(fixed,[c])
    fixed['units'][0]['sources'][0]['quote'] = 'An authored absent citation should still fail.'
    with pytest.raises(FormatError):study_result(fixed,[c])


@pytest.mark.parametrize('rows', [[],[{'unit_id':'step1','reason':'wrong scope'}],
    [{'unit_id':'assumption1','reason':'ok','text':'changed'}],
    [{'unit_id':'assumption1','reason':'a'},{'unit_id':'assumption1','reason':'b'}]])
def test_reason_completion_rejects_missing_extra_or_modified_fields(rows):
    _, raw = sample()
    with pytest.raises(FormatError):apply_reason_patch({'reasons':rows},raw,[{'unit_id':'assumption1','field':'reason'}])


@pytest.mark.parametrize('broken', [False,True])
def test_missing_reason_uses_a_field_call_not_full_generation(hub,monkeypatch,broken):
    from tools.demo import guidance_stub
    original = guidance_stub.respond
    calls, prompts = [], []
    def respond(template,data):
        calls.append(template)
        out = original(template,data)
        if template == 'research.study.v2':out['units'][5].pop('reason')
        if template == 'research.reason-repair.v1':
            prompts.append(copy.deepcopy(data))
            if broken:return {'reasons':[]}
        return out
    monkeypatch.setattr(guidance_stub,'respond',respond)
    hub.prove()
    tid = hub.api('POST','/api/papers',json=body(hub,'study')).json()['task_id']
    view = hub.run(tid)
    assert calls.count('research.study.v2') == 1 and calls.count('research.reason-repair.v1') == 1
    assert [u['id'] for u in prompts[0]['units']] == ['assumption1']
    if broken:
        assert view['status'] == 'blocked' and view['stage']['summary']['reason'] == 'paper_repair_review'
        count = hub.model_calls();hub.client.app.state.autopilot.tick();assert hub.model_calls() == count
    else:assert view['status'] == 'completed'


def test_existing_old_continuation_recovers_paid_missing_reason_reply(hub,monkeypatch):
    from tools.demo import guidance_stub
    original = guidance_stub.respond
    calls, hold = [], [True]
    def respond(template,data):
        calls.append(template)
        out = original(template,data)
        if template == 'research.study.v2':out['units'][5].pop('reason')
        if template == 'research.reason-repair.v1' and hold[0]:return {'reasons':[]}
        return out
    monkeypatch.setattr(guidance_stub,'respond',respond)
    hub.prove()
    old = hub.api('POST','/api/papers',json=body(hub,'study')).json()['task_id']
    view = hub.run(old)
    assert view['status'] == 'blocked'
    # Reproduce the older runtime's completed-with-failed-study checkpoint. Its
    # paid original reply is intact, but no specialized field seed was saved.
    with hub.client.app.state.db.session() as s:
        task = s.get(ResearchTaskRow,old);task.phase = 'done'
        s.get(StageRunRow,task.stage_id).status = 'completed'
        study = s.scalar(select(PaperStepRow).where(PaperStepRow.task_id==old,PaperStepRow.name=='study'))
        study.status,study.result = 'failed',None
        task.outcome = {**task.outcome,'studies':{study.target:{'units':[],'finished':True}}}
    r = hub.api('POST',f'/api/papers/{old}/continue',json={})
    assert r.status_code == 200,r.text
    tid = r.json()['task_id']
    with hub.client.app.state.db.session() as s:
        seed = s.scalar(select(PaperStepRow).where(PaperStepRow.task_id==tid,PaperStepRow.name=='study',PaperStepRow.key.startswith('v')))
        assert '_reason_repair' in seed.result and seed.invocation_ids == []
        # Existing continuations made by an older installation lack this row.
        # The current phase upgrades them lazily without replaying the writer.
        s.delete(seed)
    count = len(calls);hold[0] = False
    view = hub.run(tid)
    assert view['status'] == 'completed',view
    assert calls[count] == 'research.reason-repair.v1'
    assert calls.count('research.study.v2') == 1


def test_patch_missing_reason_reuses_the_exact_patch_envelope(hub,monkeypatch):
    from tools.demo import guidance_stub
    original = guidance_stub.respond
    calls = []
    def respond(template,data):
        calls.append(template)
        out = original(template,data)
        if template == 'research.study-patch.v1':out['replacements'][0].pop('reason')
        return out
    monkeypatch.setattr(guidance_stub,'respond',respond)
    hub.prove()
    tid = hub.api('POST','/api/papers',json=body(hub,'study')).json()['task_id']
    view = hub.run(tid)
    assert view['status']=='completed',view
    assert calls.count('research.study.v2')==1 and calls.count('research.study-patch.v1')==1
    assert calls.count('research.reason-repair.v1')==1
