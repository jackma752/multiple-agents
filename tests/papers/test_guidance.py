"""Authored mathematical fixtures and offline gated lifecycle; no real model quality claim."""
import copy
import json
from fractions import Fraction
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from agenthub.papers import ResearchRequest, estimate_calls
from agenthub_kernel.papers.guidance import pick, study_result, patch_result, ideas_result
from agenthub_kernel.papers.validate import FormatError
from agenthub_kernel.papers.db import ProjectRevisionRow
from tools.demo.guidance_stub import teaching


def sample():
    chunk=SimpleNamespace(id="c1",seq=0,page=1,text="Theorem. On a complete metric space, every contraction has a unique fixed point.")
    return chunk,teaching({"chunks":[{"chunk_id":chunk.id,"text":chunk.text}]})


def test_empty_studies_fail_instead_of_becoming_reviewable_drafts():
    from agenthub_kernel.papers.guidance_export import quality
    assert quality({},True)[0]=='failed'
    assert quality({'p':{'units':[],'depth':'partial'}},True)[0]=='failed'
    assert quality({'p':{'units':[],'depth':'material_missing'}},False)[0]=='failed'
    assert quality({'p':{'units':[{}],'depth':'partial'}},True)[0]=='needs_review'


def test_saved_reply_recovery_excludes_bad_sources_and_transitive_dependents():
    from agenthub_kernel.papers.guidance import recover_study_draft
    c,out=sample()
    # Append independently authored invalid material and its dependent chain.
    bad=copy.deepcopy(out['units'][-1]);bad.update(id='bad',sources=[{'chunk_id':c.id,'quote':'A quotation absent from the provided text.'}])
    child=copy.deepcopy(bad);child.update(id='child',depends_on=['bad'],sources=[])
    grandchild=copy.deepcopy(child);grandchild.update(id='grandchild',depends_on=['child'])
    seed={**out,'units':out['units']+[bad,child,grandchild]}
    recovered=recover_study_draft(seed,[c])
    assert recovered['units']==study_result(out,[c])['units']
    assert recovered['math_state']=='incomplete' and recovered['missing']
    assert recovered['recovery']['excluded_unit_ids']==['bad','child','grandchild']
    assert recovered['recovery']['model_calls']==0 and not recovered['recovery']['reviewed']
    assert seed['units'][-3]['sources'][0]['quote'].startswith('A quotation absent')
    malformed=copy.deepcopy(out);malformed['units'][0]['kind']='invented-kind'
    assert recover_study_draft(malformed,[c]) is None


def test_failed_teaching_exports_clear_diagnostics_and_reexports_without_calls(hub,monkeypatch):
    from tools.demo import guidance_stub
    original=guidance_stub.respond
    def response(template,data):
        return {'units':[]} if template in ('research.study.v1','research.study.v2') else original(template,data)
    monkeypatch.setattr(guidance_stub,'respond',response)
    tid,v=complete(hub,'proposal','empty teaching regression')
    assert v['quality_status']=='failed'
    assert v['outcome']['sample']['included']==1
    assert v['outcome']['sample']['fulltext']==1
    assert v['papers'][0]['read_status']=='failed'
    assert any('讲解未生成' in reason for reason in v['outcome']['quality_reasons'])
    before=hub.model_calls()
    assert hub.api('POST',f'/api/papers/{tid}/reexport').status_code==200
    assert hub.model_calls()==before
    report=hub.api('GET',f'/api/papers/{tid}/artifacts/research-report.json').json()
    assert report['quality_status']=='failed' and report['quality_reasons']
    assert report['sample']==v['outcome']['sample']
    assert '讲解正文未生成' in hub.api('GET',f'/api/papers/{tid}/artifacts/study.md').text
    assert '研究方案未生成' in hub.api('GET',f'/api/papers/{tid}/artifacts/research-plan.md').text
    assert '可用讲解单元为 0' in hub.api('GET',f'/api/papers/{tid}/artifacts/EXPORT-README.txt').text


