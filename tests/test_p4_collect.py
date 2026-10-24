"""P4 run judge, offline: the planned real web run, on stubs.

Codex is the stdio app-server stub (replies "OK"); Claude Code an in-process
fake that answers the first code it finds inside a handoff frame. No Codex,
no Claude Code, no model.
"""
import importlib.util
from pathlib import Path

from fastapi.testclient import TestClient

from agenthub_adapters.appserver_adapter import AppServerCodexAdapter
from agenthub_adapters.appserver_stub import StdioStubFactory
from agenthub_adapters.claude_code import (
    ClaudeCodeAdapter, FakeClaudeRunner, StubClaudeLauncher, result_object, scripted,
)
from agenthub_adapters.routing import RoutingAdapter
from agenthub_adapters.store import ExecutionStore
from agenthub_kernel.db import Database
from agenthub_rt.api import create_app

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("p4_collect_under_test", ROOT / "tools/probes/p4_collect.py")
P4 = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(P4)

USER = {"Authorization": "Bearer dev-user-token"}
CODE = "OK"  # what the "happy" Codex stub replies
BINDINGS = {"codex_binding": "appserver-stub:stdio", "claude_binding": "claude-code-stub:stdio"}
GOAL1 = f"Reply with exactly: {CODE}. Do not use tools."
GOAL2 = "Reply with exactly the code that appears in the handoff above, and nothing else. Do not use tools."


def code_from_handoff(turns=1):
    def run(argv, env, prompt):
        lines = prompt.splitlines()
        marker = next((i for i, line in enumerate(lines) if line.startswith("--- Handoff: reply of")), None)
        reply = lines[marker + 1].strip() if marker is not None and marker + 1 < len(lines) else "NONE"
        return scripted({**result_object(reply=reply), "num_turns": turns})(argv, env, prompt)
    return run


def web_run(tmp_path, *, goal2=GOAL2, turns=1):
    """What the page does: opener, switch on, a two-item plan, dispatch #1, #2, collect."""
    router = RoutingAdapter({
        "codex": AppServerCodexAdapter(
            transport_factory=StdioStubFactory("happy"), workdir_root=str(tmp_path / "codex-runs"),
            store=ExecutionStore(str(tmp_path / "appserver-executions.db"), table="appserver_executions")),
        "claude-code": ClaudeCodeAdapter(
            launcher=StubClaudeLauncher("success-usage"), workdir_root=str(tmp_path / "cc-runs"),
            store=ExecutionStore(str(tmp_path / "claude-code-executions.db"), table="claude_code_executions"),
            runner=FakeClaudeRunner(code_from_handoff(turns))),
    })
    app = create_app(db=Database(f"sqlite:///{(tmp_path / 'agent-hub.db').as_posix()}"), adapter=router)
    consent = {"accept_unmanaged_native_agents": True}

    def approve_run(turn):
        assert client.post(f"/api/approvals/{turn['approval_id']}/approve", headers=USER, json=consent).status_code == 200
        assert client.post(f"/api/invocations/{turn['invocation_id']}/run", params={"wait": "true"},
                           headers=USER).status_code == 200

    with TestClient(app) as client:
        cid = client.post("/api/conversations", headers=USER, json={"title": "P4"}).json()["conversation_id"]
        client.post(f"/api/conversations/{cid}/invocations", headers=USER, json={
            "text": "start", "agent_id": "codex", "client_request_id": "opener", "execution_mode": "supervised"})
        root = client.get(f"/api/conversations/{cid}/messages", headers=USER).json()["root_task_id"]
        assert client.patch(f"/api/tasks/{root}/policy", headers=USER,
                            json={"collaboration_enabled": True}).status_code == 200
        plan = client.post(f"/api/conversations/{cid}/plans", headers=USER, json={"items": [
            {"agent_id": "codex", "goal": GOAL1},
            {"agent_id": "claude-code", "goal": goal2, "depends_on": [1]}]}).json()
        for index in (1, 2):
            approve_run(client.post(f"/api/conversations/{cid}/plans/{plan['plan_id']}/items/{index}/dispatch",
                                    headers=USER, json={"client_request_id": f"d{index}",
                                                        "execution_mode": "supervised"}).json())
        approve_run(client.post(f"/api/conversations/{cid}/plans/{plan['plan_id']}/collect", headers=USER, json={
            "agent_id": "claude-code", "client_request_id": "c", "execution_mode": "supervised"}).json())
    app.state.db.engine.dispose()
    return tmp_path


def test_a_clean_collaboration_run_passes(tmp_path):
    facts = P4.collect(web_run(tmp_path), code=CODE, **BINDINGS)
    assert facts["verdict"] == "PASS" and facts["reason_codes"] == []
    assert facts["calls_run"] == 3 and facts["attempts_per_item"] == [1, 1]
    assert facts["item2_handoff_is_item1_reply"] and facts["item2_sent_is_frame_plus_goal"]
    assert facts["collect_handoff_is_both_replies"] and facts["collect_reply_has_code"]
    assert GOAL2 not in str(facts)  # no prompt text is written
    assert facts["item2"]["num_turns"] == 1 and facts["item2"]["models"] == ["claude-model"]
    unreported = {**facts, "item2": {**facts["item2"], "num_turns": None}}
    assert P4.judge(unreported)[1] == ["item2_turns_not_one"]


def test_a_second_turn_is_its_own_anomaly_even_with_complete_usage(tmp_path):
    facts = P4.collect(web_run(tmp_path, turns=2), code=CODE, **BINDINGS)
    assert facts["item2"]["usage_complete"] and facts["collect"]["usage_complete"]
    assert facts["verdict"] == "INCONCLUSIVE"
    assert facts["reason_codes"] == ["item2_turns_not_one", "collect_turns_not_one"]


def test_a_code_typed_into_the_dependent_goal_proves_nothing(tmp_path):
    facts = P4.collect(web_run(tmp_path, goal2=f"The code is {CODE}; repeat it."), code=CODE, **BINDINGS)
    assert facts["verdict"] == "FAIL" and "code_outside_item1" in facts["reason_codes"]
