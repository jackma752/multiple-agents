"""Two agents behind one Hub (RoutingAdapter) and a manual, approved handoff.

Offline: Codex is the in-memory app-server stub, Claude Code an in-process fake
runner. No process, no model, no credentials.
"""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, func, select

from agenthub.contracts import InvocationSnapshot, execution_admission
from agenthub_adapters.appserver_adapter import AppServerCodexAdapter
from agenthub_adapters.appserver_stub import InMemoryStubFactory
from agenthub_adapters.claude_code import (
    ClaudeCodeAdapter, FakeClaudeRunner, StubClaudeLauncher, result_object, scripted,
)
from agenthub_adapters.routing import RoutingAdapter
from agenthub_adapters.store import ExecutionStore
from agenthub_kernel import config
from agenthub_kernel.adapters import adapter_binding, adapter_for, build_adapter, describe
from agenthub_kernel.conversations import restore_replies
from agenthub_kernel.db import ApprovalRow, ChatMessageRow, Database, InvocationRow, QuotaReservationRow
from agenthub_rt.api import create_app

USER = {"Authorization": "Bearer dev-user-token"}
CC_REPLY = "CC-REPLY-5b1e"


def codex(tmp_path):
    return AppServerCodexAdapter(
        transport_factory=InMemoryStubFactory("happy"), workdir_root=str(tmp_path / "codex-runs"),
        store=ExecutionStore(str(tmp_path / "appserver.db"), table="appserver_executions"))


def claude(tmp_path, prompts):
    reply = scripted(result_object(reply=CC_REPLY))

    def script(argv, env, prompt):
        prompts.append(prompt)
        return reply(argv, env, prompt)

    return ClaudeCodeAdapter(
        launcher=StubClaudeLauncher("success-usage"), workdir_root=str(tmp_path / "cc-runs"),
        store=ExecutionStore(str(tmp_path / "cc.db"), table="claude_code_executions"),
        runner=FakeClaudeRunner(script))


@pytest.fixture
def hub(tmp_path):
    prompts = []
    router = RoutingAdapter({"codex": codex(tmp_path), "claude-code": claude(tmp_path, prompts)})
    app = create_app(db=Database(f"sqlite:///{(tmp_path / 'hub.db').as_posix()}"), adapter=router)
    with TestClient(app) as client:
        yield client, router, prompts
    app.state.db.engine.dispose()


def conversation(client):
    return client.post("/api/conversations", json={"title": "two agents"}, headers=USER).json()["conversation_id"]


def submit(client, cid, agent, text, key, **extra):
    return client.post(f"/api/conversations/{cid}/invocations", headers=USER, json={
        "text": text, "agent_id": agent, "client_request_id": key, **extra})


def approve_and_run(client, turn):
    assert client.post(f"/api/approvals/{turn['approval_id']}/approve", headers=USER).status_code == 200
    return client.post(f"/api/invocations/{turn['invocation_id']}/run", params={"wait": "true"}, headers=USER)


def replies(client, cid):
    msgs = client.get(f"/api/conversations/{cid}/messages", headers=USER).json()["messages"]
    return [m for m in msgs if m["role"] == "assistant"]


def held(client):
    with client.app.state.db.session() as s:
        return s.scalar(select(func.count()).select_from(QuotaReservationRow)
                        .where(QuotaReservationRow.released_at.is_(None)))


def snapshot_of(client, turn):
    with client.app.state.db.session() as s:
        return s.get(InvocationRow, turn["invocation_id"]).snapshot


def both_answered(client):
    """One conversation: Codex answers, then Claude Code answers."""
    cid = conversation(client)
    first = submit(client, cid, "codex", "codex, say OK", "k1").json()
    assert approve_and_run(client, first).json()["status"] == "succeeded"
    second = submit(client, cid, "claude-code", "claude, answer", "k2").json()
    assert approve_and_run(client, second).json()["status"] == "succeeded"
    return cid, first, second


# ---------------------------------------------------------------- routing


