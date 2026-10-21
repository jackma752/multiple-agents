"""Interrupt vs tool-decision races, repeats, completion races; child end evidence.

Offline only. ``approval-hold`` keeps the turn open forever and records any
answer that reaches the stub, so a decision leaking past the interrupt's
effective point would show up in ``answered_approvals()``.
"""

import time

import pytest
from sqlalchemy import func, select

from agenthub_adapters import appserver_adapter
from agenthub_adapters.appserver_adapter import AppServerCodexAdapter, _Turn
from agenthub_adapters.appserver_stub import InMemoryStubFactory
from agenthub_kernel.db import Database, InvocationRow, ToolApprovalRow, ToolGateClosureRow
from agenthub_kernel.tool_approvals import ToolGate, card_hash, close_decisions, decide
from agenthub_kernel.service import QuotaError
from tests.http.test_async_and_tool_approval import (
    GatedReplyAdapter, eventually, mock_turn, pending_card, settle, start, status_of,
)
from tests.http.test_conversation_events import events
from tests.http.test_conversation_stream import (  # noqa: F401  (make_client is a fixture)
    adapter_of, held_slots, make_client,
)
from tests.http.test_conversations import USER, approve, conversation, messages
from tests.http.test_interrupt_and_native import interrupt, running


def card_view(client, card):
    return client.get(f"/api/tool-approvals/{card['tool_approval_id']}", headers=USER).json()


def approve_card(client, card):
    return client.post(f"/api/tool-approvals/{card['tool_approval_id']}/approve",
                       headers=USER, json={"card_hash": card["card_hash"]})


def unknown_transitions(client, cid, invocation_id):
    return [e for e in events(client, cid)["events"] if e["type"] == "invocation.status"
            and e["payload"]["invocation_id"] == invocation_id and e["payload"]["to"] == "unknown"]


# --------------------------------------------------------------------------- #
# interrupt vs tool decisions
# --------------------------------------------------------------------------- #


def test_card_cannot_be_approved_while_an_interrupt_is_unconfirmed(make_client):
    client, factory, cid, turn = running(make_client, "approval-hold", timeout_seconds=1.5)
    client.app.state.worker.INTERRUPT_WAIT_SECONDS = 0.2
    card = pending_card(client, cid)
    body = interrupt(client, turn).json()
    assert body["tool_decisions_closed"] is True and body["closed_cards"] == 1
    assert body["terminal_received"] is False and body["quota_retained"] is True
    view = card_view(client, card)
    assert (view["status"], view["delivery"], view["decision_sent"]) == ("void", "not_sent", None)
    for verb in ("approve", "decline"):
        r = client.post(f"/api/tool-approvals/{card['tool_approval_id']}/{verb}", headers=USER,
                        json={"card_hash": card["card_hash"]})
        assert r.status_code == 409 and r.json()["detail"]["code"] == "tool_decisions_closed"
    settle(client)
    assert factory.servers[0].answered_approvals() == []  # nothing reached the provider
    assert status_of(client, turn["invocation_id"]) == "unknown" and held_slots(client) == 1


def test_decision_made_before_the_interrupt_but_not_sent_is_blocked(make_client, monkeypatch):
    # Slow the adapter's card polling so the approval is decided but not yet
    # claimed when the interrupt's effective point commits.
    monkeypatch.setattr(appserver_adapter, "CARD_POLL_SECONDS", 1.0)
    client, factory, cid, turn = running(make_client, "approval-hold", timeout_seconds=2.5)
    client.app.state.worker.INTERRUPT_WAIT_SECONDS = 0.2
    card = pending_card(client, cid)
    assert approve_card(client, card).json()["status"] == "approved"
    body = interrupt(client, turn).json()
    assert body["blocked_decisions"] == 1 and body["closed_cards"] == 0
    settle(client)
    view = card_view(client, card)
    # The user's decision is kept as made; it was never delivered.
    assert (view["status"], view["delivery"], view["decision_sent"]) == ("approved", "blocked", None)
    assert factory.servers[0].answered_approvals() == []
    assert status_of(client, turn["invocation_id"]) == "unknown"


def test_decision_already_sent_is_reported_not_rewritten(make_client):
    client, factory, cid, turn = running(make_client, "approval-hold", timeout_seconds=2.0)
    client.app.state.worker.INTERRUPT_WAIT_SECONDS = 0.2
    card = pending_card(client, cid)
    approve_card(client, card)
    eventually(lambda: factory.servers[0].answered_approvals())
    eventually(lambda: card_view(client, card)["delivery"] == "sent")
    body = interrupt(client, turn).json()
    assert body["already_sent_decisions"] == 1 and body["blocked_decisions"] == 0
    view = card_view(client, card)
    assert (view["status"], view["delivery"], view["decision_sent"]) == ("approved", "sent", "accept")
    requested = [e["payload"] for e in events(client, cid)["events"] if e["type"] == "task.interrupt_requested"]
    assert requested[0]["already_sent"] == 1
    settle(client)
    assert factory.servers[0].answered_approvals() == [{"id": "srv-1", "decision": "accept"}]


