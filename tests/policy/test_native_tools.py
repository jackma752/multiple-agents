"""Native hook security tests: no model or CLI, real SQLite and loopback HTTP."""
import datetime as dt
import json
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import select

from agenthub.contracts import InvocationSnapshot, Policy
from agenthub_adapters.claude_hooks import ClaudeHookBridge
from agenthub_kernel.db import Database, NativeAgentRow, TaskRow, ToolApprovalRow, TreeQuotaRow
from agenthub_kernel.service import PolicyService, now
from agenthub_kernel.tool_approvals import ToolGate, card_hash, close_decisions, decide


def fixture_gate(tmp_path, *, enabled=True, writable=True, total=1, ttl=3, adapter_version="unknown", consume_main=True):
    workspace = tmp_path / "workspace"
    cwd = tmp_path / "cwd"
    workspace.mkdir(exist_ok=True)
    cwd.mkdir(exist_ok=True)
    db = Database("sqlite:///" + str(tmp_path / "native.db"))
    db.create_all()
    with db.session() as s:
        svc = PolicyService(s, Policy(collaboration_enabled=True, max_subagents_total=total,
                                     max_subagents_concurrent=1, max_depth=1))
        task = svc.create_task(agent_id="claude-code", goal="synthetic")
        snap = InvocationSnapshot(agent_id="claude-code", task_input="synthetic", parent_task_id=None,
            depth=0, policy_version=task.policy_version, adapter_version=adapter_version, permissions={"workspace": str(workspace),
            "workspace_access": "workspace-write" if writable else "read-only",
            "native_subagents": "approved" if enabled else "disabled"})
        inv = svc.create_invocation(task_id=task.id, snapshot=snap)
        appr = svc.create_approval(invocation_id=inv.id)
        svc.approve(appr.id)
        if consume_main:
            svc.consume(inv.id)
            inv.status = "running"
        invocation_id, root_id = inv.id, task.id
    gate = ToolGate(db, invocation_id, ttl_seconds=ttl)
    gate.bind_native(str(cwd))
    return gate, workspace, cwd, root_id


def event(gate, workspace, *, key="tool-1", tool="Write", agent_id=None, **args):
    if tool == "Agent":
        fields = {"prompt": "synthetic child", "description": "test", "subagent_type": "general-purpose"}
    else:
        fields = {"file_path": str(workspace / "result.txt"), "content": "SYNTHETIC"}
    fields.update(args)
    return {"session_id": "test", "cwd": gate.native_workdir, "hook_event_name": "PreToolUse",
            "tool_use_id": key, "tool_name": tool, "tool_input": fields,
            **({"agent_id": agent_id} if agent_id else {})}


def decision(result):
    return result["hookSpecificOutput"]["permissionDecision"]


def wait_card(gate, key):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        with gate.db.session() as s:
            row = s.scalar(select(ToolApprovalRow).where(ToolApprovalRow.request_key == "claude:" + key))
            if row is not None:
                return row.id
        time.sleep(.01)
    raise AssertionError("card did not appear")


def approve_card(gate, card):
    with gate.db.session() as s:
        row = s.get(ToolApprovalRow, card)
        decide(s, card, decision="approved", actor="synthetic-user", presented_hash=card_hash(row))


def authorized(gate, ev):
    with ThreadPoolExecutor(1) as pool:
        future = pool.submit(gate.native_event, ev)
        card = wait_card(gate, ev["tool_use_id"])
        approve_card(gate, card)
        return future.result(timeout=4)


@pytest.mark.parametrize("change", [
    {"tool": "Bash"}, {"file_path": "relative.txt"}, {"agent_id": "unapproved-child"},
])
def test_unsupported_requests_create_no_card(tmp_path, change):
    gate, workspace, _, _ = fixture_gate(tmp_path)
    assert decision(gate.native_event(event(gate, workspace, **change))) == "deny"
    assert not gate.opened


def test_workspace_and_configuration_cannot_be_escaped(tmp_path):
    gate, workspace, _, _ = fixture_gate(tmp_path)
    for path in (tmp_path / "outside.txt", workspace / ".claude" / "settings.json",
                 workspace / ".git" / "config"):
        assert decision(gate.native_event(event(gate, workspace, file_path=str(path)))) == "deny"
    assert not gate.opened


