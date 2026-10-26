"""Release onboarding/auth/workspace tests, no native Agent or network inference."""
import dataclasses
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
from agenthub_kernel.service import PolicyService
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


@pytest.mark.parametrize('extra', [{'confirm':False},{'workspace_access':'danger-full-access'},
    {'agents':{'deepseek':{'enabled':True,'api_key':'bad\x00key'}}},
    {'agents':{'deepseek':{'enabled':True,'api_key':'synthetic','base_url':'http://example.com'}}}])
def test_invalid_configuration_does_not_enable_channels(release, extra):
    client, tmp=release; pair(client)
    assert post(client,'/api/desktop/settings',settings(tmp,**extra)).status_code==422
    assert client.app.state.worker.adapter.AGENT_IDS == frozenset()