def test_failed_directions_keep_existing_teaching_as_partial_output(hub,monkeypatch):
    from tools.demo import guidance_stub
    original=guidance_stub.respond
    def response(template,data):
        return {'comparison':'candidate generation failed','candidates':[]} if template=='research.directions.v1' else original(template,data)
    monkeypatch.setattr(guidance_stub,'respond',response)
    tid,v=complete(hub,'proposal','empty directions regression')
    assert any(s['units'] for s in v['outcome']['studies'].values())
    assert not v['outcome']['directions']['candidates']
    assert v['quality_status']=='needs_review'
    assert any('研究方案未生成' in reason for reason in v['outcome']['quality_reasons'])


def test_historical_failed_step_does_not_taint_current_validated_teaching(hub):
    from agenthub_kernel.papers.db import PaperStepRow
    tid,v=complete(hub,'study','historical failure regression')
    paper=next(iter(v['outcome']['studies']))
    with hub.client.app.state.db.session() as session:
        session.add(PaperStepRow(task_id=tid,key='v0:study:old-input',kind='model',name='study',
                                target=paper,input_hash='old-input',status='failed',error='old failure'))
    before=hub.model_calls()
    assert hub.api('POST',f'/api/papers/{tid}/reexport').status_code==200
    assert hub.model_calls()==before
    report=hub.api('GET',f'/api/papers/{tid}/artifacts/research-report.json').json()
    assert report['quality_status']=='passed' and report['quality_reasons']==[]


def test_structure_requires_math_chain_and_verifiable_source():
    c,out=sample()
    assert study_result(out,[c])["units"][0]["sources"][0]["pdf_page"]==1
    for mutation in ("source","dependency","chain","explanation"):
        bad=copy.deepcopy(out)
        if mutation=="source":bad["units"][0]["sources"][0]["quote"]="This invented quotation is absent."
        if mutation=="dependency":bad["units"][7]["depends_on"]=["example1"]
        if mutation=="chain":bad["units"].pop(7)
        if mutation=="explanation":bad["units"][8]["reason"]=""
        with pytest.raises(FormatError):study_result(bad,[c])


def test_source_repair_identifies_unit_and_chunk_without_relaxing_quote_matching():
    c,out=sample()
    out['units'][0]['sources'][0]['quote']='This invented quotation is absent.'
    with pytest.raises(FormatError) as exc:
        study_result(out,[c])
    assert out['units'][0]['id'] in str(exc.value) and c.id in str(exc.value)


def test_citation_repair_preserves_teaching_units_and_reuses_the_stage_gate(hub, monkeypatch):
    from tools.demo import guidance_stub
    from agenthub_kernel.db import ApprovalRow, InvocationRow
    from agenthub_kernel.papers.db import PaperStepRow
    original = guidance_stub.respond
    observed = []
    seed = {}
    def response(template, data):
        result = original(template, data)
        if template == 'research.study.v2':
            for index in (0, 4):
                result['units'][index]['sources'][0]['quote'] = f'Absent authored quotation number {index}.'
            seed.update(copy.deepcopy(result))
        if template == 'research.source-repair.v1':
            observed.append(copy.deepcopy(data))
        return result
    monkeypatch.setattr(guidance_stub, 'respond', response)
    tid, view = complete(hub, 'study')
    assert len(observed) == 1
    assert {i['unit_id'] for i in observed[0]['issues']} == {'problem1', 'setting1'}
    study = next(iter(view['outcome']['studies'].values()))
    for before, after in zip(seed['units'], study['units']):
        assert before['id'] == after['id'] and before['latex'] == after['latex']
        assert before['depends_on'] == after['depends_on']
        if before['id'] != 'step2':  # the separate authored math-review fixture edits this unit
            assert before['text'] == after['text']
    with hub.client.app.state.db.session() as session:
        steps = list(session.scalars(select(PaperStepRow).where(PaperStepRow.task_id == tid, PaperStepRow.name == 'study')))
        assert len(steps) == 1 and len(steps[0].invocation_ids) == 2
        for iid in steps[0].invocation_ids:
            assert session.get(InvocationRow, iid).status == 'succeeded'
            assert session.scalar(select(ApprovalRow).where(ApprovalRow.invocation_id == iid)).status == 'consumed'


def test_targeted_patch_cannot_silently_change_other_units():
    c,out=sample();out=study_result(out,[c]);u=copy.deepcopy(out["units"][8]);u["text"]+=" Corrected."
    patch={"replacements":[u],"math_state":"complete","missing":[]}
    fixed=patch_result(patch,out,[c],{"step2"})
    assert fixed["units"][:8]==out["units"][:8] and fixed["units"][9:]==out["units"][9:]
    with pytest.raises(FormatError):patch_result(patch,out,[c],{"step1"})