def test_read_only_is_backend_enforced(tmp_path):
    gate, workspace, _, _ = fixture_gate(tmp_path, writable=False)
    assert decision(gate.native_event(event(gate, workspace))) == "deny"
    assert not gate.opened


def test_approved_file_request_is_once_only_and_complete(tmp_path):
    gate, workspace, _, _ = fixture_gate(tmp_path)
    ev = event(gate, workspace)
    assert decision(authorized(gate, ev)) == "allow"
    assert decision(gate.native_event(ev)) == "deny"
    assert len(gate.opened) == 1
    with gate.db.session() as s:
        row = s.get(ToolApprovalRow, gate.opened[0])
        assert row.details["native_hook"] == ev
        assert row.delivery == "sending"  # not proof the HTTP response was delivered


def test_unapproved_request_times_out_without_allow(tmp_path):
    gate, workspace, _, _ = fixture_gate(tmp_path, ttl=.1)
    assert decision(gate.native_event(event(gate, workspace))) == "deny"


@pytest.mark.parametrize("change", ["close", "policy", "expired"])
def test_late_approval_never_grants_execution(tmp_path, change):
    gate, workspace, _, root = fixture_gate(tmp_path)
    ev = event(gate, workspace)
    with ThreadPoolExecutor(1) as pool:
        future = pool.submit(gate.native_event, ev)
        card = wait_card(gate, "tool-1")
        # Put the decision on the card and change the effective execution
        # state in the SAME transaction, before the polling thread can claim.
        with gate.db.session() as s:
            row = s.get(ToolApprovalRow, card)
            decide(s, card, decision="approved", actor="user", presented_hash=card_hash(row))
            if change == "close":
                close_decisions(s, gate.invocation_id, reason="stopped", actor="user")
            elif change == "policy":
                s.get(TaskRow, root).policy_version += 1
            else:
                row.expires_at = now() - dt.timedelta(seconds=1)
        assert decision(future.result(timeout=4)) == "deny"


def test_native_disabled_is_not_just_a_prompt(tmp_path):
    gate, workspace, _, _ = fixture_gate(tmp_path, enabled=False)
    assert decision(gate.native_event(event(gate, workspace, tool="Agent"))) == "deny"
    assert not gate.opened


def test_native_total_shared_with_hub_and_terminal_is_idempotent(tmp_path):
    gate, workspace, _, root = fixture_gate(tmp_path)
    ev = event(gate, workspace, tool="Agent")
    assert decision(authorized(gate, ev)) == "allow"
    gate.native_event({"hook_event_name": "SubagentStart", "agent_id": "child-1", "agent_type": "general-purpose"})
    with gate.db.session() as s:
        quota = s.get(TreeQuotaRow, root)
        assert (quota.launched_total, quota.concurrent, quota.total_invocations) == (1, 1, 2)
        svc = PolicyService(s)
        child_task = svc.create_task(agent_id="mock", goal="hub child", parent_task_id=root)
        hub_inv = svc.create_invocation(task_id=child_task.id, snapshot=InvocationSnapshot(
            agent_id="mock", task_input="child", parent_task_id=root, depth=1, policy_version=1))
        appr = svc.create_approval(invocation_id=hub_inv.id)
        svc.approve(appr.id)
        from agenthub_kernel.service import QuotaError
        with pytest.raises(QuotaError):
            svc.consume(hub_inv.id)
    # The child's own operations still need new user approvals.
    assert decision(authorized(gate, event(gate, workspace, key="child-tool", agent_id="child-1"))) == "allow"
    for _ in range(2):
        gate.native_event({"hook_event_name": "SubagentStop", "agent_id": "child-1"})
    with gate.db.session() as s:
        quota = s.get(TreeQuotaRow, root)
        assert (quota.launched_total, quota.concurrent) == (1, 0)
    # Completing a child never refunds its cumulative total.
    assert decision(authorized(gate, event(gate, workspace, key="second", tool="Agent"))) == "deny"


@pytest.mark.parametrize("extra", [{"resume": "old"}, {"run_in_background": True}, {"isolation": "worktree"}])
def test_background_and_resume_cannot_bypass_counting(tmp_path, extra):
    gate, workspace, _, _ = fixture_gate(tmp_path)
    assert decision(gate.native_event(event(gate, workspace, tool="Agent", **extra))) == "deny"
    assert not gate.opened


