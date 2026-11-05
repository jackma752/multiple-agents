"""Large review inputs: exact round-trip, real stage creation and zero-call refusal."""
import copy
import json
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from agenthub.chat import ConversationTurn,MAX_MESSAGE_CHARS
from agenthub_kernel.db import InvocationRow,StageRunRow,ApprovalRow
from agenthub_kernel.papers.guidance import CHECK
from agenthub_kernel.papers.review_transport import indexed_review,render_for_model,PaperInputTooLarge
from agenthub_kernel.papers.templates import render
from agenthub_kernel.papers.validate import FormatError


def material():
    chunks=[{'chunk_id':f'chunk{i}','source_id':f'S{i+1:02d}','pdf_page':i+1,
        'text':f'Unique source {i}: '+('全文 α 🧮 exact text with line breaks.\n'*80)+f'End of source {i}.'} for i in range(2)]
    units=[]
    for i in range(35):
        c=chunks[i%2]
        units.append({'id':f'u{i}','kind':'step','title':f'原有单元{i}','text':'保持这个解释原样。',
            'latex':r'x_{n+1}=Tx_n','reason':'保留理由和材料缺口。','depends_on':[f'u{i-1}'] if i else [],
            'sources':[{'chunk_id':c['chunk_id'],'quote':c['text'],'pdf_page':c['pdf_page']}]})
    return {'paper_id':'authored-paper','units':units,'chunks':chunks,'changed_ids':[u['id'] for u in units],
        'previous_issues':[{'unit_id':'u0','problem':'保留原审阅问题'}]}


def expand(data):
    fixed=copy.deepcopy(data);fixed.pop('source_format')
    chunks={c['source_id']:c for c in data['chunks']}
    for unit in fixed['units']:
        for i,ref in enumerate(unit['sources']):
            c=chunks[ref['source_id']];start,end=ref['span']
            assert c['text'][start:end].startswith(ref['anchor'])
            unit['sources'][i]={'chunk_id':c['chunk_id'],'quote':c['text'][start:end],'pdf_page':c['pdf_page']}
    return fixed


def test_long_review_is_exactly_reversible_including_unicode_math_and_dependencies():
    data=material();before=copy.deepcopy(data)
    assert len(render(CHECK,data))>MAX_MESSAGE_CHARS
    template,prompt=render_for_model('study_review:paper:r1',CHECK,data)
    assert template.id=='research.study-review-indexed.v1' and len(prompt)<MAX_MESSAGE_CHARS
    sent=json.JSONDecoder().raw_decode(prompt.split('<<<DATA (untrusted material)\n')[1])[0]
    assert expand(sent)==before and data==before
    assert sent['chunks']==before['chunks'] and sent['previous_issues']==before['previous_issues']
    ConversationTurn(text=prompt,agent_id='codex',client_request_id='authored-review')


def test_small_requests_keep_the_exact_old_text_and_semantic_content():
    data={'units':[],'chunks':[]}
    template,prompt=render_for_model('study_review:p:r0',CHECK,data)
    assert template is CHECK and prompt==render(CHECK,data)
    assert json.loads(render(CHECK,data,compact=True).split('<<<DATA (untrusted material)\n')[1].split('\nDATA>>>')[0])==data


@pytest.mark.parametrize('bad',['unknown','absent','ambiguous','page','extra','duplicate_alias'])
def test_indexing_never_guesses_or_expands_approved_material(bad):
    data=material();ref=data['units'][0]['sources'][0]
    if bad=='unknown':ref['chunk_id']='unknown'
    if bad=='absent':ref['quote']='An authored quote absent from every chunk.'
    if bad=='ambiguous':ref['quote']='exact text with line breaks.'
    if bad=='page':ref['pdf_page']=99
    if bad=='extra':ref['extra']='cannot silently drop provenance'
    if bad=='duplicate_alias':data['chunks'][1]['source_id']=data['chunks'][0]['source_id']
    before=copy.deepcopy(data)
    with pytest.raises(FormatError):indexed_review(data)
    assert data==before


def test_still_oversized_teaching_is_retained_and_not_truncated():
    data=material();data['units'][0]['text']='Retain all authored teaching. '*10000
    before=copy.deepcopy(data)
    with pytest.raises(PaperInputTooLarge) as error:render_for_model('study_review:p:r1',CHECK,data)
    assert error.value.sent_chars>MAX_MESSAGE_CHARS and data==before


def test_exact_message_boundary_and_retry_suffix_refuse_before_creation(hub):
    from agenthub_kernel.autopilot import _Blocked
    from tests.papers.test_guidance import body
    hub.prove()
    tid=hub.api('POST','/api/papers',json=body(hub,'study')).json()['task_id']
    before=hub.model_calls()
    ap=hub.client.app.state.autopilot
    with hub.client.app.state.db.session() as s:
        from agenthub_kernel.papers.db import ResearchTaskRow
        stage=s.get(StageRunRow,s.get(ResearchTaskRow,tid).stage_id)
        with pytest.raises(_Blocked) as error:
            ap._create(s,stage,'p:study_review:authored:r1','deepseek','x'*(MAX_MESSAGE_CHARS+1),'authored-key')
        assert error.value.reason=='paper_input_too_large'
        assert '100001/100000' in error.value.message
    assert hub.model_calls()==before
    view=hub.view(tid)
    assert not any(st['invocation_ids'] for st in view['steps'])