def test_proof_retrieval_follows_anchor_and_neighbor_not_keyword_only():
    chunks=[SimpleNamespace(id=str(i),seq=i,page=i+1,text=("Appendix Z proof" if i==8 else "filler ")+" x"*50) for i in range(12)]
    selected=pick(chunks,[{"terms":["Appendix Z"],"pages":[]}],budget=1200)
    assert {c.id for c in selected}=={"7","8","9","10"}
    assert pick(chunks,[{"terms":["absent proof"],"pages":[999]}])==[]
    assert not any(c.id=="8" for c in pick(chunks,[{"terms":["Appendix Z"]}],exclude={"8"}))


def test_teaching_example_tail_bound_and_iterations():
    # Arithmetic verifies this authored example only, not arbitrary generated proofs.
    x=Fraction(0)
    for n in range(1,9):
        x=(x+1)/2
        assert x==1-Fraction(1,2**n)
        assert abs(1-x)<=Fraction(1,2)**n


def body(hub, mode="project", topic="core learning"):
    data=hub.body(target=1,topic=topic)
    data["request"].update(mode=mode,core_papers=1,repair_rounds=1)
    return data


def complete(hub, mode="project", topic="core learning"):
    hub.prove()
    r=hub.api("POST","/api/papers",json=body(hub,mode,topic));assert r.status_code==200,r.text
    tid=r.json()["task_id"]
    view=hub.run(tid)
    assert view["status"]=="completed", json.dumps(view["stage"],ensure_ascii=False)
    return tid,view


def test_guided_flow_patches_one_unit_reuses_results_and_saves_project(hub):
    tid,v=complete(hub)
    s=next(iter(v["outcome"]["studies"].values()))
    assert s["depth"]=="model_checked" and "修复后" in s["units"][8]["text"]
    names=[step["name"] for step in v["steps"]]
    assert names.count("study")==1 and names.count("study_patch")==1 and names.count("study_review")==2
    assert "section" not in names and "synthesize" not in names
    assert v["quality_status"]=="passed"  # this stage's teaching checks, not publication readiness
    assert v["outcome"]["quality_dimensions"]["research_plan"]=="unverified"
    assert len(v["outcome"]["directions"]["candidates"])==2
    assert "study_patch" in v["usage_by_step"]
    before=hub.model_calls()
    project={"expected_version":0,"selected_candidate":"A","notes":{"proof_notes":"下一步查证唯一性","draft":"尚未开始"}}
    r=hub.api("PUT",f"/api/papers/{tid}/project",json=project)
    assert r.status_code==200,r.text
    assert r.json()["version"]==1
    assert hub.api("PUT",f"/api/papers/{tid}/project",json=project).status_code==409
    project.update(expected_version=1,selected_candidate="B")
    assert hub.api("PUT",f"/api/papers/{tid}/project",json=project).json()["version"]==2
    with hub.client.app.state.db.session() as db:
        assert len(list(db.scalars(select(ProjectRevisionRow).where(ProjectRevisionRow.task_id==tid))))==2
    assert hub.api("POST",f"/api/papers/{tid}/reexport").status_code==200
    assert hub.model_calls()==before
    md=hub.api("GET",f"/api/papers/{tid}/artifacts/study.md").text
    assert "\\frac{q^n}" in md and "补充讲解" in md
    assert "\n$$\n" in md and "\n\\[\n" not in md
    assert all("display_markdown" in unit for unit in s["units"])
    # Presentation fields must not enter persisted model data or subsequent prompts.
    from agenthub_kernel.papers.db import ResearchTaskRow
    with hub.client.app.state.db.session() as db:
        stored=next(iter(db.get(ResearchTaskRow,tid).outcome["studies"].values()))
        assert all("display_markdown" not in unit for unit in stored["units"])
    assert "下一步查证唯一性" in hub.api("GET",f"/api/papers/{tid}/artifacts/paper-project.md").text
    hub.restart()
    assert hub.api("GET",f"/api/papers/{tid}/project").json()["version"]==2


