"""User deletion cannot erase budget/audit or turn into an execution bypass."""
import pytest
from sqlalchemy import select
from agenthub_kernel.db import ApprovalRow, ChatMessageRow, ConversationRow, InvocationRow, StageRunRow
from agenthub_rt.auth import Principal, authenticate
from tests.http.test_conversations import client, conversation, submit, approve, run, USER, AGENT


def test_delete_hides_chat_revokes_pending_and_cannot_run_it(client):
    cid=conversation(client); turn=submit(client,cid)
    r=client.delete(f'/api/conversations/{cid}',headers=USER)
    assert r.status_code==200 and r.json()['files_preserved'] and r.json()['usage_preserved']
    assert client.get('/api/conversations',headers=USER).json()['conversations']==[]
    assert client.get(f'/api/conversations/{cid}/messages',headers=USER).status_code==404
    assert client.post(f'/api/conversations/{cid}/invocations',headers=USER,json={
        'text':'retry','agent_id':'mock','client_request_id':'two'}).status_code==404
    assert client.post(f"/api/approvals/{turn['approval_id']}/approve",headers=USER).status_code==409
    assert client.app.state.worker.adapter.start_count==0
    with client.app.state.db.session() as s:
        assert s.get(ConversationRow,cid) is not None
        assert s.scalar(select(ChatMessageRow.id).where(ChatMessageRow.conversation_id==cid)) is not None


def test_completed_chat_deletion_retains_usage(client):
    cid=conversation(client); turn=submit(client,cid); approve(client,turn)
    assert run(client,turn).json()['status']=='succeeded'
    before=client.get('/api/usage/summary',headers=USER).json()
    assert client.delete(f'/api/conversations/{cid}',headers=USER).status_code==200
    assert client.get('/api/usage/summary',headers=USER).json()==before


@pytest.mark.parametrize('status',['running','starting','unknown','queued'])
def test_delete_refuses_active_or_unknown_calls(client,status):
    cid=conversation(client); turn=submit(client,cid)
    with client.app.state.db.session() as s: s.get(InvocationRow,turn['invocation_id']).status=status
    assert client.delete(f'/api/conversations/{cid}',headers=USER).status_code==409
    assert len(client.get('/api/conversations',headers=USER).json()['conversations'])==1


def test_delete_requires_owner_user(client):
    cid=conversation(client)
    assert client.delete(f'/api/conversations/{cid}',headers=AGENT).status_code==403
    client.app.dependency_overrides[authenticate]=lambda:Principal('different-user','user')
    assert client.delete(f'/api/conversations/{cid}').status_code==404
    client.app.dependency_overrides.clear()
    assert client.get(f'/api/conversations/{cid}/messages',headers=USER).status_code==200


def test_delete_cannot_orphan_an_open_stage(client):
    cid=conversation(client); turn=submit(client,cid)
    with client.app.state.db.session() as s:
        root=s.get(ConversationRow,cid).root_task_id
        s.add(StageRunRow(id='stage-live',conversation_id=cid,root_task_id=root,status='paused',
            execution_mode='strict',goal='goal',criteria='',rules={},rules_hash='hash',granted_by='local-user'))
    assert client.delete(f'/api/conversations/{cid}',headers=USER).status_code==409
