"""Native scope is rejected atomically before user-message/approval creation."""
import pytest
from sqlalchemy import func, select

from agenthub.contracts import Policy
from agenthub_kernel.db import ApprovalRow, ChatMessageRow, InvocationRow, TaskRow, TreeQuotaRow
from tests.http.test_conversations import client, conversation, submit, USER  # noqa: F401


def counts(client):
    with client.app.state.db.session() as session:
        return tuple(session.scalar(select(func.count()).select_from(model))
            for model in (TaskRow,TreeQuotaRow,InvocationRow,ApprovalRow,ChatMessageRow))


def native(client, cid, key='native'):
    return client.post(f'/api/conversations/{cid}/invocations',headers=USER,json={
        'text':'synthetic ordinary task','agent_id':'claude-code','execution_mode':'strict',
        'client_request_id':key,'native_subagents':'approved'})


def set_policy(client, root, **changes):
    response = client.patch(f'/api/tasks/{root}/policy',headers=USER,
        json=Policy(collaboration_enabled=True,**changes).as_dict())
    assert response.status_code == 200, response.text


def test_new_conversation_native_request_rolls_back_all_draft_side_effects(client):
    cid = conversation(client)
    before = counts(client)
    response = native(client,cid)
    assert response.status_code == 409
    assert response.json()['detail']['code'] == 'native_collaboration_disabled'
    assert counts(client) == before
    assert not client.app.state.worker.adapter.inputs
    # Failed request keys are not poisoned; ordinary strict mode stays usable.
    turn = submit(client,cid,key='native',agent_id='claude-code')
    assert turn['native_subagents'] == 'disabled'


@pytest.mark.parametrize('changes,code',[
    ({'max_subagents_concurrent':0},'native_collaboration_disabled'),
    ({'max_depth':0},'native_collaboration_disabled'),
    ({'token_budget_total':10000,'token_budget_per_call':1000},'native_budget_unmetered'),
])
def test_invalid_saved_native_policy_creates_no_message_or_approval(client,changes,code):
    cid = conversation(client)
    first = submit(client,cid)
    set_policy(client,first['task_id'],**changes)
    before = counts(client)
    response = native(client,cid)
    assert response.status_code == 409 and response.json()['detail']['code'] == code
    assert counts(client) == before
    assert not client.app.state.worker.adapter.inputs


def test_saved_collaboration_allows_only_pending_approval_and_idempotent_replay(client):
    cid = conversation(client)
    first = submit(client,cid)
    set_policy(client,first['task_id'])
    before = counts(client)
    response = native(client,cid)
    assert response.status_code == 200, response.text
    turn = response.json()
    assert turn['native_subagents'] == 'approved' and turn['approval_status'] == 'pending'
    replay = native(client,cid).json()
    assert replay['replayed'] and replay['invocation_id'] == turn['invocation_id']
    assert tuple(a-b for a,b in zip(counts(client),before)) == (0,0,1,1,1)
    assert not client.app.state.worker.adapter.inputs


def test_reenable_collaboration_can_retry_previously_rejected_request_key(client):
    cid = conversation(client)
    first = submit(client,cid)
    assert native(client,cid).status_code == 409
    set_policy(client,first['task_id'])
    assert native(client,cid).status_code == 200


def test_generic_invocation_route_rejects_native_scope_before_draft_or_approval(client):
    before = counts(client)
    response = client.post('/api/invocations',headers=USER,json={
        'task_id':'ignored','agent_id':'claude-code','task_input':'synthetic task',
        'execution_mode':'strict','permissions':{'native_subagents':'approved'}})
    assert response.status_code == 409
    assert response.json()['detail']['code'] == 'native_collaboration_disabled'
    assert counts(client) == before
