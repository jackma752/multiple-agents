"""T-1h run judge, offline: the planned real web run, on stubs.

Codex is the stdio app-server stub (it replies "OK"); Claude Code is an
in-process fake that answers the code it finds in the file snippet. The
fixture file holds that code. No Codex, no Claude Code, no model.
"""
import importlib.util
import json
from pathlib import Path

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
SPEC = importlib.util.spec_from_file_location("t1h_collect_under_test", ROOT / "tools/probes/t1h_collect.py")
T1H = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(T1H)

USER = {"Authorization": "Bearer dev-user-token"}
CODE = "OK"  # what the "happy" Codex stub replies
BINDINGS = {"codex_binding": "appserver-stub:stdio", "claude_binding": "claude-code-stub:stdio"}
ASK = "Reply with exactly the code in the referenced file, and nothing else. Do not use tools."
REF = "code.md:L1-3"


def code_from_file(argv, env, prompt):
    line = next((x for x in prompt.splitlines() if x.startswith("CODE: ")), "CODE: NONE")
    return scripted(result_object(reply=line[6:]))(argv, env, prompt)


def web_run(tmp_path, *, refs=(REF,), ask=ASK):
    files = tmp_path / "files"
    files.mkdir()
    (files / "code.md").write_text(f"T-1h fixture\nCODE: {CODE}\nend\n", encoding="utf-8")
    data = tmp_path / "data"
    data.mkdir()
    router = RoutingAdapter({
        "codex": AppServerCodexAdapter(
            transport_factory=StdioStubFactory("happy"), workdir_root=str(data / "codex-runs"),
            store=ExecutionStore(str(data / "appserver-executions.db"), table="appserver_executions")),
        "claude-code": ClaudeCodeAdapter(
            launcher=StubClaudeLauncher("success-usage"), workdir_root=str(data / "cc-runs"),
            store=ExecutionStore(str(data / "claude-code-executions.db"), table="claude_code_executions"),
            runner=FakeClaudeRunner(code_from_file)),
    })
    app = create_app(db=Database(f"sqlite:///{(data / 'agent-hub.db').as_posix()}"), adapter=router,
                     files_root=str(files))
    consent = {"accept_unmanaged_native_agents": True}

    def send(client, cid, agent, key, **extra):
        return client.post(f"/api/conversations/{cid}/invocations", headers=USER, json={
            "text": ask, "agent_id": agent, "client_request_id": key, "execution_mode": "supervised",
            "file_refs": list(refs), **extra}).json()

    with TestClient(app) as client:
        cid = client.post("/api/conversations", headers=USER, json={"title": "T-1h"}).json()["conversation_id"]
        send(client, cid, "codex", "opener")  # creates the task, so a budget can be set; revoked below
        root = client.get(f"/api/conversations/{cid}/messages", headers=USER).json()["root_task_id"]
        assert client.patch(f"/api/tasks/{root}/policy", headers=USER, json={
            "token_budget_total": 200000, "token_budget_per_call": 60000,
            "token_budget_mode": "strict"}).status_code == 200
        for agent, key in (("codex", "t1"), ("claude-code", "t2")):
            turn = send(client, cid, agent, key)
            assert client.post(f"/api/approvals/{turn['approval_id']}/approve", headers=USER,
                               json=consent).status_code == 200
            assert client.post(f"/api/invocations/{turn['invocation_id']}/run", params={"wait": "true"},
                               headers=USER).status_code == 200
    app.state.db.engine.dispose()
    return data, files / "code.md"


def test_a_clean_run_passes(tmp_path):
    data, fixture = web_run(tmp_path)
    facts = T1H.collect(data, code=CODE, fixture=fixture, ref=REF, **BINDINGS)
    assert facts["verdict"] == "PASS" and facts["reason_codes"] == []
    assert facts["agents"] == ["codex", "claude-code"] and facts["invocations"] == 2
    for call in facts["calls"]:
        assert call["preview_valid"] and call["sent_equals_adapter_input"] and call["snippets_match_fixture"]
        assert call["usage"]["completeness"] == "complete" and call["budget_item"]["state"] == "settled"
    assert facts["budget_counters"] == facts["budget_item_sums"]
    assert facts["budget_counters"][0] == facts["ledger_known_sum"] > 0
    assert ASK not in json.dumps(facts)  # no prompt text is written


def test_a_code_typed_by_the_user_proves_nothing(tmp_path):
    data, fixture = web_run(tmp_path, ask=f"The code is {CODE}; repeat it.")
    facts = T1H.collect(data, code=CODE, fixture=fixture, ref=REF, **BINDINGS)
    assert facts["verdict"] == "FAIL" and "codex_code_in_user_text" in facts["reason_codes"]


def test_a_sent_input_that_differs_from_the_approved_one_fails(tmp_path):
    data, fixture = web_run(tmp_path)
    db = Database(f"sqlite:///{(data / 'agent-hub.db').as_posix()}")
    with db.session() as s:
        inv = next(i for i in s.query(InvocationRow).all()
                   if i.snapshot.get("agent_id") == "claude-code" and i.status == "succeeded")
        inv.snapshot = {**inv.snapshot, "task_input": inv.snapshot["task_input"] + "x"}
        s.commit()
    db.engine.dispose()
    facts = T1H.collect(data, code=CODE, fixture=fixture, ref=REF, **BINDINGS)
    assert facts["verdict"] == "FAIL"
    assert {"claude-code_preview_invalid", "claude-code_sent_differs_from_approved"} <= set(facts["reason_codes"])


def test_without_the_file_it_is_not_shown(tmp_path):
    data, fixture = web_run(tmp_path, refs=())
    facts = T1H.collect(data, code=CODE, fixture=fixture, ref=REF, **BINDINGS)
    assert facts["verdict"] == "FAIL"  # no snippet: the code cannot have come from the file
    assert "claude-code_snippet_mismatch" in facts["reason_codes"]
