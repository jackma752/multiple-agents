"""Interrupt a live turn; observe native child agents. Offline stubs only.

``interrupt-terminal``: the stub acknowledges ``turn/interrupt`` and then
reports the turn ``interrupted``. ``interrupt-accepted``: it acknowledges and
then never reports anything - the case where nothing may be claimed stopped.
"""

import json

import pytest

from agenthub_adapters import appserver_stub
from agenthub_adapters.appserver_adapter import AppServerCodexAdapter
from agenthub_adapters.appserver_stub import DEMO_INTRO, InMemoryStubFactory
from agenthub_kernel.db import ConversationEventRow, InvocationRow, StreamChunkRow
from agenthub_rt.auth import Principal, authenticate
from tests.http.test_async_and_tool_approval import (
    eventually, pending_card, settle, start, status_of,
)
from tests.http.test_conversation_events import events, types
from tests.http.test_conversation_stream import (  # noqa: F401  (make_client is a fixture)
    adapter_of, held_slots, make_client, stored, submit,
)
from tests.http.test_conversations import AGENT, USER, approve, conversation, messages


def running(make_client, scenario, **kwargs):
    factory = InMemoryStubFactory(scenario)
    client = make_client(factory, **kwargs)
    cid = conversation(client)
    turn = submit(client, cid)
    approve(client, turn)
    assert start(client, turn).status_code == 202
    eventually(lambda: "stream.turn_started" in types(events(client, cid)))
    return client, factory, cid, turn


def interrupt(client, turn, headers=USER):
    return client.post(f"/api/invocations/{turn['invocation_id']}/interrupt", headers=headers)


def test_interrupt_confirmed_by_the_turns_terminal_event_settles_canceled(make_client):
    client, factory, cid, turn = running(make_client, "interrupt-terminal")
    r = interrupt(client, turn)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["interrupt_requested"] and body["interrupt_accepted"]
    assert body["terminal_received"] is True and body["remote_stop_confirmed"] is True
    assert body["status"] == "canceled" and body["quota_retained"] is False
    settle(client)
    assert held_slots(client) == 0
    sent = [m for m in factory.servers[0].received if m.get("method") == "turn/interrupt"]
    assert sent and sent[0]["params"] == {"threadId": "stub-thread-0001", "turnId": "stub-turn-0001"}
    page = events(client, cid)
    requested = [e["payload"] for e in page["events"] if e["type"] == "task.interrupt_requested"]
    assert requested == [{"root_task_id": turn["task_id"], "invocation_id": turn["invocation_id"],
                          "actor": "local-user", "repeat": False, "closed_cards": 0,
                          "blocked_decisions": 0, "in_flight": 0, "already_sent": 0}]
    acked = [e["payload"] for e in page["events"] if e["type"] == "task.interrupt_acknowledged"]
    assert acked == [{"root_task_id": turn["task_id"], "invocation_id": turn["invocation_id"],
                      "accepted": True, "code": "awaiting_terminal_event"}]
    ended = [e["payload"] for e in page["events"] if e["type"] == "stream.turn_ended"]
    assert ended[0]["turn_status"] == "interrupted" and ended[0]["verdict"] == "cancelled"
    # The effective point is recorded before the provider is asked.
    assert names_in_order(page, "task.interrupt_requested", "task.interrupt_acknowledged",
                          "stream.turn_ended")


def names_in_order(page, *names):
    found = types(page)
    positions = [found.index(n) for n in names]
    return positions == sorted(positions)


def test_accepted_interrupt_without_terminal_event_claims_nothing(make_client):
    client, factory, cid, turn = running(make_client, "interrupt-accepted", timeout_seconds=1.5)
    client.app.state.worker.INTERRUPT_WAIT_SECONDS = 0.3
    body = interrupt(client, turn).json()
    assert body["interrupt_accepted"] is True and body["terminal_received"] is False
    assert body["remote_stop_confirmed"] is False and body["quota_retained"] is True
    assert body["status"] == "starting"
    settle(client)
    # Never "canceled": the adapter's own deadline ends it as unconfirmed.
    with client.app.state.db.session() as s:
        inv = s.get(InvocationRow, turn["invocation_id"])
        assert inv.status == "unknown" and "interrupt" in (inv.error or "")
    assert held_slots(client) == 1
    record = adapter_of(client).lookup_by_invocation(turn["invocation_id"])
    assert record["payload"]["interrupt_requested"] and record["payload"]["interrupt_accepted"]
    assert record["status"] == "running" and "native_session" not in record["payload"]