def test_repeated_interrupt_is_recorded_and_settles_once(make_client):
    client, factory, cid, turn = running(make_client, "approval-hold", timeout_seconds=1.5)
    client.app.state.worker.INTERRUPT_WAIT_SECONDS = 0.1
    first, second = interrupt(client, turn).json(), interrupt(client, turn).json()
    assert first["repeat"] is False and second["repeat"] is True
    assert second["tool_decisions_closed"] is True and second["terminal_received"] is False
    sent = [m for m in factory.servers[0].received if m.get("method") == "turn/interrupt"]
    assert len(sent) == 2  # the user may ask again; each ask is recorded
    with client.app.state.db.session() as s:
        assert s.scalar(select(func.count()).select_from(ToolGateClosureRow)) == 1
    repeats = [e["payload"]["repeat"] for e in events(client, cid)["events"]
               if e["type"] == "task.interrupt_requested"]
    assert repeats == [False, True]
    settle(client)
    assert len(unknown_transitions(client, cid, turn["invocation_id"])) == 1
    assert held_slots(client) == 1


def test_normal_completion_racing_the_interrupt_settles_as_completed(make_client):
    client, _factory, cid, turn = running(make_client, "complete-on-interrupt")
    body = interrupt(client, turn).json()
    assert body["interrupt_accepted"] is True and body["terminal_received"] is True
    assert body["status"] == "succeeded"  # not "canceled": the turn finished on its own
    settle(client)
    assert held_slots(client) == 0 and messages(client, cid)[-1]["text"] == "done"
    terminal = [e["payload"]["to"] for e in events(client, cid)["events"]
                if e["type"] == "invocation.status" and e["payload"]["to"] in
                ("succeeded", "canceled", "failed", "unknown")]
    assert terminal == ["succeeded"]


def test_interrupt_without_adapter_support_closes_decisions_and_claims_nothing(make_client, tmp_path):
    adapter = GatedReplyAdapter(tmp_path / "gated.db")
    client = make_client(adapter=adapter)
    cid = conversation(client)
    turn = mock_turn(client, cid)
    approve(client, turn)
    assert start(client, turn).status_code == 202
    assert adapter.entered.wait(5)
    body = interrupt(client, turn).json()
    assert body["interrupt_requested"] is False and body["reason"] == "unsupported"
    assert body["tool_decisions_closed"] is True and body["quota_retained"] is True
    assert body["status"] == "starting"
    adapter.release.set()
    settle(client)
    # Nothing was marked unknown by the request itself; the call ended normally.
    assert status_of(client, turn["invocation_id"]) == "succeeded"


def test_tree_stop_closes_tool_decisions_before_anything_else(make_client):
    client, factory, cid, turn = running(make_client, "approval-hold", timeout_seconds=1.5)
    client.app.state.worker.INTERRUPT_WAIT_SECONDS = 0.2
    card = pending_card(client, cid)
    assert client.post(f"/api/tasks/{turn['task_id']}/stop", headers=USER).status_code == 200
    assert card_view(client, card)["status"] == "void"
    r = approve_card(client, card)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "tool_decisions_closed"
    settle(client)
    assert factory.servers[0].answered_approvals() == []
    assert status_of(client, turn["invocation_id"]) == "unknown"


# --------------------------------------------------------------------------- #
# gate rules, directly
# --------------------------------------------------------------------------- #


@pytest.fixture
def kernel_db(tmp_path):
    db = Database(f"sqlite:///{(tmp_path / 'gate.db').as_posix()}")
    db.create_all()
    with db.session() as s:
        s.add(InvocationRow(id="inv-1", task_id="task-1", payload_hash="h", snapshot={},
                            status="running"))
    yield db
    db.engine.dispose()


def opened(db, key="1"):
    return ToolGate(db, "inv-1", ttl_seconds=30).open(
        method="item/commandExecution/requestApproval", request_key=key, thread_id="t",
        turn_id="u", item_id="i", details={"command": "ls"}, available_decisions=["accept", "decline"])


def test_claim_on_a_closed_call_blocks_even_without_the_closure_sweep(kernel_db):
    card = opened(kernel_db)
    with kernel_db.session() as s:
        decide(s, card, decision="approved", actor="u",
               presented_hash=card_hash(s.get(ToolApprovalRow, card)))
    with kernel_db.session() as s:  # closure row only - no sweep of the cards
        s.add(ToolGateClosureRow(invocation_id="inv-1", reason="interrupt", closed_by="u"))
    assert ToolGate(kernel_db, "inv-1", ttl_seconds=30).claim(card) is None
    with kernel_db.session() as s:
        assert s.get(ToolApprovalRow, card).delivery == "blocked"


