"""Two-agent run judge, offline: the same flow as the real web run, on stubs.

Codex is the stdio app-server stub (a real child process, so local cleanup can
be judged); Claude Code is an in-process fake whose reply is whatever code it
finds inside the handoff frame. No Codex, no Claude Code, no model.
"""
import importlib.util
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agenthub_adapters.appserver_adapter import AppServerCodexAdapter
from agenthub_adapters.appserver_stub import StdioStubFactory
from agenthub_adapters.claude_code import (
    ClaudeCodeAdapter, FakeClaudeRunner, StubClaudeLauncher, result_object, scripted,
)
from agenthub_adapters.routing import RoutingAdapter
from agenthub_adapters.store import ExecutionStore
from agenthub_kernel.db import Database, InvocationRow
from agenthub_rt.api import create_app

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("two_agent_collect_under_test", ROOT / "tools/probes/two_agent_collect.py")
TA = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(TA)

USER = {"Authorization": "Bearer dev-user-token"}
CODE = "OK"  # what the "happy" Codex stub replies
BINDINGS = {"codex_binding": "appserver-stub:stdio", "claude_binding": "claude-code-stub:stdio"}
ASK_CODEX = "Reply with exactly the word in this message's second sentence. OK."
ASK_CLAUDE = "Reply with exactly the code that appears in the handoff below, and nothing else."


def code_from_handoff(argv, env, prompt):
    lines = prompt.splitlines()
    marker = next((i for i, line in enumerate(lines) if line.startswith("--- Handoff: reply of")), None)
    reply = lines[marker + 1].strip() if marker is not None and marker + 1 < len(lines) else "NONE"
    return scripted(result_object(reply=reply))(argv, env, prompt)


def web_run(tmp_path, *, mode="supervised", handoff=True, claude_text=ASK_CLAUDE, second_conversation=True):
    """What the page does: two calls, a switch to another conversation and back, one handoff."""
    router = RoutingAdapter({
        "codex": AppServerCodexAdapter(
            transport_factory=StdioStubFactory("happy"), workdir_root=str(tmp_path / "codex-runs"),
            store=ExecutionStore(str(tmp_path / "appserver-executions.db"), table="appserver_executions")),
        "claude-code": ClaudeCodeAdapter(
            launcher=StubClaudeLauncher("success-usage"), workdir_root=str(tmp_path / "cc-runs"),
            store=ExecutionStore(str(tmp_path / "claude-code-executions.db"), table="claude_code_executions"),
            runner=FakeClaudeRunner(code_from_handoff)),
    })
    app = create_app(db=Database(f"sqlite:///{(tmp_path / 'agent-hub.db').as_posix()}"), adapter=router)
    consent = {"accept_unmanaged_native_agents": True} if mode == "supervised" else None

    def call(client, cid, agent, text, key, **extra):
        turn = client.post(f"/api/conversations/{cid}/invocations", headers=USER, json={
            "text": text, "agent_id": agent, "client_request_id": key, "execution_mode": mode, **extra}).json()
        assert client.post(f"/api/approvals/{turn['approval_id']}/approve", headers=USER,
                           json=consent).status_code == 200
        assert client.post(f"/api/invocations/{turn['invocation_id']}/run", params={"wait": "true"},
                           headers=USER).status_code == 200
        return turn

    with TestClient(app) as client:
        cid = client.post("/api/conversations", headers=USER, json={"title": "A"}).json()["conversation_id"]
        call(client, cid, "codex", ASK_CODEX, "t1")
        if second_conversation:  # switch away and back, as the page does
            client.post("/api/conversations", headers=USER, json={"title": "B"})
        msgs = client.get(f"/api/conversations/{cid}/messages", headers=USER).json()["messages"]
        source = [m for m in msgs if m["role"] == "assistant"][0]["message_id"]
        call(client, cid, "claude-code", claude_text, "t2",
             **({"handoff_message_ids": [source]} if handoff else {}))
    app.state.db.engine.dispose()
    return tmp_path


def test_a_clean_two_agent_run_with_a_handoff_passes(tmp_path):
    facts = TA.collect(web_run(tmp_path), code=CODE, **BINDINGS)
    assert facts["verdict"] == "PASS" and facts["reason_codes"] == []
    assert facts["agents"] == ["codex", "claude-code"] and facts["modes"] == ["supervised", "supervised"]
    assert facts["handoff_points_at_codex_reply"] and facts["sent_input_is_text_plus_frame"]
    assert facts["codex_reply_exact"] and facts["claude_reply_exact"] and not facts["code_in_user_text"]
    assert facts["codex_exit_confirmed"] and facts["codex_descendants_clean"]
    blob = json.dumps(facts)
    assert ASK_CLAUDE not in blob and ASK_CODEX not in blob  # no prompt text is written


def test_a_code_typed_by_the_user_proves_nothing(tmp_path):
    facts = TA.collect(web_run(tmp_path, claude_text="The code is OK; repeat it."), code=CODE, **BINDINGS)
    assert facts["verdict"] == "FAIL" and "code_in_user_text" in facts["reason_codes"]


def test_without_a_handoff_it_is_not_shown(tmp_path):
    facts = TA.collect(web_run(tmp_path, handoff=False), code=CODE, **BINDINGS)
    assert facts["verdict"] == "INCONCLUSIVE"
    assert {"no_handoff", "claude_reply_not_the_code"} <= set(facts["reason_codes"])