def test_stage_reports_context_problem_not_quota_and_never_starts_oversize(hub,monkeypatch):
    from tools.demo import guidance_stub
    from tests.papers.test_guidance import body
    original=guidance_stub.respond
    def reply(template,data):
        out=original(template,data)
        if template=='research.study.v2':
            for unit in out['units']:unit['text']='Authored untruncated teaching. '*400
        return out
    monkeypatch.setattr(guidance_stub,'respond',reply)
    hub.prove()
    tid=hub.api('POST','/api/papers',json=body(hub,'study')).json()['task_id']
    view=hub.run(tid)
    assert view['status']=='blocked' and view['stage']['summary']['reason']=='paper_input_too_large'
    assert '调整调用次数或额度不能解决' in view['stage']['summary']['needs']
    assert not any(st['name']=='study_review' and st['invocation_ids'] for st in view['steps'])
    count=hub.model_calls();hub.client.app.state.autopilot.tick();assert hub.model_calls()==count


def test_large_review_reaches_codex_adapter_under_normal_stage_grant(tmp_path):
    from fastapi.testclient import TestClient
    from agenthub_adapters.appserver_adapter import AppServerCodexAdapter
    from agenthub_adapters.appserver_stub import InMemoryStubFactory
    from agenthub_adapters.claude_code import ClaudeCodeAdapter,FakeClaudeRunner,StubClaudeLauncher,result_object,scripted
    from agenthub_adapters.routing import RoutingAdapter
    from agenthub_adapters.store import ExecutionStore
    from agenthub_kernel.db import Database,StageGrantRow
    from agenthub_kernel.papers.net import FakeFetcher
    from agenthub_rt.api import create_app
    from tools.demo.paper_fixtures import FixtureWeb
    from tools.demo.paper_stub_model import respond
    from tests.papers.conftest import Hub
    from tests.papers.test_guidance import body
    authored=[]
    def cc(argv,env,prompt):
        reply=respond(prompt)
        if prompt.startswith('AGENTHUB-TEMPLATE: research.study.v2\n'):
            data=json.JSONDecoder().raw_decode(prompt.split('<<<DATA (untrusted material)\n')[1])[0]
            out=json.loads(reply);c=data['chunks'][0]
            for unit in out['units']:
                unit['text']+=' Authored example context preserved.'*150
                unit['sources']=[{'chunk_id':c['chunk_id'],'quote':c['text']} for _ in range(12)]
            authored.extend(copy.deepcopy(out['units']))
            reply=json.dumps(out,ensure_ascii=False)
        return scripted(result_object(reply=reply))(argv,env,prompt)
    cc_runner=FakeClaudeRunner(cc)
    factory=InMemoryStubFactory('happy')
    router=RoutingAdapter({
        'claude-code':ClaudeCodeAdapter(launcher=StubClaudeLauncher('success-usage'),workdir_root=str(tmp_path/'cc'),
            store=ExecutionStore(str(tmp_path/'cc.db'),table='claude_code_executions'),runner=cc_runner),
        'codex':AppServerCodexAdapter(transport_factory=factory,workdir_root=str(tmp_path/'codex'),
            store=ExecutionStore(str(tmp_path/'codex.db'),table='appserver_executions'))})
    h=Hub(tmp_path);app=create_app(db=Database(f'sqlite:///{(tmp_path/"hub.db").as_posix()}'),adapter=router,
        paper_fetcher=FakeFetcher(FixtureWeb()),paper_root=str(tmp_path/'papers'),autopilot_interval=None)
    with TestClient(app) as client:
        h.client=client;h.prove(agents=('claude-code','codex'))
        request=body(h,'study');request['roles']['review_agent']='codex'
        r=h.api('POST','/api/papers',json=request);assert r.status_code==200,r.text
        tid=r.json()['task_id']
        view=h.run(tid,until=lambda v:any(st['name']=='study_review' and st['invocation_ids'] for st in v['steps']))
        step=next(st for st in view['steps'] if st['name']=='study_review' and st['invocation_ids'])
        assert step['template']['id']=='research.study-review-indexed.v1'
        with app.state.db.session() as s:
            inv=s.get(InvocationRow,step['invocation_ids'][0]);snapshot=inv.snapshot
            assert inv.status=='succeeded' and snapshot['agent_id']=='codex'
            assert len(snapshot['task_input'])<=MAX_MESSAGE_CHARS and 'max_output_tokens' not in snapshot['resources']
            grant=s.scalar(select(StageGrantRow).where(StageGrantRow.invocation_id==inv.id))
            assert grant and s.get(ApprovalRow,grant.approval_id).status=='consumed'
            sent=json.JSONDecoder().raw_decode(snapshot['task_input'].split('<<<DATA (untrusted material)\n')[1])[0]
            decoded=expand(sent)
            assert [u['text'] for u in decoded['units']]==[u['text'] for u in authored]
            assert len(render(CHECK,decoded))>MAX_MESSAGE_CHARS
    app.state.db.engine.dispose()
