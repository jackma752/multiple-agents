"""Workspace wire/cwd checks with local protocol peers, never real agents."""
import dataclasses
import os
import pytest
from agenthub.contracts import InvocationSnapshot
from agenthub.workspace import approved_workspace
from agenthub_adapters.appserver_adapter import AppServerCodexAdapter, settings_problems
from agenthub_adapters import appserver_adapter
from agenthub_adapters.appserver_adapter import workspace_thread_config
from agenthub_adapters.appserver_stub import InMemoryStubFactory
from agenthub_adapters.claude_code import ClaudeCodeAdapter, StubClaudeLauncher, FakeClaudeRunner, scripted, result_object


def snap(**kw):
    return InvocationSnapshot(agent_id='codex', task_input='inspect', parent_task_id=None, depth=0, **kw)


@pytest.mark.parametrize('access', ['read-only', 'workspace-write'])
def test_codex_thread_and_turn_use_approved_workspace(tmp_path, access):
    workspace=tmp_path/'中文 project'; workspace.mkdir(); (workspace/'existing.txt').write_text('data')
    factory=InMemoryStubFactory('happy')
    adapter=AppServerCodexAdapter(transport_factory=factory,workdir_root=str(tmp_path/'runs'))
    result=adapter.start('inv-workspace',snap(adapter_version=adapter.binding_identity(),permissions={'workspace':str(workspace),'workspace_access':access}),gate_token='test')
    assert result.ok, result.detail
    sent=factory.servers[0].received
    thread=next(m['params'] for m in sent if m.get('method')=='thread/start')
    turn=next(m['params'] for m in sent if m.get('method')=='turn/start')
    assert thread['cwd']==str(workspace)
    assert thread['sandbox']==access
    assert turn['sandboxPolicy']['type']==('readOnly' if access=='read-only' else 'workspaceWrite')
    if access=='workspace-write':
        config=thread['config']
        assert config['sandbox_workspace_write.writable_roots']==[]
        assert config['sandbox_workspace_write.network_access'] is False
        assert config['sandbox_workspace_write.exclude_tmpdir_env_var'] is True
        assert config['sandbox_workspace_write.exclude_slash_tmp'] is True
        assert turn['sandboxPolicy']['writableRoots']==[str(workspace)]
    assert (workspace/'existing.txt').read_text()=='data'


def test_writable_resume_reapplies_workspace_policy(tmp_path):
    factory=InMemoryStubFactory('happy')
    adapter=AppServerCodexAdapter(transport_factory=factory,workdir_root=str(tmp_path/'runs'))
    snapshot=snap(adapter_version=adapter.binding_identity(),
                  permissions={'workspace':str(tmp_path),'workspace_access':'workspace-write'})
    first=adapter.start('inv-first',snapshot,gate_token='test')
    assert first.ok, first.detail
    resumed=dataclasses.replace(snapshot,context_mode='native_resume',resume_thread_id=first.adapter_invocation_id)
    second=adapter.start('inv-second',resumed,gate_token='test')
    assert second.ok, second.detail
    params=next(m['params'] for m in factory.servers[1].received if m.get('method')=='thread/resume')
    assert params['sandbox']=='workspace-write'
    assert params['config']['sandbox_workspace_write.exclude_tmpdir_env_var'] is True
    assert adapter.store.get('inv-second')['payload']['settings_readback']['sandbox']['type']=='workspaceWrite'


@pytest.mark.parametrize('configured', [None, 'unelevated', 'elevated', 'mxc'])
def test_windows_backend_default_preserves_existing_selection(tmp_path, monkeypatch, configured):
    monkeypatch.setattr(appserver_adapter,'IS_WINDOWS',True)
    class Client:
        def request(self,method,params):
            assert method=='config/read'
            assert params=={'includeLayers':False,'cwd':str(tmp_path)}
            return {'config':{'windows':{'sandbox':configured} if configured else None}}
    config=workspace_thread_config(Client(),str(tmp_path))
    if configured is None:
        assert config['windows.sandbox']=='unelevated'
    else:
        assert 'windows.sandbox' not in config


def test_other_platform_does_not_select_windows_backend(tmp_path, monkeypatch):
    monkeypatch.setattr(appserver_adapter,'IS_WINDOWS',False)
    class Client:
        def request(self,*args):
            pytest.fail('Windows backend lookup on another platform')
    assert 'windows.sandbox' not in workspace_thread_config(Client(),str(tmp_path))