def test_http_auth_session_cwd_and_failure_closed(tmp_path):
    gate, workspace, cwd, _ = fixture_gate(tmp_path)
    with ClaudeHookBridge(gate, cwd=str(cwd), config_root=str(tmp_path / "settings"), permissions={}) as bridge:
        ev = {**event(gate, workspace, tool="Bash"), "session_id": bridge.session_id}
        def post(body, token):
            req = urllib.request.Request(bridge.url, data=json.dumps(body).encode(),
                headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"})
            return urllib.request.urlopen(req, timeout=3)
        with pytest.raises(urllib.error.HTTPError) as error:
            post(ev, "wrong")
        assert error.value.code == 403
        with pytest.raises(urllib.error.HTTPError):
            post({**ev, "session_id": "other"}, bridge.token)
        with pytest.raises(urllib.error.HTTPError):
            post({**ev, "cwd": str(tmp_path)}, bridge.token)
        with post(ev, bridge.token) as response:
            assert decision(json.load(response)) == "deny"
        for groups in bridge.settings()["hooks"].values():
            assert groups[0]["hooks"][0]["onFailure"] == "block"
        assert "--safe-mode" not in bridge.argv()
        settings_path = bridge.settings_path
    import os
    assert not os.path.exists(settings_path)


def test_parallel_native_launches_take_at_most_one_slot(tmp_path):
    gate, workspace, _, root = fixture_gate(tmp_path)
    with ThreadPoolExecutor(2) as pool:
        futures = [pool.submit(gate.native_event, event(gate, workspace, key=k, tool="Agent")) for k in ("a", "b")]
        cards = [wait_card(gate, k) for k in ("a", "b")]
        from sqlalchemy import text
        with gate.db.session() as s:
            s.execute(text("BEGIN IMMEDIATE"))
            for card in cards:
                row = s.get(ToolApprovalRow, card)
                decide(s, card, decision="approved", actor="user", presented_hash=card_hash(row), already_locked=True)
        assert sorted(decision(f.result(4)) for f in futures) == ["allow", "deny"]
    with gate.db.session() as s:
        quota = s.get(TreeQuotaRow, root)
        assert (quota.launched_total, quota.concurrent) == (1, 1)


def test_unknown_end_keeps_native_reservation(tmp_path):
    gate, workspace, _, root = fixture_gate(tmp_path)
    assert decision(authorized(gate, event(gate, workspace, tool="Agent"))) == "allow"
    gate.native_event({"hook_event_name": "PostToolUseFailure", "tool_name": "Agent", "tool_use_id": "tool-1"})
    assert gate.native_unfinished()
    with gate.db.session() as s:
        assert s.get(TreeQuotaRow, root).concurrent == 1
        assert s.scalar(select(NativeAgentRow)).state == "unknown"


def test_strict_token_budget_does_not_claim_native_metering(tmp_path):
    gate, workspace, _, root = fixture_gate(tmp_path)
    with gate.db.session() as s:
        task = s.get(TaskRow, root)
        task.policy = {**task.policy, "token_budget_total": 100, "token_budget_per_call": 10}
    assert decision(gate.native_event(event(gate, workspace, tool="Agent"))) == "deny"
    assert not gate.opened


def test_stage_file_rule_cannot_authorize_agent_or_shell():
    from agenthub.stage import ToolRules, tool_decision
    rules = ToolRules(allow_file_changes=True)
    for tool, expected in (("Write", "approved"), ("Edit", "approved"), ("Agent", "declined"),
                           ("Bash", "declined"), ("Read", "declined")):
        assert tool_decision(rules, "claude/PreToolUse", {"native_hook": {"tool_name": tool}})[0] == expected


def test_empty_gate_response_is_explicitly_denied(tmp_path, monkeypatch):
    gate, workspace, cwd, _ = fixture_gate(tmp_path)
    monkeypatch.setattr(gate, "native_event", lambda _ev: {})
    with ClaudeHookBridge(gate, cwd=str(cwd), config_root=str(tmp_path / "settings"), permissions={}) as bridge:
        ev = {**event(gate, workspace), "session_id": bridge.session_id}
        req = urllib.request.Request(bridge.url, data=json.dumps(ev).encode(),
            headers={"Authorization": "Bearer " + bridge.token, "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=3) as response:
            assert decision(json.load(response)) == "deny"


def test_codex_disabled_readback_requires_boolean_false():
    from agenthub_adapters.appserver_adapter import verify_native_disabled
    assert verify_native_disabled({"config": {"agents": {"enabled": False}}})
    for value in ({}, {"config": {}}, {"config": {"agents": {"enabled": 0}}},
                  {"config": {"agents": {"enabled": "false"}}}, {"config": {"agents": {"enabled": True}}}):
        assert not verify_native_disabled(value)


def test_explicit_supervised_mode_checks_its_own_conditions():
    from agenthub.contracts import ExecutionAdmission, execution_admission
    class Both:
        def execution_admission(self):
            return ExecutionAdmission(True, "strict", "scoped", "file tools")

        def supervised_execution_admission(self):
            return ExecutionAdmission(False, "blocked", "supervised_conditions", "different argv")
    assert execution_admission(Both(), "strict").allowed
    assert not execution_admission(Both(), "supervised").allowed


def test_native_lifecycle_events_contain_no_tool_content(tmp_path):
    from agenthub_kernel.db import ChatMessageRow, ConversationEventRow, ConversationRow
    gate, workspace, _, root = fixture_gate(tmp_path)
    with gate.db.session() as s:
        s.add(ConversationRow(id="native-c", user_id="u", title="synthetic", root_task_id=root))
        s.flush()
        s.add(ChatMessageRow(conversation_id="native-c", role="user", text="synthetic", invocation_id=gate.invocation_id))
    assert decision(authorized(gate, event(gate, workspace, tool="Agent"))) == "allow"
    gate.native_event({"hook_event_name": "SubagentStart", "agent_id": "child-1", "agent_type": "general-purpose"})
    gate.native_event({"hook_event_name": "SubagentStop", "agent_id": "child-1"})
    with gate.db.session() as s:
        rows = s.scalars(select(ConversationEventRow).where(ConversationEventRow.type == "native_agent.status").order_by(ConversationEventRow.seq)).all()
        assert [r.payload["state"] for r in rows] == ["reserved", "running", "finished"]
        assert all(set(r.payload) == {"invocation_id", "native_agent_id", "tool_approval_id", "state", "depth"} for r in rows)


@pytest.mark.parametrize("reason", ["collaboration", "budget"])
def test_impossible_native_choice_starts_zero_main_models(tmp_path, reason):
    from agenthub_adapters.claude_code import ClaudeCodeAdapter, FakeClaudeRunner, StubClaudeLauncher, result_object, scripted
    from agenthub_kernel.db import ApprovalRow
    from agenthub_kernel.worker import Worker
    adapter = ClaudeCodeAdapter(launcher=StubClaudeLauncher("success-usage"),
        runner=FakeClaudeRunner(scripted(result_object())), workdir_root=str(tmp_path / "runs"))
    gate, _, _, root = fixture_gate(tmp_path, adapter_version=adapter.binding_identity(), consume_main=False)
    with gate.db.session() as s:
        task = s.get(TaskRow, root)
        change = {"collaboration_enabled": False} if reason == "collaboration" else {"token_budget_total": 100, "token_budget_per_call": 10}
        task.policy = {**task.policy, **change}
    result = Worker(gate.db, adapter).run_one(gate.invocation_id)
    assert result["code"] == ("native_collaboration_disabled" if reason == "collaboration" else "native_budget_unmetered")
    assert not adapter.runner.calls
    with gate.db.session() as s:
        assert s.get(TreeQuotaRow, root).total_invocations == 0
        assert s.scalar(select(ApprovalRow)).status == "approved"


def test_native_choice_is_bound_in_both_hashes():
    from agenthub.chat import ConversationTurn
    closed = ConversationTurn(text="test", agent_id="claude-code", client_request_id="k")
    opened = ConversationTurn(text="test", agent_id="claude-code", client_request_id="k", native_subagents="approved")
    assert closed.request_hash() != opened.request_hash()
    a = InvocationSnapshot(agent_id="claude-code", task_input="test", parent_task_id=None, depth=0)
    b = InvocationSnapshot(agent_id="claude-code", task_input="test", parent_task_id=None, depth=0,
                           permissions={"native_subagents": "approved"})
    assert a.payload_hash() != b.payload_hash()