def test_an_altered_sent_input_fails(tmp_path):
    data_dir = web_run(tmp_path)
    db = Database(f"sqlite:///{(data_dir / 'agent-hub.db').as_posix()}")
    with db.session() as s:
        inv = next(i for i in s.query(InvocationRow).all() if i.snapshot.get("agent_id") == "claude-code")
        inv.snapshot = {**inv.snapshot, "task_input": inv.snapshot["task_input"] + "\nextra"}
        s.commit()
    db.engine.dispose()
    facts = TA.collect(data_dir, code=CODE, **BINDINGS)
    assert facts["verdict"] == "FAIL" and "handoff_content_mismatch" in facts["reason_codes"]


def test_strict_calls_are_not_the_supervised_run(tmp_path):
    facts = TA.collect(web_run(tmp_path, mode="strict"), code=CODE, **BINDINGS)
    assert facts["verdict"] == "FAIL" and "not_supervised" in facts["reason_codes"]


def test_no_switch_is_not_shown(tmp_path):
    facts = TA.collect(web_run(tmp_path, second_conversation=False), code=CODE, **BINDINGS)
    assert facts["verdict"] == "INCONCLUSIVE" and facts["reason_codes"] == ["no_second_conversation"]


def test_the_real_bindings_are_the_pinned_constants(tmp_path):
    facts = TA.collect(web_run(tmp_path), code=CODE)  # default: the real pinned identities
    assert facts["verdict"] == "FAIL"
    assert {"codex_binding_mismatch", "claude_binding_mismatch"} <= set(facts["reason_codes"])
    assert TA.CODEX_BINDING.startswith("codex-app-server@codex-cli 0.163.0-alpha.11.2;sha256=c81ed464")
    assert TA.CLAUDE_BINDING.startswith("claude-code@2.1.308;sha256=c9796ba2")


def test_a_call_with_no_adapter_record_is_not_shown_rather_than_execution_evidence(tmp_path):
    # Run 1: the call was refused before launch, so no record and no workdir existed.
    import sqlite3

    data_dir = web_run(tmp_path)
    conn = sqlite3.connect(data_dir / "appserver-executions.db")
    conn.execute("delete from appserver_executions")
    conn.commit()
    conn.close()
    facts = TA.collect(data_dir, code=CODE, **BINDINGS)
    assert "codex_record_missing" in facts["reason_codes"]
    assert "codex_execution_evidence" not in facts["reason_codes"] and facts["verdict"] == "INCONCLUSIVE"


def adapter_info(**changes):
    """A /api/adapter answer for the real pinned pair, as describe() renders it."""
    agent = lambda binding, transport: {"transport": transport, "binding_identity": binding, "identity_ok": True,
                                        "supervised_admission": {"allowed": True, "mode": "supervised"},
                                        "execution_admission": {"allowed": False, "mode": "blocked"}}
    info = {"kind": "multi", "agents": {"codex": agent(TA.CODEX_BINDING, "app-server"),
                                        "claude-code": agent(TA.CLAUDE_BINDING, "claude-code-cli")}}
    for path, value in changes.items():
        agent_id, key = path.split(".")
        info["agents"][agent_id][key] = value
    return info


def test_preflight_accepts_only_the_pinned_pair_on_app_server():
    assert TA.check_adapter_info(adapter_info()) == []
    assert TA.check_adapter_info(adapter_info(**{"codex.transport": "exec"})) == ["codex_transport_not_app_server"]
    assert "claude_binding_not_pinned" in TA.check_adapter_info(adapter_info(**{"claude-code.binding_identity": "x"}))
    assert "codex_supervised_not_admitted" in TA.check_adapter_info(
        adapter_info(**{"codex.supervised_admission": {"allowed": False, "mode": "blocked"}}))
    assert "claude_strict_not_blocked" in TA.check_adapter_info(
        adapter_info(**{"claude-code.execution_admission": {"allowed": True, "mode": "simulation"}}))
    assert "codex_identity_not_ok" in TA.check_adapter_info(adapter_info(**{"codex.identity_ok": False}))
    assert TA.check_adapter_info({"kind": "codex"})[0] == "not_multi"


def test_preflight_refuses_the_offline_stub_pair(tmp_path):
    from agenthub_kernel.adapters import describe

    router = RoutingAdapter({
        "codex": AppServerCodexAdapter(transport_factory=StdioStubFactory("happy"),
                                       workdir_root=str(tmp_path / "c"),
                                       store=ExecutionStore(str(tmp_path / "a.db"), table="appserver_executions")),
        "claude-code": ClaudeCodeAdapter(launcher=StubClaudeLauncher("success-usage"),
                                         workdir_root=str(tmp_path / "cc"),
                                         store=ExecutionStore(str(tmp_path / "b.db"), table="claude_code_executions"),
                                         runner=FakeClaudeRunner(code_from_handoff))})
    problems = TA.check_adapter_info(describe(router))
    assert {"codex_binding_not_pinned", "claude_binding_not_pinned",
            "codex_supervised_not_admitted", "claude_supervised_not_admitted"} <= set(problems)


@pytest.mark.parametrize("change, code", [
    ({"invocations": 3}, "budget_exceeded"),
    ({"accept_sent": True}, "accept_path_reached"),
    ({"handoff_points_at_codex_reply": False}, "handoff_source_mismatch"),
    ({"claude_workdir_empty": False}, "claude_execution_evidence"),
])
def test_judge_fails_each_broken_boundary(tmp_path, change, code):
    facts = {**TA.collect(web_run(tmp_path), code=CODE, **BINDINGS), **change}
    assert code in TA.judge(facts)[0]