def test_claim_before_closure_is_reported_in_flight(kernel_db):
    gate = ToolGate(kernel_db, "inv-1", ttl_seconds=30)
    card = opened(kernel_db)
    with kernel_db.session() as s:
        decide(s, card, decision="approved", actor="u",
               presented_hash=card_hash(s.get(ToolApprovalRow, card)))
    assert gate.claim(card) == "accept"
    assert gate.claim(card) is None  # one claim per decision
    with kernel_db.session() as s:
        summary = close_decisions(s, "inv-1", reason="interrupt", actor="u")
    assert summary["in_flight"] == [card] and summary["blocked_decisions"] == []
    gate.delivered(card, decision="accept", ok=True)
    with kernel_db.session() as s:
        row = s.get(ToolApprovalRow, card)
        assert (row.delivery, row.decision_sent) == ("sent", "accept")


def test_request_after_closure_is_recorded_but_unanswerable(kernel_db):
    with kernel_db.session() as s:
        close_decisions(s, "inv-1", reason="interrupt", actor="u")
    card = opened(kernel_db, key="late")
    with kernel_db.session() as s:
        row = s.get(ToolApprovalRow, card)
        assert (row.status, row.delivery) == ("void", "blocked")
    assert ToolGate(kernel_db, "inv-1", ttl_seconds=30).claim(card) is None
    with kernel_db.session() as s, pytest.raises(QuotaError) as exc:
        decide(s, card, decision="approved", actor="u", presented_hash=None)
    assert exc.value.code == "tool_decisions_closed"


# --------------------------------------------------------------------------- #
# native child agents: end evidence
# --------------------------------------------------------------------------- #


def test_child_without_end_evidence_keeps_the_call_unknown_and_records_the_parent_turn(make_client):
    client, _factory, cid, turn = running(make_client, "native-spawn")
    settle(client)
    assert status_of(client, turn["invocation_id"]) == "unknown" and held_slots(client) == 1
    ended = [e["payload"] for e in events(client, cid)["events"] if e["type"] == "stream.turn_ended"]
    assert ended[0]["turn_status"] == "completed"  # the parent's actual terminal status
    assert ended[0]["terminal"] is False and ended[0]["native_unfinished"] == ["child-thread-1"]
    record = adapter_of(client).lookup_by_invocation(turn["invocation_id"])["payload"]
    assert record["parent_turn"] == {"turn_status": "completed", "verdict": "finished", "terminal": True}
    assert record["native_unfinished"] == ["child-thread-1"]
    assert record["native_children"] == {"child-thread-1": "unknown"}
    assert "native_session" not in record
    with client.app.state.db.session() as s:
        error = s.get(InvocationRow, turn["invocation_id"]).error
    assert "child-thread-1" in error and "not counted in Hub quota" in error
    assert [m["role"] for m in messages(client, cid)] == ["user"]  # no invented reply


def test_child_with_its_own_end_evidence_lets_the_call_settle(make_client):
    client, _factory, cid, turn = running(make_client, "native-spawn-finished")
    settle(client)
    assert status_of(client, turn["invocation_id"]) == "succeeded" and held_slots(client) == 0
    record = adapter_of(client).lookup_by_invocation(turn["invocation_id"])["payload"]
    assert record["native_children"] == {"child-thread-1": "completed"}
    assert record["native_unfinished"] == []
    assert messages(client, cid)[-1]["text"] == "OK"


@pytest.mark.parametrize("states,unfinished", [
    ({}, ["c1"]),                                                     # spawn call alone
    ({"c1": {"status": "notFound"}}, ["c1"]),                         # ambiguous
    ({"c1": {"status": "running", "message": "secret"}}, ["c1"]),
    ({"c1": {"status": "completed", "message": "secret"}}, []),
    ({"c1": {"status": "shutdown"}}, []),
])
def test_only_a_childs_own_state_counts_as_end_evidence(states, unfinished):
    turn = _Turn("t", "u")
    seen = []
    item = {"type": "collabAgentToolCall", "id": "x", "tool": "spawnAgent", "status": "completed",
            "receiverThreadIds": ["c1"], "agentsStates": states, "prompt": "do not copy"}
    AppServerCodexAdapter._observe_native(item, turn, lambda etype, p: seen.append(p))
    assert turn.unfinished_children() == unfinished
    assert "secret" not in repr(seen) and "do not copy" not in repr(seen)
