"""Selected DeepSeek models reach HTTP, budgets and automatic stage rules offline."""
import pytest
from fastapi.testclient import TestClient
from agenthub.models import budget_key
from agenthub_adapters.deepseek import DeepSeekAdapter
from agenthub_adapters.store import ExecutionStore
from agenthub_kernel.db import ApprovalRow, Database
from agenthub_rt.api import create_app
from tests.http.test_autopilot import run_stage

USER = {"Authorization": "Bearer dev-user-token"}


@pytest.fixture
def hub(tmp_path):
    sent = []
    def transport(payload, headers):
        sent.append(payload)
        yield {"model": payload["model"], "choices": [{"delta": {"content": "offline reply"}, "finish_reason": "stop"}],
               "usage": {"prompt_tokens": 12, "completion_tokens": 3, "total_tokens": 15,
                         "prompt_cache_hit_tokens": 0, "prompt_cache_miss_tokens": 12}}
    adapter = DeepSeekAdapter("https://api.deepseek.com", "deepseek-flash", stub="ok",
        credentials_path=str(tmp_path / "unused"), transport=transport,
        store=ExecutionStore(str(tmp_path / "adapter.db")))
    app = create_app(db=Database(f"sqlite:///{(tmp_path / 'hub.db').as_posix()}"), adapter=adapter)
    with TestClient(app) as client:
        yield client, sent
    app.state.db.engine.dispose()


def create(client):
    return client.post('/api/conversations', headers=USER, json={}).json()['conversation_id']


def submit(client, cid, model, key, **extra):
    response = client.post(f'/api/conversations/{cid}/invocations', headers=USER,
        json={'agent_id':'deepseek','text':'x','model':model,'client_request_id':key,**extra})
    assert response.status_code == 200, response.text
    return response.json()


def run(client, turn):
    assert client.post(f"/api/approvals/{turn['approval_id']}/approve",headers=USER).status_code == 200
    return client.post(f"/api/invocations/{turn['invocation_id']}/run",headers=USER,params={'wait':'true'})


def test_model_candidates_are_passive_and_selected_models_reach_wire_and_ledger(hub):
    client, sent = hub
    models = client.get('/api/agents/deepseek/models',headers=USER).json()
    assert models['selection'] and models['effort_selection'] and not models['refresh_available']
    assert {m['id'] for m in models['models']} == {'deepseek-flash','deepseek-v4-pro'}
    assert models['default_model'] == 'deepseek-flash' and sent == []
    cid = create(client)
    for model in ('deepseek-flash','deepseek-v4-pro'):
        turn = submit(client,cid,model,model,reasoning_effort='high')
        assert turn['model'] == model and turn['reasoning_effort'] == 'high'
        assert run(client,turn).json()['status'] == 'succeeded'
        assert sent[-1]['model'] == model and sent[-1]['reasoning_effort'] == 'high'
    usage = client.get(f'/api/conversations/{cid}/usage',headers=USER).json()
    assert {m['model'] for m in usage['by_model']} == {'deepseek-flash','deepseek-v4-pro'}
    assert usage['total']['known']['input_total'] == 24
    assert usage['total']['known']['output_total'] == 6


def test_one_deepseek_model_budget_does_not_block_another_or_consume_refused_approval(hub):
    client,sent = hub
    cid=create(client)
    first=submit(client,cid,'deepseek-flash','first')
    assert run(client,first).json()['status']=='succeeded'
    root=client.get(f'/api/conversations/{cid}/messages',headers=USER).json()['root_task_id']
    response=client.patch(f'/api/tasks/{root}/policy',headers=USER,json={
        'model_budgets':{budget_key('deepseek','deepseek','deepseek-flash'):{'total':10,'per_call':5}}})
    assert response.status_code==200,response.text
    refused=submit(client,cid,'deepseek-flash','refused')
    response=run(client,refused)
    assert response.status_code==409 and len(sent)==1
    with client.app.state.db.session() as session:
        assert session.get(ApprovalRow,refused['approval_id']).status=='approved'
    other=submit(client,cid,'deepseek-v4-pro','other')
    assert run(client,other).json()['status']=='succeeded' and len(sent)==2


def test_automatic_stage_uses_authorized_deepseek_model_and_effort(hub):
    client,sent=hub
    cid=create(client)
    assert run(client,submit(client,cid,'deepseek-flash','proof')).json()['status']=='succeeded'
    response=client.post(f'/api/conversations/{cid}/stages',headers=USER,json={
        'goal':'reply once','criteria':'return a reply','execution_mode':'strict',
        'agents':['deepseek'],'main_agent':'deepseek','plan':'none',
        'models':{'deepseek':{'model':'deepseek-v4-pro','reasoning_effort':'low'}}})
    assert response.status_code==200,response.text
    assert run_stage(client,cid)=='completed'
    assert len(sent)==2 and sent[-1]['model']=='deepseek-v4-pro'
    assert sent[-1]['reasoning_effort']=='low'
