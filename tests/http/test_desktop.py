"""Release onboarding/auth/workspace tests, no native Agent or network inference."""
import dataclasses
import datetime as dt
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from agenthub.contracts import InvocationSnapshot
from agenthub.workspace import approved_workspace
from agenthub_kernel import desktop
from agenthub_kernel.db import Database, InvocationRow, ApprovalRow
from agenthub_kernel.service import PolicyService, now
from agenthub_rt.api import create_app

ORIGIN = {"Origin": "http://testserver"}


@pytest.fixture
def release(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_HUB_DESKTOP", "1")
    monkeypatch.setenv("AGENT_HUB_DESKTOP_HOME", str(tmp_path / "desktop"))
    app = create_app(db=Database(f"sqlite:///{tmp_path / 'kernel.db'}"))
    with TestClient(app, base_url="http://localhost") as client:
        yield client, tmp_path


def pair(client):
    ticket = client.app.state.desktop_ticket
    result = client.post("/api/desktop/session", json={"ticket": ticket}, headers={"Origin":"http://localhost"})
    assert result.status_code == 200, result.text
    return ticket


def settings(tmp_path, **extra):
    workspace = tmp_path / "工作 项目"
    workspace.mkdir(exist_ok=True)
    return {"workspace": str(workspace), "workspace_access":"read-only", "agents": {},
            "confirm": True, **extra}


def post(client, path, body):
    return client.post(path, json=body, headers={"Origin":"http://localhost"})


def test_release_rejects_known_dev_tokens_and_missing_session(release):
    client, _ = release
    for token in ("dev-user-token", "dev-agent-token", "dev-worker-token"):
        assert client.get('/api/desktop/settings', headers={'Authorization':'Bearer '+token}).status_code == 401
    assert client.get('/api/client-config').json() == {"desktop":True,"development_token":""}


def test_pair_is_single_use_cookie_is_http_only_and_origin_checked(release):
    client, tmp = release
    ticket = pair(client)
    assert 'HttpOnly' in client.cookies.jar._cookies['localhost.local']['/']['agenthub_session']._rest
    assert client.post('/api/desktop/session', json={'ticket':ticket}).status_code == 401
    body = settings(tmp)
    assert client.post('/api/desktop/settings', json=body).status_code == 403
    assert client.post('/api/desktop/settings', json=body, headers={'Origin':'https://evil.example'}).status_code == 403
    assert post(client, '/api/desktop/settings', body).status_code == 200


def test_expired_pair_and_untrusted_host(release):
    client, _ = release
    client.app.state.desktop_ticket_deadline = 0
    assert client.post('/api/desktop/session', json={'ticket':client.app.state.desktop_ticket}).status_code == 401
    assert client.get('/chat', headers={'Host':'evil.example'}).status_code == 403


def test_only_local_launcher_can_mint_reopen_ticket(release):
    client,_=release
    old=pair(client)
    assert post(client,'/api/desktop/reopen',{'control':client.app.state.desktop_control}).status_code==403
    assert client.post('/api/desktop/reopen',json={'control':'unknown'}).status_code==403
    response=client.post('/api/desktop/reopen',json={'control':client.app.state.desktop_control})
    assert response.status_code==200
    ticket=response.json()['ticket']; assert ticket!=old
    assert 'control' not in client.get('/api/desktop/settings').text
    assert client.post('/api/desktop/session',json={'ticket':ticket}).status_code==200
    assert client.post('/api/desktop/session',json={'ticket':ticket}).status_code==401


def test_no_agents_is_valid_and_disabled_agents_are_not_routed(release):
    client, tmp = release
    pair(client)
    assert post(client, '/api/desktop/settings', settings(tmp)).status_code == 200
    assert client.get('/api/adapter').json()['agent_ids'] == []
    assert client.app.state.worker.adapter.for_agent('codex') is None


def test_workspace_snapshot_is_hash_bound_and_old_approval_revoked(release):
    client, tmp = release
    pair(client)
    body = settings(tmp)
    assert post(client, '/api/desktop/settings', body).status_code == 200
    with client.app.state.db.session() as session:
        svc = PolicyService(session)
        task = svc.create_task(agent_id='codex', goal='workspace')
        inv = svc.create_invocation(task_id=task.id, snapshot=InvocationSnapshot(
            agent_id='codex', task_input='read', parent_task_id=None, depth=0))
        aid = svc.create_approval(invocation_id=inv.id).id
        iid = inv.id
        snap = InvocationSnapshot(**inv.snapshot)
        assert approved_workspace(snap) == str(Path(body['workspace']).resolve())
        changed = dataclasses.replace(snap, permissions={**snap.permissions,'workspace_access':'workspace-write'})
        assert changed.payload_hash() != snap.payload_hash()
    body['workspace_access'] = 'workspace-write'
    assert post(client, '/api/desktop/settings', body).status_code == 200
    with client.app.state.db.session() as session:
        assert session.get(ApprovalRow, aid).status == 'revoked'
        assert session.get(InvocationRow, iid).snapshot['permissions']['workspace_access'] == 'read-only'


def test_adoption_never_happens_during_scan_and_updated_binary_is_refused(release, monkeypatch):
    client, tmp = release
    pair(client)
    calls = []
    monkeypatch.setattr(desktop.subprocess, 'run', lambda *a, **kw: calls.append(a) or
                        SimpleNamespace(returncode=0, stdout=b'codex-cli test-version'))
    post(client, '/api/desktop/discover', {})
    assert calls == []
    exe = tmp / 'codex.exe'; exe.write_bytes(b'fake native program')
    body = settings(tmp, agents={'codex':{'enabled':True,'path':str(exe),'config_dir':str(tmp)}})
    assert post(client, '/api/desktop/settings', body).status_code == 200
    assert len(calls) == 1
    adapter = client.app.state.worker.adapter.for_agent('codex')
    assert adapter.identity_matches()[0]
    exe.write_bytes(b'changed program with different bytes')
    assert not adapter.identity_matches()[0]
    assert adapter.transport_factory.supervised_problems()
    profile = desktop.load()
    assert profile['agents']['codex']['sha256'] == hashlib.sha256(b'fake native program').hexdigest()


def test_deepseek_key_not_returned_and_only_selected_channel_enabled(release):
    client, tmp = release; pair(client)
    key = 'synthetic-test-key-never-real'
    body = settings(tmp, agents={'deepseek':{'enabled':True,'api_key':key}})
    assert post(client, '/api/desktop/settings', body).status_code == 200
    response = client.get('/api/desktop/settings')
    assert key not in response.text
    assert key not in (desktop.home()/'profile.json').read_text()
    assert key not in (desktop.home()/'adoptions.jsonl').read_text()
    assert client.app.state.worker.adapter.AGENT_IDS == frozenset({'deepseek'})


def test_runtime_changes_refused_while_call_running(release):
    client, tmp = release; pair(client)
    post(client, '/api/desktop/settings', settings(tmp))
    with client.app.state.db.session() as session:
        svc=PolicyService(session); task=svc.create_task(agent_id='codex',goal='pending')
        inv=svc.create_invocation(task_id=task.id,snapshot=InvocationSnapshot(agent_id='codex',task_input='read',parent_task_id=None,depth=0))
        inv.status='running'
    assert post(client, '/api/desktop/settings', settings(tmp)).status_code == 409


def test_codex_unlimited_is_approved_and_not_reaped_by_old_tree_limit(release):
    client,tmp=release; pair(client)
    body=settings(tmp,codex_timeout_seconds=600)
    assert post(client,'/api/desktop/settings',body).status_code==200
    with client.app.state.db.session() as session:
        svc=PolicyService(session); task=svc.create_task(agent_id='codex',goal='long task')
        snap=InvocationSnapshot(agent_id='codex',task_input='work',parent_task_id=None,depth=0)
        old=svc.create_invocation(task_id=task.id,snapshot=snap)
        approval=svc.create_approval(invocation_id=old.id)
        old_id,approval_id,task_id=old.id,approval.id,task.id
        assert old.snapshot['permissions']['execution_timeout_seconds']==600
    body['codex_timeout_seconds']=0
    assert post(client,'/api/desktop/settings',body).status_code==200
    assert client.get('/api/desktop/settings').json()['codex_timeout_seconds']==0
    with client.app.state.db.session() as session:
        svc=PolicyService(session)
        assert session.get(ApprovalRow,approval_id).status=='revoked'
        old=session.get(InvocationRow,old_id)
        fresh=svc.create_invocation(task_id=task_id,snapshot=snap)
        assert fresh.snapshot['permissions']['execution_timeout_seconds']==0
        assert fresh.payload_hash!=old.payload_hash
        fresh.status='running'; fresh.started_at=now()-dt.timedelta(hours=2)
        assert svc.list_overdue()==[]
    assert client.app.state.worker.reap_timeouts()['examined']==0


def test_custom_codex_limit_is_used_by_background_watchdog(release):
    client,tmp=release; pair(client)
    assert post(client,'/api/desktop/settings',settings(tmp,codex_timeout_seconds=3600)).status_code==200
    with client.app.state.db.session() as session:
        svc=PolicyService(session); task=svc.create_task(agent_id='codex',goal='long task')
        inv=svc.create_invocation(task_id=task.id,snapshot=InvocationSnapshot(
            agent_id='codex',task_input='work',parent_task_id=None,depth=0))
        inv.status='running'; inv.started_at=now()-dt.timedelta(minutes=30)
        assert svc.list_overdue()==[]
        inv.started_at=now()-dt.timedelta(hours=2)
        overdue=svc.list_overdue()
        assert len(overdue)==1 and overdue[0]['limit_seconds']==3600


@pytest.mark.parametrize('value',[-1,86401,True,1.5,'600',None])
def test_invalid_codex_deadline_does_not_write_profile(release,value):
    client,tmp=release; pair(client)
    assert post(client,'/api/desktop/settings',settings(tmp)).status_code==200
    before=(desktop.home()/'profile.json').read_bytes()
    assert post(client,'/api/desktop/settings',settings(tmp,codex_timeout_seconds=value)).status_code==422
    assert (desktop.home()/'profile.json').read_bytes()==before


def test_old_desktop_profile_without_a_limit_defaults_to_unlimited(release):
    client,tmp=release; pair(client)
    assert post(client,'/api/desktop/settings',settings(tmp)).status_code==200
    p=desktop.home()/'profile.json'; data=json.loads(p.read_text()); data.pop('codex_timeout_seconds'); data.pop('execution_timeout_seconds')
    p.write_text(json.dumps(data))
    assert client.get('/api/desktop/settings').json()['execution_timeout_seconds']==0


def test_explicit_053_limit_is_preserved_and_shared_by_new_calls(release):
    client,tmp=release; pair(client)
    assert post(client,'/api/desktop/settings',settings(tmp,codex_timeout_seconds=600)).status_code==200
    p=desktop.home()/'profile.json'; data=json.loads(p.read_text()); data.pop('execution_timeout_seconds')
    p.write_text(json.dumps(data))
    assert client.get('/api/desktop/settings').json()['execution_timeout_seconds']==600
    for agent in ['codex','claude-code','deepseek']:
        assert desktop.workspace_permissions(agent)['execution_timeout_seconds']==600


def test_user_can_settle_old_timeout_then_change_future_limit_without_restarting(release):
    client,tmp=release; pair(client)
    assert post(client,'/api/desktop/settings',settings(tmp,execution_timeout_seconds=600)).status_code==200
    output=tmp/'工作 项目'/'result.md'; output.write_text('existing work',encoding='utf-8')
    with client.app.state.db.session() as session:
        svc=PolicyService(session); task=svc.create_task(agent_id='codex',goal='long task')
        inv=svc.create_invocation(task_id=task.id,snapshot=InvocationSnapshot(
            agent_id='codex',task_input='work',parent_task_id=None,depth=0,
            policy_version=task.policy_version))
        approval=svc.create_approval(invocation_id=inv.id)
        svc.approve(approval.id); svc.consume(inv.id)
        svc.mark_unknown(inv.id,reason='no terminal turn event before the deadline')
        iid=inv.id
    body=settings(tmp,codex_timeout_seconds=0)
    assert post(client,'/api/desktop/settings',body).status_code==409
    route=f'/api/invocations/{iid}/reconcile'
    resolution={'action':'settle','outcome':'timed_out'}
    # The same user session and origin checks as the existing manual controls.
    assert client.post(route,json=resolution,headers={'Origin':'http://untrusted.invalid'}).status_code==403
    result=post(client,route,resolution)
    assert result.status_code==200 and result.json()['outcome']=='timed_out'
    assert post(client,route,resolution).status_code==409  # no double release
    assert post(client,'/api/desktop/settings',body).status_code==200
    with client.app.state.db.session() as session:
        rows=list(session.query(InvocationRow).all())
        assert len(rows)==1 and rows[0].status=='timed_out'
        assert rows[0].snapshot['permissions']['execution_timeout_seconds']==600
    assert output.read_text(encoding='utf-8')=='existing work'
    assert client.app.state.worker.inflight()==set()


@pytest.mark.parametrize('extra', [{'confirm':False},{'workspace_access':'danger-full-access'},
    {'agents':{'deepseek':{'enabled':True,'api_key':'bad\x00key'}}},
    {'agents':{'deepseek':{'enabled':True,'api_key':'synthetic','base_url':'http://example.com'}}}])
def test_invalid_configuration_does_not_enable_channels(release, extra):
    client, tmp=release; pair(client)
    assert post(client,'/api/desktop/settings',settings(tmp,**extra)).status_code==422
    assert client.app.state.worker.adapter.AGENT_IDS == frozenset()
