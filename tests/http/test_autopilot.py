"""P4.6a: stage authorization and the scheduler (offline stubs only; no model, no real agent).

A stage is the user's one authorization: within its rules the scheduler
approves the calls a stage needs and decides tool requests; the execution gate
still checks every call and refuses a stage call once the stage is not active.
"""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from agenthub.stage import ToolRules, make_rules, parse_verdict, tool_decision
from agenthub_adapters.appserver_adapter import AppServerCodexAdapter
from agenthub_adapters.appserver_stub import InMemoryStubFactory
from agenthub_adapters.claude_code import (
    ClaudeCodeAdapter, FakeClaudeRunner, StubClaudeLauncher, result_object, scripted,
)
from agenthub_adapters.routing import RoutingAdapter
from agenthub_adapters.store import ExecutionStore
from agenthub_kernel.db import ApprovalRow, Database, InvocationRow, StageGrantRow, StageRunRow, ToolApprovalRow
from agenthub_rt.api import create_app
from tests.http.test_async_and_tool_approval import eventually
from tests.http.test_conversations import USER

PLAN_REPLY = "1. codex: 列出需求样例的功能\n2. claude-code: 审阅第 1 项的结果 (after 1)"
LAUNCHED = ("starting", "running", "succeeded", "failed", "unknown", "canceled", "timed_out")


def build(tmp_path, *, scenario="happy", verdict="PASS", autopilot_interval=None):
    def cc(argv, env, prompt):
        if "Propose a plan" in prompt:
            reply = PLAN_REPLY
        elif "VERDICT" in prompt:
            reply = f"VERDICT: {verdict}\nchecked against the criteria"
        else:
            reply = "CC-DONE"
        return scripted(result_object(reply=reply))(argv, env, prompt)

    factory = InMemoryStubFactory(scenario)
    router = RoutingAdapter({
        "codex": AppServerCodexAdapter(
            transport_factory=factory, workdir_root=str(tmp_path / "codex-runs"),
            store=ExecutionStore(str(tmp_path / "appserver.db"), table="appserver_executions")),
        "claude-code": ClaudeCodeAdapter(
            launcher=StubClaudeLauncher("success-usage"), workdir_root=str(tmp_path / "cc-runs"),
            store=ExecutionStore(str(tmp_path / "cc.db"), table="claude_code_executions"),
            runner=FakeClaudeRunner(cc)),
    })
    app = create_app(db=Database(f"sqlite:///{(tmp_path / 'hub.db').as_posix()}"), adapter=router,
                     autopilot_interval=autopilot_interval)
    return app, factory


@pytest.fixture
def make_hub(tmp_path):
    clients = []

    def _make(**kw):
        app, factory = build(tmp_path, **kw)
        client = TestClient(app)
        client.__enter__()
        clients.append(client)
        return client, factory

    yield _make
    for client in clients:
        client.__exit__(None, None, None)
        client.app.state.db.engine.dispose()


def conv(client):
    return client.post("/api/conversations", headers=USER, json={}).json()["conversation_id"]


def manual(client, cid, agent, key, mode="strict"):
    """One user-approved call that succeeds: what "proven first" asks for."""
    turn = client.post(f"/api/conversations/{cid}/invocations", headers=USER, json={
        "text": "hello", "agent_id": agent, "client_request_id": key, "execution_mode": mode}).json()
    body = {"accept_unmanaged_native_agents": True} if mode == "supervised" else None
    assert client.post(f"/api/approvals/{turn['approval_id']}/approve", headers=USER, json=body).status_code == 200
    r = client.post(f"/api/invocations/{turn['invocation_id']}/run", params={"wait": "true"}, headers=USER)
    assert r.json()["status"] == "succeeded", r.text
    return turn


def prove(client, cid, agents=("codex", "claude-code"), mode="strict"):
    for n, agent in enumerate(agents):
        manual(client, cid, agent, f"prove-{agent}-{n}", mode)


def start(client, cid, **kw):
    body = {"goal": "整理任务清单需求", "criteria": "列出三项功能", "execution_mode": "strict",
            "agents": ["codex", "claude-code"], "main_agent": "codex", **kw}
    return client.post(f"/api/conversations/{cid}/stages", headers=USER, json=body)


def stage_of(client, cid):
    return client.get(f"/api/conversations/{cid}/stage", headers=USER).json()["stage"]