def test_each_agent_runs_on_its_own_adapter(hub):
    client, router, prompts = hub
    cid, first, second = both_answered(client)
    out = replies(client, cid)
    assert [(m["invocation"]["agent_id"], m["text"]) for m in out] == [("codex", "OK"), ("claude-code", CC_REPLY)]
    codex_adapter, cc_adapter = router.for_agent("codex"), router.for_agent("claude-code")
    assert codex_adapter.start_count == 1 and cc_adapter.start_count == 1
    assert prompts == ["claude, answer"]
    assert held(client) == 0
    # Each approval bound the identity of the adapter that serves its own agent.
    assert snapshot_of(client, first)["adapter_version"] == adapter_binding(codex_adapter)
    assert snapshot_of(client, second)["adapter_version"] == adapter_binding(cc_adapter)
    assert adapter_binding(codex_adapter) != adapter_binding(cc_adapter)


def test_router_level_answers_fail_closed():
    router = RoutingAdapter({"codex": object()})
    assert execution_admission(router).allowed is False
    assert execution_admission(router, "supervised").allowed is False
    assert adapter_binding(router) == "unavailable"
    assert adapter_binding(router, "nobody") == "unavailable"
    assert adapter_for(router, "nobody") is None


def test_a_router_refuses_an_adapter_that_does_not_serve_the_agent(tmp_path):
    with pytest.raises(ValueError):
        RoutingAdapter({"claude-code": codex(tmp_path)})
    with pytest.raises(ValueError):
        RoutingAdapter({})


def test_an_unserved_agent_is_refused_before_its_approval_is_consumed(hub):
    client, router, _prompts = hub
    cid = conversation(client)
    turn = submit(client, cid, "nobody", "hello", "k1").json()
    assert snapshot_of(client, turn)["adapter_version"] == "unavailable"
    r = approve_and_run(client, turn)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "agent_not_served"
    with client.app.state.db.session() as s:
        assert s.get(ApprovalRow, turn["approval_id"]).status == "approved"  # not consumed
    assert router.for_agent("codex").start_count == 0 and router.for_agent("claude-code").start_count == 0
    assert held(client) == 0


def test_a_call_approved_for_one_agents_identity_cannot_run_as_another(hub):
    client, router, _prompts = hub
    cid = conversation(client)
    turn = submit(client, cid, "claude-code", "hello", "k1").json()
    with client.app.state.db.session() as s:  # the stored snapshot now names codex's identity
        inv = s.get(InvocationRow, turn["invocation_id"])
        inv.snapshot = {**inv.snapshot, "adapter_version": adapter_binding(router.for_agent("codex"))}
        inv.payload_hash = InvocationSnapshot(**inv.snapshot).payload_hash()
        s.get(ApprovalRow, turn["approval_id"]).payload_hash = inv.payload_hash
        s.commit()
    r = approve_and_run(client, turn)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "adapter_identity_changed"
    assert router.for_agent("claude-code").start_count == 0


def test_invocation_level_operations_reach_the_owning_adapter(hub):
    client, router, _prompts = hub
    _cid, first, second = both_answered(client)
    worker = client.app.state.worker
    assert worker._adapter_of(first["invocation_id"]) is router.for_agent("codex")
    assert worker._adapter_of(second["invocation_id"]) is router.for_agent("claude-code")
    nobody = worker._adapter_of("no-such-invocation")
    assert not any(hasattr(nobody, m) for m in ("cancel_invocation", "stop_local_invocation",
                                                "interrupt_invocation", "lookup_by_invocation"))
    # The router's own dispatch finds the owner from its store as well.
    record = router.for_agent("claude-code").lookup_by_invocation(second["invocation_id"])
    assert record is not None and router.lookup_by_invocation(second["invocation_id"]) == record
    assert router.owner_of(second["invocation_id"]) is router.for_agent("claude-code")
    assert router.owner_of(first["invocation_id"]) is router.for_agent("codex")
    assert router.cancel_invocation("no-such-invocation") == {"ok": False, "reason": "not_routed"}


def test_restored_replies_come_from_each_agents_own_store(hub):
    client, router, _prompts = hub
    cid, _first, _second = both_answered(client)
    with client.app.state.db.session() as s:
        s.execute(delete(ChatMessageRow).where(ChatMessageRow.role == "assistant"))
        s.commit()
    assert restore_replies(client.app.state.db, router) == 2
    assert [m["text"] for m in replies(client, cid)] == ["OK", CC_REPLY]