def test_question_is_pending_and_bounded_to_selected_step(hub):
    tid,v=complete(hub,"study")
    s=next(iter(v["outcome"]["studies"].values()))
    data={"paper_id":s["paper_id"],"unit_id":"step1","question":"为什么？","client_request_id":"question1"}
    before=hub.model_calls()
    r=hub.api("POST",f"/api/papers/{tid}/questions",json=data)
    assert r.status_code==200,r.text
    assert hub.model_calls()==before
    assert r.json()["approval_status"]=="pending"
    again=hub.api("POST",f"/api/papers/{tid}/questions",json=data)
    assert again.json()["invocation_id"]==r.json()["invocation_id"]
    data["unit_id"]="outside"
    assert hub.api("POST",f"/api/papers/{tid}/questions",json=data).status_code==404
    assert hub.model_calls()==before
    assert hub.client.get(f"/api/papers/{tid}/project").status_code==401
    assert hub.client.get(f"/api/papers/{tid}/project",headers={"Authorization":"Bearer dev-agent-token"}).status_code==403


def test_material_issue_fetches_new_proof_chunks_before_patch(monkeypatch):
    from agenthub_kernel.papers import guidance, pipeline
    from tools.demo.guidance_stub import respond
    chunks=[SimpleNamespace(id=str(i),seq=i,page=i+1,text=("Definition" if i==0 else "Appendix Z proof" if i==9 else "Context")+" information"*18) for i in range(12)]
    paper=SimpleNamespace(id="paper1",document_id="doc1",title="Authored proof fixture")
    monkeypatch.setattr(pipeline,"_included",lambda ctx:[paper])
    original_pick=guidance.pick
    monkeypatch.setattr(guidance,"pick",lambda cs,requests=(),**kw:original_pick(cs,requests,**kw,budget=500))
    class Ctx:
        request=SimpleNamespace(topic="fixture",core_papers=1,repair_rounds=1)
        rules=SimpleNamespace(review_agent=None)
        session=SimpleNamespace(get=lambda *args:SimpleNamespace(id="doc1",parse_status="ok",sha256="doc-hash"),scalars=lambda *args:chunks)
        task=SimpleNamespace(outcome={})
        calls=[]
        def outcome(self,**kwargs):self.task.outcome={**self.task.outcome,**kwargs}
        def model(self,name,target,template,data,key,validator,**kwargs):
            self.calls.append((name,data))
            out=respond(template.id,data)
            if template==guidance.STUDY:
                out.update(math_state="incomplete",missing=[{"reason":"Appendix Z is required","pages":[10],"terms":["Appendix Z"]}])
            return "done",validator(out)
    ctx=Ctx()
    assert guidance.phase_read(ctx)=="related_read"
    study_prompt=ctx.calls[0][1];patch_prompt=ctx.calls[1][1]
    old={c["chunk_id"] for c in study_prompt["chunks"]}
    new={c["chunk_id"] for c in patch_prompt["chunks"]}
    assert old.isdisjoint(new) and "9" in new
    assert ctx.task.outcome["studies"]["paper1"]["depth"]=="method_read"


def test_missing_proof_does_not_pay_to_paraphrase_same_material(hub, monkeypatch):
    from tools.demo import guidance_stub
    original = guidance_stub.respond
    def response(template, data):
        # Pure missing material: no independent math/citation defect to repair.
        return {'issues': []} if template=='research.study-review.v1' else original(template, data)
    monkeypatch.setattr(guidance_stub, 'respond', response)
    tid,v=complete(hub,"study","SIM_STUDY_GAP")
    s=next(iter(v["outcome"]["studies"].values()))
    assert s["depth"]=="partial" and s["missing"]
    assert not any(st["name"]=="study_patch" for st in v["steps"])
    assert v["quality_status"]!="passed"