def run_stage(client, cid, passes=40):
    autopilot, worker = client.app.state.autopilot, client.app.state.worker
    for _ in range(passes):
        autopilot.tick()
        assert worker.wait_idle(10)
        status = stage_of(client, cid)["status"]
        if status not in ("active", "repairing"):
            return status
    return status


def count(client, model, *where):
    with client.app.state.db.session() as s:
        return s.scalar(select(func.count()).select_from(model).where(*where)) or 0


def launched(client):
    return count(client, InvocationRow, InvocationRow.status.in_(LAUNCHED))


def save_plan(client, cid, items):
    root = client.get(f"/api/conversations/{cid}/messages", headers=USER).json()["root_task_id"]
    assert client.patch(f"/api/tasks/{root}/policy", headers=USER,
                        json={"collaboration_enabled": True}).status_code == 200
    r = client.post(f"/api/conversations/{cid}/plans", headers=USER, json={"items": items})
    assert r.status_code == 200, r.text


# --------------------------------------------------------------------------- #
# pure rules
# --------------------------------------------------------------------------- #


def cmd(*argv):
    return {"command_view": {"parsed": True, "argv": list(argv)}}


@pytest.mark.parametrize("prefixes, method, details, decision", [
    (("echo",), "item/commandExecution/requestApproval", cmd("echo", "stub"), "approved"),
    (("echo",), "item/commandExecution/requestApproval", cmd("echoes", "x"), "declined"),  # word boundary
    (("echo",), "item/commandExecution/requestApproval", cmd("echo", "a;", "del", "x"), "declined"),
    (("git",), "item/commandExecution/requestApproval", cmd("git", "push", "origin"), "declined"),
    (("git push",), "item/commandExecution/requestApproval", cmd("git", "push", "origin"), "approved"),
    (("Get-ChildItem",), "item/commandExecution/requestApproval",
     {"command_view": {"parsed": True, "shell": "powershell.exe", "inner_command": "Get-ChildItem ."}}, "approved"),
    (("echo",), "item/commandExecution/requestApproval", {"command_view": {"parsed": False}}, "declined"),
    ((), "item/commandExecution/requestApproval", cmd("echo", "stub"), "declined"),
    (("echo",), "item/fileChange/requestApproval", {}, "declined"),
    (("echo",), "item/mcpToolCall/requestApproval", {}, "declined"),
])
def test_tool_rules_allow_only_what_is_named(prefixes, method, details, decision):
    assert tool_decision(ToolRules(command_prefixes=prefixes), method, details)[0] == decision


def test_file_changes_need_their_rule_and_root_widening_never_passes():
    rules = ToolRules(allow_file_changes=True)
    assert tool_decision(rules, "item/fileChange/requestApproval", {})[0] == "approved"
    assert tool_decision(rules, "item/fileChange/requestApproval", {"grantRoot": "C:\\"})[0] == "declined"


@pytest.mark.parametrize("data", [
    {"agents": [], "main_agent": "codex"},
    {"agents": ["codex"], "main_agent": "claude-code"},
    {"agents": ["codex"], "main_agent": "codex", "review_agent": "claude-code"},
    {"agents": ["codex"], "main_agent": "codex", "max_calls": 0},
    {"agents": ["codex"], "main_agent": "codex", "command_prefixes": ["dir; del x"]},
    {"agents": ["codex"], "main_agent": "codex", "plan": "whatever"},
    {"agents": ["ghost"], "main_agent": "ghost"},
])
def test_invalid_rules_are_refused(data):
    with pytest.raises(ValueError):
        make_rules(data, served=("claude-code", "codex"))


def test_verdict_is_read_from_the_first_line_only():
    assert parse_verdict("VERDICT: PASS\nok") == "PASS"
    assert parse_verdict("\n  verdict: fail — missing") == "FAIL"
    assert parse_verdict("Looks fine.\nVERDICT: PASS") is None


# --------------------------------------------------------------------------- #
# authorization
# --------------------------------------------------------------------------- #


def test_a_stage_needs_a_user_approved_success_per_agent_and_mode(make_hub):
    client, _ = make_hub()
    cid = conv(client)
    r = start(client, cid)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "stage_not_proven"
    prove(client, cid, agents=("codex",))
    r = start(client, cid)
    assert r.status_code == 409 and "claude-code" in r.json()["detail"]["message"]
    prove(client, cid, agents=("claude-code",))
    r = start(client, cid, execution_mode="supervised", supervised_consent=True)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "stage_not_proven"  # proven strict only
    r = start(client, cid, execution_mode="supervised")
    assert r.status_code == 409 and r.json()["detail"]["code"] == "boundary_consent_required"
    assert count(client, StageRunRow) == 0