def test_downgraded_writable_policy_refuses_before_turn_and_records_readback(tmp_path, monkeypatch):
    from agenthub_adapters.appserver_stub import ScriptedAppServer
    reply=ScriptedAppServer.reply
    def downgraded(self,request_id,result=None,error=None):
        if result and 'sandbox' in result:
            result={**result,'sandbox':{'type':'readOnly','networkAccess':False}}
        return reply(self,request_id,result,error)
    monkeypatch.setattr(ScriptedAppServer,'reply',downgraded)
    factory=InMemoryStubFactory('happy')
    adapter=AppServerCodexAdapter(transport_factory=factory,workdir_root=str(tmp_path/'runs'))
    result=adapter.start('inv-downgraded',snap(adapter_version=adapter.binding_identity(),
        permissions={'workspace':str(tmp_path),'workspace_access':'workspace-write'}),gate_token='test')
    assert not result.ok and 'appserver_settings_mismatch: sandbox' in result.detail
    assert not any(m.get('method')=='turn/start' for m in factory.servers[0].received)
    payload=adapter.store.get('inv-downgraded')['payload']
    assert payload['turn_requested'] is False
    assert payload['settings_readback']['sandbox']['type']=='readOnly'


def test_writable_sandbox_readback_rejects_wider_roots_and_network(tmp_path):
    result={'cwd':str(tmp_path),'approvalPolicy':'on-request','approvalsReviewer':'user',
            'sandbox':{'type':'workspaceWrite','writableRoots':[str(tmp_path)],'networkAccess':False}}
    result['sandbox'].update(excludeTmpdirEnvVar=True,excludeSlashTmp=True)
    assert not settings_problems(result,str(tmp_path),writable=True)
    result['sandbox']['writableRoots']=[str(tmp_path.parent)]
    assert 'sandbox' in settings_problems(result,str(tmp_path),writable=True)
    result['sandbox']['writableRoots']=[str(tmp_path)]; result['sandbox']['networkAccess']=True
    assert 'sandbox' in settings_problems(result,str(tmp_path),writable=True)


@pytest.mark.parametrize('extra', ['excludeTmpdirEnvVar','excludeSlashTmp'])
def test_writable_policy_refuses_implicit_temporary_write_roots(tmp_path, extra):
    result={'cwd':str(tmp_path),'approvalPolicy':'on-request','approvalsReviewer':'user',
            'sandbox':{'type':'workspaceWrite','writableRoots':[], 'networkAccess':False,
                       'excludeTmpdirEnvVar':True,'excludeSlashTmp':True}}
    assert not settings_problems(result,str(tmp_path),writable=True)
    result['sandbox'][extra]=False
    assert 'sandbox' in settings_problems(result,str(tmp_path),writable=True)


def test_claude_cwd_is_workspace_but_tools_stay_disabled(tmp_path):
    workspace=tmp_path/'project'; workspace.mkdir();(workspace/'file.txt').write_text('already here')
    runner=FakeClaudeRunner(scripted(result_object()))
    adapter=ClaudeCodeAdapter(launcher=StubClaudeLauncher('success-usage'),workdir_root=str(tmp_path/'runs'),runner=runner)
    snapshot=dataclasses.replace(snap(adapter_version=adapter.binding_identity(),permissions={'workspace':str(workspace),'workspace_access':'read-only'}),agent_id='claude-code')
    result=adapter.start('inv-cc-workspace',snapshot,gate_token='test')
    assert result.ok
    assert runner.calls[0]['cwd']==str(workspace)
    argv=runner.calls[0]['argv']; assert argv[argv.index('--tools')+1]==''


def test_removed_workspace_refused_before_start(tmp_path):
    snapshot=snap(permissions={'workspace':str(tmp_path/'missing'),'workspace_access':'read-only'})
    adapter=AppServerCodexAdapter(transport_factory=InMemoryStubFactory('happy'),workdir_root=str(tmp_path/'runs'))
    result=adapter.start('inv-no-directory',dataclasses.replace(snapshot,adapter_version=adapter.binding_identity()),gate_token='test')
    assert not result.ok and adapter.start_count==0
    assert 'workspace' in result.detail