def test_missing_material_does_not_skip_independent_math_repairs(hub, monkeypatch):
    from tools.demo import guidance_stub
    original = guidance_stub.respond
    patch_requests = []
    gap = {'reason': 'Authored unavailable appendix', 'pages': [999], 'terms': ['unavailable appendix']}
    def response(template, data):
        out = original(template, data)
        if template == 'research.study.v2':
            out.update(math_state='incomplete', missing=[copy.deepcopy(gap)])
        if template == 'research.study-patch.v1':
            patch_requests.append(copy.deepcopy(data))
            # The fixture tries to erase the gap and upgrade completeness. The
            # backend must retain the gap despite successful math corrections.
            out.update(math_state='complete', missing=[])
        return out
    monkeypatch.setattr(guidance_stub, 'respond', response)
    _, view = complete(hub, 'study')
    study = next(iter(view['outcome']['studies'].values()))
    assert len(patch_requests)==1
    assert patch_requests[0]['editable_ids']==['step2']
    assert patch_requests[0]['chunks'] and not patch_requests[0]['allow_additions']
    assert 'No new material' in patch_requests[0]['material_constraint']
    assert '修复后' in study['units'][8]['text']
    assert study['math_state']=='incomplete' and study['missing']==[gap]
    assert study['depth']=='partial' and view['quality_status']=='needs_review'


def test_preserving_gaps_cannot_authorize_new_units_or_edit_other_units():
    c, out = sample()
    out.update(math_state='incomplete', missing=[{'reason':'Missing authored proof', 'pages':[99], 'terms':[]}])
    study = study_result(out, [c])
    patch = {'replacements':[copy.deepcopy(study['units'][8])], 'math_state':'complete', 'missing':[]}
    fixed = patch_result(patch, study, [c], {'step2'}, preserve_material_gaps=True)
    assert fixed['missing']==study['missing'] and fixed['math_state']=='incomplete'
    bad = copy.deepcopy(patch); bad['replacements'][0]['id']='step1'
    with pytest.raises(FormatError):
        patch_result(bad, study, [c], {'step2'}, preserve_material_gaps=True)
    bad = copy.deepcopy(patch); bad['additions']=[copy.deepcopy(study['units'][9])]
    with pytest.raises(FormatError):
        patch_result(bad, study, [c], {'step2'}, preserve_material_gaps=True)


def test_model_payload_preserves_teaching_and_quotes_without_copying_backend_audits():
    from agenthub_kernel.papers.guidance import unit_payload
    c,out=sample();study=study_result(out,[c])
    original=copy.deepcopy(study)
    model_units=unit_payload(study['units'])
    assert len(json.dumps(model_units))<len(json.dumps(study['units']))
    for before,after in zip(study['units'],model_units):
        assert {k:v for k,v in before.items() if k!='sources'}=={k:v for k,v in after.items() if k!='sources'}
        assert [{k:v for k,v in r.items() if k!='source_location'} for r in before['sources']]==after['sources']
    assert study==original and study['units'][0]['sources'][0]['source_location']


def test_legacy_hash_and_new_budget_estimate():
    req=ResearchRequest(topic="test")
    assert "mode" not in req.as_dict()
    guided=ResearchRequest(topic="test",mode="project")
    estimate=estimate_calls(guided,review=True)
    assert "targeted_read_or_repair" in estimate["parts"] and estimate["worst_case"]>estimate["expected"]
    for args in ({"mode":"bad"},{"core_papers":4},{"repair_rounds":3}):
        with pytest.raises(ValueError):ResearchRequest(topic="test",**args)


def test_guided_pause_restart_reuses_finished_reading(hub):
    hub.prove()
    r=hub.api("POST","/api/papers",json=body(hub,"study"));tid=r.json()["task_id"]
    v=hub.run(tid,until=lambda v:any(s["name"]=="study_review" for s in v["steps"]))
    assert v["status"]=="running"
    assert hub.api("POST",f"/api/papers/{tid}/stage/pause").status_code==200
    before=hub.model_calls();hub.client.app.state.autopilot.tick();assert hub.model_calls()==before
    hub.restart()
    assert hub.api("POST",f"/api/papers/{tid}/stage/resume").status_code==200
    finished=hub.run(tid)
    assert finished["status"]=="completed"
    assert len([s for s in finished["steps"] if s["name"]=="study"])==1
    assert all(s["attempts"]==1 for s in finished["steps"] if s["kind"]=="model")


def test_guided_budget_refusal_has_no_model_start(hub):
    hub.prove();data=body(hub);data.update(token_budget_total=5,token_budget_per_call=5)
    before=hub.model_calls()
    r=hub.api("POST","/api/papers",json=data);assert r.status_code==200,r.text
    view=hub.run(r.json()["task_id"])
    assert view["status"]=="blocked" and hub.model_calls()==before