def test_a_single_agent_stage_runs_without_clicks_and_ends_completed(make_hub):
    client, factory = make_hub()
    cid = conv(client)
    prove(client, cid, agents=("codex",))
    before = len(factory.servers)
    r = start(client, cid, agents=["codex"])
    assert r.status_code == 200, r.text
    assert run_stage(client, cid) == "completed"
    stage = stage_of(client, cid)
    assert [s["step"] for s in stage["steps"]] == ["main"] and len(factory.servers) == before + 1
    summary = stage["summary"]
    assert summary["kind"] == "completed" and summary["result_message_id"] and "不会自动开始" in summary["next"]
    msgs = client.get(f"/api/conversations/{cid}/messages", headers=USER).json()["messages"]
    card = next(m["invocation"] for m in msgs if m["invocation"] and
                m["invocation"]["invocation_id"] == stage["steps"][0]["invocation_id"])
    assert card["authorized_by"]["kind"] == "stage" and card["approval_status"] == "consumed"
    calls = count(client, InvocationRow)
    run_stage(client, cid, passes=3)  # nothing more: the stage is over, no next stage begins
    assert count(client, InvocationRow) == calls and count(client, StageRunRow) == 1


def test_a_plan_stage_dispatches_by_dependency_reviews_and_collects(make_hub):
    client, _ = make_hub()
    cid = conv(client)
    prove(client, cid)
    save_plan(client, cid, [{"agent_id": "codex", "goal": "列出功能"},
                            {"agent_id": "claude-code", "goal": "审阅第 1 项", "depends_on": [1]}])
    r = start(client, cid, plan="current", review_agent="claude-code", collect_agent="claude-code")
    assert r.status_code == 200, r.text
    assert run_stage(client, cid) == "completed"
    stage = stage_of(client, cid)
    assert [s["step"] for s in stage["steps"]] == ["item:1", "item:2", "review", "collect"]
    assert stage["summary"]["review"] == "PASS" and stage["summary"]["calls"] == 4
    with client.app.state.db.session() as s:
        item2 = s.get(InvocationRow, stage["steps"][1]["invocation_id"])
        item1_reply = stage["steps"][0]["invocation_id"]
        assert item2.snapshot["depth"] == 1 and len(item2.snapshot["handoff"]) == 1
        grants = s.scalars(select(StageGrantRow)).all()
        assert {g.invocation_id for g in grants} == {x["invocation_id"] for x in stage["steps"]}
        assert all(s.get(ApprovalRow, g.approval_id).status == "consumed" for g in grants)
        assert item1_reply
    plan = client.get(f"/api/conversations/{cid}/plans", headers=USER).json()["plans"][0]
    assert plan["all_done"]


def test_the_main_agent_can_propose_the_plan_within_the_rules(make_hub):
    client, _ = make_hub()
    cid = conv(client)
    prove(client, cid)
    r = start(client, cid, plan="agent", main_agent="claude-code", collect_agent="claude-code")
    assert r.status_code == 200, r.text
    assert run_stage(client, cid) == "completed"
    stage = stage_of(client, cid)
    assert [s["step"] for s in stage["steps"]] == ["plan", "item:1", "item:2", "collect"]
    plan = client.get(f"/api/conversations/{cid}/plans", headers=USER).json()
    assert plan["collaboration_enabled"] and plan["plans"][0]["source"].startswith("reply:")


def test_a_failed_review_ends_the_stage_blocked_with_what_is_needed(make_hub):
    client, _ = make_hub(verdict="FAIL")
    cid = conv(client)
    prove(client, cid)
    assert start(client, cid, agents=["codex", "claude-code"], review_agent="claude-code", retry_limit=0).status_code == 200
    assert run_stage(client, cid) == "blocked"
    summary = stage_of(client, cid)["summary"]
    assert summary["kind"] == "blocked" and summary["step"] == "review"
    assert summary["reason"] == "review_failed" and summary["needs"] and summary["done"] == ["main"]


def test_the_call_limit_of_the_rules_blocks_instead_of_raising_it(make_hub):
    client, _ = make_hub()
    cid = conv(client)
    prove(client, cid)
    assert start(client, cid, review_agent="claude-code", max_calls=1).status_code == 200
    assert run_stage(client, cid) == "blocked"
    summary = stage_of(client, cid)["summary"]
    assert summary["reason"] == "max_calls_reached" and summary["calls"] == 1


