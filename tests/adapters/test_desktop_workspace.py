"""Workspace wire/cwd checks with local protocol peers, never real agents."""
import dataclasses
import os
import pytest
from agenthub.contracts import InvocationSnapshot
from agenthub.workspace import approved_workspace
from agenthub_adapters.appserver_adapter import AppServerCodexAdapter, settings_problems
from agenthub_adapters.appserver_stub import InMemoryStubFactory
from agenthub_adapters.claude_code import ClaudeCodeAdapter, StubClaudeLauncher, FakeClaudeRunner, scripted, result_object


def snap(**kw):
    return InvocationSnapshot(agent_id='codex', task_input='inspect', parent_task_id=None, depth=0, **kw)


def test_codex_thread_and_turn_use_approved_workspace(tmp_path):
    workspace=tmp_path/'中文 project'; workspace.mkdir(); (workspace/'existing.txt').write_text('data')
    factory=InMemoryStubFactory('happy')
    adapter=AppServerCodexAdapter(transport_factory=factory,workdir_root=str(tmp_path/'runs'))
    result=adapter.start('inv-workspace',snap(adapter_version=adapter.binding_identity(),permissions={'workspace':str(workspace),'workspace_access':'read-only'}),gate_token='test')
    assert result.ok, result.detail
    sent=factory.servers[0].received
    thread=next(m['params'] for m in sent if m.get('method')=='thread/start')
    turn=next(m['params'] for m in sent if m.get('method')=='turn/start')
    assert thread['cwd']==str(workspace)
    assert thread['sandbox']=='read-only'
    assert turn['sandboxPolicy']['type']=='readOnly'
    assert (workspace/'existing.txt').read_text()=='data'


def test_writable_sandbox_readback_rejects_wider_roots_and_network(tmp_path):
    result={'cwd':str(tmp_path),'approvalPolicy':'on-request','approvalsReviewer':'user',
            'sandbox':{'type':'workspaceWrite','writableRoots':[str(tmp_path)],'networkAccess':False}}
    assert not settings_problems(result,str(tmp_path),writable=True)
    result['sandbox']['writableRoots']=[str(tmp_path.parent)]
    assert 'sandbox' in settings_problems(result,str(tmp_path),writable=True)
    result['sandbox']['writableRoots']=[str(tmp_path)]; result['sandbox']['networkAccess']=True
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