def test_interrupt_requires_a_running_call_and_a_user(make_client):
    client, _factory, cid, turn = running(make_client, "interrupt-terminal")
    for headers, code in (({}, 401), (AGENT, 403)):
        assert interrupt(client, turn, headers).status_code == code
    client.app.dependency_overrides[authenticate] = lambda: Principal("other-user", "user")
    try:
        assert interrupt(client, turn).status_code == 404
    finally:
        client.app.dependency_overrides.clear()
    assert interrupt(client, turn).json()["status"] == "canceled"
    settle(client)
    again = interrupt(client, turn)
    assert again.status_code == 409 and again.json()["detail"]["code"] == "not_active"
    assert client.post("/api/invocations/nope/interrupt", headers=USER).status_code == 404


def test_adapter_without_a_live_turn_reports_it(tmp_path):
    adapter = AppServerCodexAdapter(transport_factory=InMemoryStubFactory(),
                                    workdir_root=str(tmp_path / "runs"))
    out = adapter.interrupt_invocation("not-running")
    assert out["interrupt_requested"] is False and out["reason"] == "no_live_turn"
    assert adapter.cancel_invocation("not-running")["reason"] == "unsupported"


def test_tree_stop_interrupts_inflight_calls_and_settles_once(make_client):
    client, _factory, cid, turn = running(make_client, "interrupt-terminal")
    r = client.post(f"/api/tasks/{turn['task_id']}/stop", headers=USER)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["confirmed"] == 1 and body["unconfirmed"] == 0
    assert body["outcomes"][0]["cancel"]["outcome"] == "canceled"
    settle(client)
    assert status_of(client, turn["invocation_id"]) == "canceled" and held_slots(client) == 0
    statuses = [e["payload"]["to"] for e in events(client, cid)["events"]
                if e["type"] == "invocation.status" and e["payload"]["invocation_id"] == turn["invocation_id"]]
    assert statuses.count("canceled") == 1


def test_tree_stop_without_terminal_event_keeps_the_slot(make_client):
    client, _factory, _cid, turn = running(make_client, "interrupt-accepted", timeout_seconds=1.5)
    client.app.state.worker.INTERRUPT_WAIT_SECONDS = 0.3
    body = client.post(f"/api/tasks/{turn['task_id']}/stop", headers=USER).json()
    assert body["confirmed"] == 0 and body["unconfirmed"] == 1
    settle(client)
    assert status_of(client, turn["invocation_id"]) == "unknown" and held_slots(client) == 1


def test_native_child_agent_is_observed_without_copying_its_prompt(make_client):
    client, _factory, cid, turn = running(make_client, "native-spawn")
    settle(client)
    seen = [e["payload"] for e in events(client, cid)["events"]
            if e["type"] == "stream.native_agent_observed"]
    assert [(s["item_type"], s["tool"], s["status"], s["child_thread_ids"]) for s in seen] == [
        ("collabAgentToolCall", "spawnAgent", "inProgress", ["child-thread-1"]),
        ("collabAgentToolCall", "spawnAgent", "completed", ["child-thread-1"]),
    ]
    dumped = json.dumps([r.payload for r in stored(client, ConversationEventRow, cid)]
                        + [c.text for c in stored(client, StreamChunkRow, cid)], ensure_ascii=False)
    assert "child prompt text" not in dumped
    record = adapter_of(client).lookup_by_invocation(turn["invocation_id"])
    assert record["payload"]["native_agents"][0]["child_thread_ids"] == ["child-thread-1"]
    # The spawn CALL completed, but the child never reported its own end:
    # the call stays unknown (see test_interrupt_races for the full rule).
    assert status_of(client, turn["invocation_id"]) == "unknown"


def test_interrupt_while_a_tool_card_waits_voids_the_card(make_client, monkeypatch):
    monkeypatch.setattr(appserver_stub, "DEMO_DELAY", 0)
    client, factory, cid, turn = running(make_client, "demo")
    card = pending_card(client, cid)
    body = interrupt(client, turn).json()
    assert body["status"] == "canceled" and body["terminal_received"] is True
    settle(client)
    final = client.get(f"/api/tool-approvals/{card['tool_approval_id']}", headers=USER).json()
    assert final["status"] == "void" and final["delivery"] == "not_sent"
    assert factory.servers[0].answered_approvals() == []
    reply = messages(client, cid)[-1]
    assert reply["role"] == "assistant" and reply["text"] == "".join(DEMO_INTRO)