def test_pause_and_stop_keep_the_adapter_idle(make_hub):
    client, factory = make_hub()
    cid = conv(client)
    prove(client, cid, agents=("codex",))
    sid = start(client, cid, agents=["codex"]).json()["stage_id"]
    assert client.post(f"/api/stages/{sid}/pause", headers=USER).status_code == 200
    calls = count(client, InvocationRow)
    client.app.state.autopilot.tick()
    assert count(client, InvocationRow) == calls  # paused: nothing new

    # Approved under the stage, not yet started; then paused: the gate refuses it.
    assert client.post(f"/api/stages/{sid}/resume", headers=USER).status_code == 200
    autopilot = client.app.state.autopilot
    created = autopilot._advance_one(sid)
    assert len(created) == 1
    assert client.post(f"/api/stages/{sid}/pause", headers=USER).status_code == 200
    servers = len(factory.servers)
    r = client.post(f"/api/invocations/{created[0]}/run", params={"wait": "true"}, headers=USER)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "stage_not_active"
    assert len(factory.servers) == servers

    # Stopped: its unstarted approval is revoked; it never starts and never resumes.
    r = client.post(f"/api/stages/{sid}/stop", headers=USER)
    assert r.status_code == 200 and r.json()["status"] == "stopped"
    with client.app.state.db.session() as s:
        assert s.scalar(select(ApprovalRow.status).where(ApprovalRow.invocation_id == created[0])) == "revoked"
    autopilot.tick()
    assert len(factory.servers) == servers and stage_of(client, cid)["status"] == "stopped"
    assert client.post(f"/api/stages/{sid}/resume", headers=USER).status_code == 409


def test_one_open_stage_per_tree_and_only_the_owner_changes_it(make_hub):
    client, _ = make_hub()
    cid = conv(client)
    prove(client, cid, agents=("codex",))
    sid = start(client, cid, agents=["codex"]).json()["stage_id"]
    r = start(client, cid, agents=["codex"])
    assert r.status_code == 409 and r.json()["detail"]["code"] == "stage_in_progress"
    assert client.post("/api/stages/stage-missing/pause", headers=USER).status_code == 404
    assert client.post(f"/api/stages/{sid}/explode", headers=USER).status_code == 422


# --------------------------------------------------------------------------- #
# tool requests
# --------------------------------------------------------------------------- #


def prove_with_a_tool_card(client, cid):
    """The approval-allow stub asks for a command on every call: the user answers it once."""
    turn = client.post(f"/api/conversations/{cid}/invocations", headers=USER, json={
        "text": "hello", "agent_id": "codex", "client_request_id": "prove-tool"}).json()
    client.post(f"/api/approvals/{turn['approval_id']}/approve", headers=USER)
    client.post(f"/api/invocations/{turn['invocation_id']}/run", headers=USER)
    card = eventually(lambda: (client.get(f"/api/conversations/{cid}/tool-approvals", params={"status": "pending"},
                                          headers=USER).json()["tool_approvals"] or [None])[0])
    client.post(f"/api/tool-approvals/{card['tool_approval_id']}/approve", headers=USER,
                json={"card_hash": card["card_hash"]})
    assert client.app.state.worker.wait_idle(10)


@pytest.mark.parametrize("prefixes, decision", [(["echo"], "approved"), ([], "declined")])
def test_tool_requests_follow_the_stage_rules_without_waiting(make_hub, prefixes, decision):
    client, factory = make_hub(scenario="approval-allow")
    cid = conv(client)
    prove_with_a_tool_card(client, cid)
    r = start(client, cid, agents=["codex"], command_prefixes=prefixes, retry_limit=0)
    assert r.status_code == 200, r.text
    # This stub ends the turn without a final message either way, so the stage
    # ends "no_result"; what matters here is that nobody had to click.
    assert run_stage(client, cid) == "blocked" and stage_of(client, cid)["summary"]["reason"] == "no_result"
    stage = stage_of(client, cid)
    with client.app.state.db.session() as s:
        card = s.scalars(select(ToolApprovalRow).where(
            ToolApprovalRow.invocation_id == stage["steps"][0]["invocation_id"])).one()
        assert card.status == decision and card.decided_by == f"stage:{stage['stage_id']}"
        assert card.decision_sent == ("accept" if decision == "approved" else "decline")
    assert any(e["kind"] == f"tool_{decision}" and e["detail"]["rule"] for e in stage["events"])
    view = client.get(f"/api/tool-approvals/{card.id}", headers=USER).json()
    assert view["auto"]["decision"] == decision