def test_describe_and_the_adapter_endpoint_list_each_agent(hub):
    client, router, _prompts = hub
    info = client.get("/api/adapter", headers=USER).json()
    assert info["kind"] == "multi" and info["agent_ids"] == ["claude-code", "codex"]
    assert info["execution_admission"]["allowed"] is False  # the router itself admits nothing
    for agent in ("codex", "claude-code"):
        assert info["agents"][agent]["execution_admission"]["mode"] == "simulation"
    assert describe(router)["agents"]["codex"]["binding_identity"] == adapter_binding(router.for_agent("codex"))


def test_build_adapter_multi_builds_each_agent_like_its_single_kind(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AGENT_HUB_ADAPTER", "multi")
    monkeypatch.setenv("AGENT_HUB_CODEX_TRANSPORT", "app-server")
    monkeypatch.setenv("AGENT_HUB_APPSERVER_STUB", "happy")
    monkeypatch.setenv("AGENT_HUB_CLAUDE_CODE_STUB", "success-usage")
    monkeypatch.setenv("AGENT_HUB_CLAUDE_CODE_WORKDIR", str(tmp_path / "cc-runs"))
    router = build_adapter()
    assert isinstance(router, RoutingAdapter) and router.AGENT_IDS == {"codex", "claude-code"}
    assert type(router.for_agent("codex")).__name__ == "AppServerCodexAdapter"
    assert type(router.for_agent("claude-code")).__name__ == "ClaudeCodeAdapter"
    for agent in ("codex", "claude-code"):
        assert execution_admission(router.for_agent(agent)).mode == "simulation"


@pytest.mark.parametrize("transport", [None, "exec"])
def test_multi_always_builds_codex_on_the_app_server_transport(monkeypatch, tmp_path, transport):
    # Run 1 of the two-agent real conversation failed because multi built Codex
    # on the default exec transport. Under multi the env setting cannot pick exec.
    from agenthub_adapters.appserver_adapter import PINNED_APPSERVER, CodexAppServerFactory

    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path))
    monkeypatch.delenv("AGENT_HUB_APPSERVER_STUB", raising=False)
    monkeypatch.setenv("AGENT_HUB_CLAUDE_CODE_STUB", "success-usage")
    monkeypatch.setenv("AGENT_HUB_CLAUDE_CODE_WORKDIR", str(tmp_path / "cc-runs"))
    if transport is None:
        monkeypatch.delenv("AGENT_HUB_CODEX_TRANSPORT", raising=False)
    else:
        monkeypatch.setenv("AGENT_HUB_CODEX_TRANSPORT", transport)
    codex_adapter = build_adapter("multi").for_agent("codex")
    assert type(codex_adapter).__name__ == "AppServerCodexAdapter" and codex_adapter.transport == "app-server"
    factory = codex_adapter.transport_factory
    assert type(factory) is CodexAppServerFactory and factory.codex_path == PINNED_APPSERVER["path"]
    assert describe(codex_adapter)["transport"] == "app-server"


# ---------------------------------------------------------------- handoff


def test_a_handoff_sends_exactly_the_chosen_reply_and_the_approval_shows_it(hub):
    client, router, prompts = hub
    cid = conversation(client)
    first = submit(client, cid, "codex", "codex, say OK", "k1").json()
    approve_and_run(client, first)
    source = replies(client, cid)[0]
    r = submit(client, cid, "claude-code", "Review this answer.", "k2",
               handoff_message_ids=[source["message_id"]])
    assert r.status_code == 200, r.text
    turn = r.json()
    assert turn["handoff"] == [{"message_id": source["message_id"], "agent_id": "codex", "chars": 2,
                                "sha256": __import__("hashlib").sha256(b"OK").hexdigest()}]
    sent = turn["sent_text"]
    # Context first (T-3): the handed-over reply, then the user's message last.
    assert sent.startswith("--- Handoff: reply of codex (Hub message ")
    assert sent.endswith("\nOK\n--- End of handoff ---\n\nReview this answer.")
    # The approval binds the composed input; the user's message keeps only what was typed.
    snap = snapshot_of(client, turn)
    assert snap["task_input"] == sent and snap["handoff"] == turn["handoff"]
    with client.app.state.db.session() as s:
        user_msg = s.scalar(select(ChatMessageRow).where(ChatMessageRow.invocation_id == turn["invocation_id"]))
        assert user_msg.text == "Review this answer."
    # The page reads the same fields from the message list.
    listed = [m for m in client.get(f"/api/conversations/{cid}/messages", headers=USER).json()["messages"]
              if m["role"] == "user" and m["invocation"]["invocation_id"] == turn["invocation_id"]][0]
    assert listed["invocation"]["handoff"] == turn["handoff"] and listed["invocation"]["sent_text"] == sent
    assert approve_and_run(client, turn).json()["status"] == "succeeded"
    assert prompts == [sent]  # exactly what was approved, nothing else


def test_editing_the_source_after_approval_does_not_change_what_is_sent(hub):
    client, _router, prompts = hub
    cid = conversation(client)
    approve_and_run(client, submit(client, cid, "codex", "say OK", "k1").json())
    source = replies(client, cid)[0]
    turn = submit(client, cid, "claude-code", "Check.", "k2", handoff_message_ids=[source["message_id"]]).json()
    with client.app.state.db.session() as s:
        s.get(ChatMessageRow, source["message_id"]).text = "TAMPERED"
        s.commit()
    approve_and_run(client, turn)
    assert prompts == [turn["sent_text"]] and "TAMPERED" not in prompts[0]


@pytest.mark.parametrize("kind", ["other_conversation", "user_message", "missing"])
def test_a_handoff_source_must_be_a_reply_in_this_conversation(hub, kind):
    client, _router, _prompts = hub
    cid, first, _second = both_answered(client)
    other = conversation(client)
    if kind == "other_conversation":
        target, message_id = other, replies(client, cid)[0]["message_id"]
    elif kind == "user_message":
        msgs = client.get(f"/api/conversations/{cid}/messages", headers=USER).json()["messages"]
        target, message_id = cid, [m for m in msgs if m["role"] == "user"][0]["message_id"]
    else:
        target, message_id = cid, 999_999
    with client.app.state.db.session() as s:
        before = s.scalar(select(func.count()).select_from(InvocationRow))
    r = submit(client, target, "claude-code", "use it", "k9", handoff_message_ids=[message_id])
    assert r.status_code == 404 and r.json()["detail"]["code"] == "handoff_not_found"
    with client.app.state.db.session() as s:
        assert s.scalar(select(func.count()).select_from(InvocationRow)) == before  # nothing created


def test_handoff_limits(hub):
    client, _router, _prompts = hub
    cid, _first, _second = both_answered(client)
    ids = [m["message_id"] for m in replies(client, cid)]
    assert submit(client, cid, "codex", "x", "d1", handoff_message_ids=[ids[0], ids[0]]).status_code == 422
    assert submit(client, cid, "codex", "x", "d2", handoff_message_ids=[1, 2, 3, 4]).status_code == 422
    with client.app.state.db.session() as s:
        s.get(ChatMessageRow, ids[1]).text = "y" * 20_001
        s.commit()
    r = submit(client, cid, "codex", "x", "d3", handoff_message_ids=[ids[1]])
    assert r.status_code == 409 and r.json()["detail"]["code"] == "handoff_too_large"


def test_a_handoff_is_part_of_the_request_key(hub):
    client, _router, _prompts = hub
    cid, _first, _second = both_answered(client)
    reply_id = replies(client, cid)[0]["message_id"]
    first = submit(client, cid, "claude-code", "same", "k7", handoff_message_ids=[reply_id])
    again = submit(client, cid, "claude-code", "same", "k7", handoff_message_ids=[reply_id])
    assert again.json()["replayed"] is True and again.json()["invocation_id"] == first.json()["invocation_id"]
    changed = submit(client, cid, "claude-code", "same", "k7")
    assert changed.status_code == 409 and changed.json()["detail"]["code"] == "idempotency_conflict"


def test_snapshots_without_handoff_keep_their_hash():
    base = dict(agent_id="codex", task_input="t", parent_task_id=None, depth=0)
    plain = InvocationSnapshot(**base)
    assert "handoff" not in plain.to_dict()
    record = {"message_id": 1, "agent_id": "codex", "chars": 2, "sha256": "0" * 64}
    with_handoff = InvocationSnapshot(**base, handoff=(record,))
    assert with_handoff.to_dict()["handoff"] == [record]
    assert with_handoff.payload_hash() != plain.payload_hash()
    assert InvocationSnapshot(**with_handoff.to_dict()) == with_handoff
    with pytest.raises(ValueError):
        InvocationSnapshot(**base, handoff=("not a record",))
