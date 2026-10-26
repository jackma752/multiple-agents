"""Async ``/run`` and per-request tool approval cards; offline stubs only.

No model call, no Codex process, no credentials. The app-server side is the
in-memory scripted server; ``approval-allow`` waits for whatever answer the
client sends (accept or decline), ``approval-silent`` never finishes the turn.
"""

import datetime as dt
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import func, select

from agenthub.contracts import STREAM_DELTA
from agenthub_adapters.appserver import ServerRequest
from agenthub_adapters.appserver_adapter import AppServerCodexAdapter, _Turn
from agenthub_adapters.appserver_stub import InMemoryStubFactory
from agenthub_adapters.mock import MockAdapter
from agenthub_kernel import worker as worker_module
from agenthub_kernel.db import (
    ApprovalRow, ConversationEventRow, Database, EventRow, InvocationRow,
    StreamChunkRow, ToolApprovalRow,
)
from agenthub_kernel.service import QuotaError, now
from agenthub_kernel.tool_approvals import ToolGate, card_hash, decide, void_pending
from agenthub_kernel.worker import Worker
from agenthub_rt.auth import Principal, authenticate
from tests.http.test_conversation_events import events, types
from tests.http.test_conversation_stream import (  # noqa: F401  (make_client is a fixture)
    held_slots, make_client, stored, submit,
)
from tests.http.test_conversations import AGENT, USER, ReplyAdapter, approve, conversation, messages


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def start(client, turn):
    return client.post(f"/api/invocations/{turn['invocation_id']}/run", headers=USER)


def eventually(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.02)
    raise AssertionError("condition not reached in time")


def status_of(client, invocation_id):
    with client.app.state.db.session() as s:
        return s.get(InvocationRow, invocation_id).status


def settle(client, timeout=10):
    assert client.app.state.worker.wait_idle(timeout), "background call did not finish"


def mock_turn(client, cid, key="one"):
    r = client.post(f"/api/conversations/{cid}/invocations", headers=USER, json={
        "text": "x", "agent_id": "mock", "client_request_id": key})
    assert r.status_code == 200, r.text
    return r.json()


def cards(client, cid, **params):
    r = client.get(f"/api/conversations/{cid}/tool-approvals", params=params, headers=USER)
    assert r.status_code == 200, r.text
    return r.json()["tool_approvals"]


def pending_card(client, cid):
    return eventually(lambda: (cards(client, cid, status="pending") or [None])[0])


def tool_client(make_client, scenario="approval-allow", *, ttl=10, **kwargs):
    factory = InMemoryStubFactory(scenario)
    client = make_client(factory, **kwargs)
    client.app.state.worker.tool_approval_ttl = ttl
    return client, factory


def running_tool_call(make_client, scenario="approval-allow", **kwargs):
    client, factory = tool_client(make_client, scenario, **kwargs)
    cid = conversation(client)
    turn = submit(client, cid)
    approve(client, turn)
    assert start(client, turn).status_code == 202
    return client, factory, cid, turn, pending_card(client, cid)


class GatedReplyAdapter(ReplyAdapter):
    """Blocks inside start until the test releases it."""

    def __init__(self, path, fail=None):
        super().__init__(path)
        self.entered = threading.Event()
        self.release = threading.Event()
        self.fail = fail

    def start(self, invocation_id, snapshot, *, gate_token=None):
        self.entered.set()
        assert self.release.wait(10)
        if self.fail:
            raise self.fail
        return super().start(invocation_id, snapshot, gate_token=gate_token)


# --------------------------------------------------------------------------- #
# async /run
# --------------------------------------------------------------------------- #


def test_run_returns_202_at_once_and_the_call_settles_in_background(make_client, tmp_path):
    adapter = GatedReplyAdapter(tmp_path / "gated.db")
    client = make_client(adapter=adapter)
    cid = conversation(client)
    turn = mock_turn(client, cid)
    approve(client, turn)
    began = time.monotonic()
    r = start(client, turn)
    assert time.monotonic() - began < 2
    assert r.status_code == 202, r.text
    body = r.json()
    assert body["accepted"] is True and body["launch_scheduled"] is True
    assert body["status"] == "starting" and "future" not in body
    assert adapter.entered.wait(5)
    assert status_of(client, turn["invocation_id"]) == "starting"
    assert "starting" in [e["payload"].get("to") for e in events(client, cid)["events"]]
    adapter.release.set()
    settle(client)
    assert status_of(client, turn["invocation_id"]) == "succeeded"
    assert messages(client, cid)[-1]["role"] == "assistant"
    assert held_slots(client) == 0 and adapter.start_count == 1


def test_refusals_stay_synchronous_and_a_running_call_cannot_start_twice(make_client, tmp_path):
    adapter = GatedReplyAdapter(tmp_path / "gated.db")
    client = make_client(adapter=adapter)
    cid = conversation(client)
    turn = mock_turn(client, cid)
    r = start(client, turn)  # not approved yet
    assert r.status_code == 409 and r.json()["detail"]["code"] == "not_ready"
    assert adapter.start_count == 0
    approve(client, turn)
    assert start(client, turn).status_code == 202
    assert adapter.entered.wait(5)
    again = start(client, turn)
    assert again.status_code == 409 and again.json()["detail"]["accepted"] is False
    adapter.release.set()
    settle(client)
    assert adapter.start_count == 1


def test_adapter_exception_in_background_is_unknown_and_keeps_the_slot(make_client, tmp_path):
    adapter = GatedReplyAdapter(tmp_path / "gated.db", fail=RuntimeError("boom"))
    client = make_client(adapter=adapter)
    cid = conversation(client)
    turn = mock_turn(client, cid)
    approve(client, turn)
    assert start(client, turn).status_code == 202
    adapter.release.set()
    settle(client)
    assert status_of(client, turn["invocation_id"]) == "unknown"
    assert held_slots(client) == 1
    assert [m["role"] for m in messages(client, cid)] == ["user"]


def test_settlement_failure_in_background_is_recorded_as_unknown(make_client, tmp_path, monkeypatch):
    adapter = GatedReplyAdapter(tmp_path / "gated.db")
    client = make_client(adapter=adapter)
    cid = conversation(client)
    turn = mock_turn(client, cid)
    approve(client, turn)

    def broken(*_a, **_k):
        raise RuntimeError("settlement storage failed")

    monkeypatch.setattr(worker_module, "capture_reply", broken)
    adapter.release.set()
    assert start(client, turn).status_code == 202
    settle(client)
    with client.app.state.db.session() as s:
        inv = s.get(InvocationRow, turn["invocation_id"])
        assert inv.status == "unknown" and "background execution failed" in inv.error
    assert held_slots(client) == 1


def test_recover_leaves_calls_this_process_is_running_alone(make_client, tmp_path):
    adapter = GatedReplyAdapter(tmp_path / "gated.db")
    client = make_client(adapter=adapter)
    cid = conversation(client)
    turn = mock_turn(client, cid)
    approve(client, turn)
    assert start(client, turn).status_code == 202
    assert adapter.entered.wait(5)
    summary = client.app.state.worker.recover()
    assert turn["invocation_id"] not in summary["unknown"]
    assert status_of(client, turn["invocation_id"]) == "starting"
    adapter.release.set()
    settle(client)
    assert status_of(client, turn["invocation_id"]) == "succeeded"


def test_shutdown_waits_for_inflight_calls_then_refuses_new_ones(make_client, tmp_path):
    adapter = GatedReplyAdapter(tmp_path / "gated.db")
    client = make_client(adapter=adapter)
    cid = conversation(client)
    first, second = mock_turn(client, cid, "a"), mock_turn(client, cid, "b")
    approve(client, first)
    approve(client, second)
    assert start(client, first).status_code == 202
    assert adapter.entered.wait(5)
    threading.Timer(0.3, adapter.release.set).start()
    assert client.app.state.worker.shutdown(timeout=10) is True
    assert status_of(client, first["invocation_id"]) == "succeeded"
    r = start(client, second)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "worker_stopped"
    with client.app.state.db.session() as s:
        assert s.get(ApprovalRow, second["approval_id"]).status == "approved"


# --------------------------------------------------------------------------- #
# tool approval cards
# --------------------------------------------------------------------------- #


def test_approved_card_answers_exactly_that_request_with_accept(make_client):
    client, factory, cid, turn, card = running_tool_call(make_client)
    assert card["invocation_id"] == turn["invocation_id"] and card["status"] == "pending"
    assert (card["thread_id"], card["turn_id"], card["item_id"]) == (
        "stub-thread-0001", "stub-turn-0001", "item-cmd-1")
    assert card["method"] == "item/commandExecution/requestApproval"
    assert card["details"]["command"] == "echo stub" and card["details"]["cwd"] == "C:\\stub"
    assert card["available_decisions"] == ["accept", "decline"]
    assert card["details"]["provider_available_decisions"] == ["accept", "decline", "cancel"]
    assert len(card["card_hash"]) == 64 and card["delivery"] == "pending"
    assert status_of(client, turn["invocation_id"]) == "starting"  # waiting on the user

    r = client.post(f"/api/tool-approvals/{card['tool_approval_id']}/approve",
                    headers=USER, json={"card_hash": card["card_hash"]})
    assert r.status_code == 200 and r.json()["status"] == "approved"
    assert r.json()["decided_by"]
    settle(client)
    assert factory.servers[0].answered_approvals() == [{"id": "srv-1", "decision": "accept"}]
    assert status_of(client, turn["invocation_id"]) == "succeeded"
    assert "".join(c.text for c in stored(client, StreamChunkRow, cid)) == "ran\n"
    final = client.get(f"/api/tool-approvals/{card['tool_approval_id']}", headers=USER).json()
    assert final["delivery"] == "sent" and final["decision_sent"] == "accept"

    names = types(events(client, cid))
    requested = names.index("tool_approval.requested")
    decided = names.index("tool_approval.status")
    delivered = names.index("tool_approval.delivery")
    succeeded = max(i for i, e in enumerate(events(client, cid)["events"])
                    if e["type"] == "invocation.status" and e["payload"]["to"] == "succeeded")
    assert requested < decided < delivered < succeeded
    # Card details (the command) stay out of every event and audit payload.
    dumped = json.dumps([r.payload for r in stored(client, ConversationEventRow, cid)]
                        + [r.payload for r in stored(client, EventRow)], ensure_ascii=False)
    assert "echo stub" not in dumped and "C:\\\\stub" not in dumped


def test_declined_card_sends_decline_and_nothing_runs(make_client):
    client, factory, cid, turn, card = running_tool_call(make_client)
    r = client.post(f"/api/tool-approvals/{card['tool_approval_id']}/decline", headers=USER)
    assert r.status_code == 200 and r.json()["status"] == "declined"
    settle(client)
    assert factory.servers[0].answered_approvals() == [{"id": "srv-1", "decision": "decline"}]
    assert stored(client, StreamChunkRow, cid) == []
    final = client.get(f"/api/tool-approvals/{card['tool_approval_id']}", headers=USER).json()
    assert final["decision_sent"] == "decline"
    late = client.post(f"/api/tool-approvals/{card['tool_approval_id']}/approve",
                       headers=USER, json={"card_hash": final["card_hash"]})
    assert late.status_code == 409 and late.json()["detail"]["code"] == "not_pending"


def test_expired_card_is_declined_and_cannot_be_approved_late(make_client):
    client, factory, cid, turn, card = running_tool_call(make_client, ttl=0.4)
    settle(client)
    final = client.get(f"/api/tool-approvals/{card['tool_approval_id']}", headers=USER).json()
    assert final["status"] == "expired" and final["decision_sent"] == "decline"
    assert factory.servers[0].answered_approvals() == [{"id": "srv-1", "decision": "decline"}]
    late = client.post(f"/api/tool-approvals/{card['tool_approval_id']}/approve",
                       headers=USER, json={"card_hash": card["card_hash"]})
    assert late.status_code == 409 and late.json()["detail"]["code"] == "not_pending"


def test_approval_needs_the_current_card_hash(make_client):
    client, factory, cid, turn, card = running_tool_call(make_client)
    path = f"/api/tool-approvals/{card['tool_approval_id']}/approve"
    missing = client.post(path, headers=USER)
    assert missing.status_code == 409 and missing.json()["detail"]["code"] == "card_hash_required"
    wrong = client.post(path, headers=USER, json={"card_hash": "0" * 64})
    assert wrong.status_code == 409 and wrong.json()["detail"]["code"] == "card_changed"
    bad_shape = client.post(path, headers=USER, json={"card_hash": "short"})
    assert bad_shape.status_code == 422
    assert cards(client, cid)[0]["status"] == "pending"
    client.post(f"/api/tool-approvals/{card['tool_approval_id']}/decline", headers=USER)
    settle(client)
    assert factory.servers[0].answered_approvals()[0]["decision"] == "decline"


@pytest.mark.parametrize("headers,status", [({}, 401), (AGENT, 403)])
def test_cards_require_a_user(make_client, headers, status):
    client, _factory, cid, _turn, card = running_tool_call(make_client)
    cid_path = f"/api/conversations/{cid}/tool-approvals"
    card_path = f"/api/tool-approvals/{card['tool_approval_id']}"
    for method, path, body in [("get", cid_path, None), ("get", card_path, None),
                               ("post", card_path + "/approve", {"card_hash": card["card_hash"]}),
                               ("post", card_path + "/decline", None)]:
        r = client.request(method, path, headers=headers, **({"json": body} if body else {}))
        assert r.status_code == status and "echo stub" not in r.text
    assert cards(client, cid)[0]["status"] == "pending"
    client.post(card_path + "/decline", headers=USER)
    settle(client)


def test_other_users_cannot_see_or_answer_cards(make_client):
    client, _factory, cid, _turn, card = running_tool_call(make_client)
    card_path = f"/api/tool-approvals/{card['tool_approval_id']}"
    client.app.dependency_overrides[authenticate] = lambda: Principal("other-user", "user")
    try:
        for method, path, body in [
            ("get", f"/api/conversations/{cid}/tool-approvals", None), ("get", card_path, None),
            ("post", card_path + "/approve", {"card_hash": card["card_hash"]}),
            ("post", card_path + "/decline", None),
        ]:
            r = client.request(method, path, headers=USER, **({"json": body} if body else {}))
            assert r.status_code == 404 and "echo stub" not in r.text
    finally:
        client.app.dependency_overrides.clear()
    assert cards(client, cid)[0]["status"] == "pending"
    client.post(card_path + "/decline", headers=USER)
    settle(client)


def test_card_is_void_when_the_transport_ends_first(make_client):
    client, factory, cid, turn, card = running_tool_call(make_client, "approval-silent",
                                                         timeout_seconds=20)
    stopped = client.app.state.worker.stop_local(turn["invocation_id"])
    assert stopped["remote_stop_confirmed"] is False
    settle(client)
    final = client.get(f"/api/tool-approvals/{card['tool_approval_id']}", headers=USER).json()
    assert final["status"] == "void" and final["delivery"] == "not_sent"
    assert final["decision_sent"] is None
    assert factory.servers[0].answered_approvals() == []
    late = client.post(f"/api/tool-approvals/{card['tool_approval_id']}/approve",
                       headers=USER, json={"card_hash": final["card_hash"]})
    # The local stop is an effective point: tool decisions of the call are closed.
    assert late.status_code == 409 and late.json()["detail"]["code"] == "tool_decisions_closed"
    assert status_of(client, turn["invocation_id"]) == "unknown" and held_slots(client) == 1


# --------------------------------------------------------------------------- #
# kernel rules, exercised directly
# --------------------------------------------------------------------------- #


@pytest.fixture
def kernel_db(tmp_path):
    db = Database(f"sqlite:///{(tmp_path / 'cards.db').as_posix()}")
    db.create_all()
    with db.session() as s:
        s.add(InvocationRow(id="inv-1", task_id="task-1", payload_hash="h", snapshot={},
                            status="running"))
    yield db
    db.engine.dispose()


def open_card(db, *, offered=("accept", "decline"), ttl=30, invocation_id="inv-1", key="1"):
    return ToolGate(db, invocation_id, ttl_seconds=ttl).open(
        method="item/commandExecution/requestApproval", request_key=key,
        thread_id="t", turn_id="u", item_id="i", details={"command": "ls"},
        available_decisions=list(offered) if offered is not None else None,
    )


def presented(db, card_id):
    with db.session() as s:
        return card_hash(s.get(ToolApprovalRow, card_id))


def test_accept_cannot_be_given_when_the_provider_did_not_offer_it(kernel_db):
    card = open_card(kernel_db, offered=("decline", "cancel"))
    with kernel_db.session() as s, pytest.raises(QuotaError) as exc:
        decide(s, card, decision="approved", actor="u", presented_hash=presented(kernel_db, card))
    assert exc.value.code == "accept_not_offered"
    with kernel_db.session() as s:
        assert decide(s, card, decision="declined", actor="u", presented_hash=None).status == "declined"


def test_concurrent_approvals_settle_exactly_once(kernel_db):
    card = open_card(kernel_db)
    digest = presented(kernel_db, card)

    def attempt(_):
        try:
            with kernel_db.session() as s:
                decide(s, card, decision="approved", actor="u", presented_hash=digest)
            return "ok"
        except QuotaError as exc:
            return exc.code

    with ThreadPoolExecutor(max_workers=8) as pool:
        outcomes = list(pool.map(attempt, range(8)))
    assert outcomes.count("ok") == 1 and set(outcomes) - {"ok"} == {"not_pending"}


def test_expiry_wins_over_a_late_approval(kernel_db):
    card = open_card(kernel_db, ttl=0.05)
    time.sleep(0.1)
    with kernel_db.session() as s, pytest.raises(QuotaError) as exc:
        decide(s, card, decision="approved", actor="u", presented_hash=presented(kernel_db, card))
    assert exc.value.code == "expired"
    assert ToolGate(kernel_db, "inv-1", ttl_seconds=1).poll(card) == "expired"
    with kernel_db.session() as s, pytest.raises(QuotaError) as exc:
        decide(s, card, decision="approved", actor="u", presented_hash=presented(kernel_db, card))
    assert exc.value.code == "not_pending"


def test_a_card_of_a_finished_call_cannot_be_approved(kernel_db):
    card = open_card(kernel_db)
    with kernel_db.session() as s:
        s.get(InvocationRow, "inv-1").status = "succeeded"
    with kernel_db.session() as s, pytest.raises(QuotaError) as exc:
        decide(s, card, decision="approved", actor="u", presented_hash=presented(kernel_db, card))
    assert exc.value.code == "call_not_active"


def test_deadline_caps_the_card_lifetime(kernel_db):
    deadline = now() + dt.timedelta(seconds=2)
    card = ToolGate(kernel_db, "inv-1", ttl_seconds=600).open(
        method="m", request_key="k", thread_id="t", turn_id="u", item_id="i",
        details={}, available_decisions=None, deadline=deadline)
    with kernel_db.session() as s:
        assert s.get(ToolApprovalRow, card).expires_at <= deadline


def test_recover_voids_cards_nobody_can_deliver(kernel_db, tmp_path):
    card = open_card(kernel_db)
    Worker(kernel_db, MockAdapter(store_path=str(tmp_path / "m.db"))).recover()
    with kernel_db.session() as s:
        row = s.get(ToolApprovalRow, card)
        assert row.status == "void" and row.delivery == "not_sent"


def test_void_pending_is_a_no_op_on_a_database_without_cards(tmp_path):
    db = Database(f"sqlite:///{(tmp_path / 'old.db').as_posix()}")  # no create_all
    assert void_pending(db, invocation_ids=["x"]) == 0
    db.engine.dispose()


class FakeGate:
    def __init__(self, statuses):
        self.statuses = statuses
        self.delivered_calls = []

    def poll(self, card_id):
        return self.statuses[card_id]

    def delivered(self, card_id, *, decision, ok):
        self.delivered_calls.append((card_id, decision, ok))


class FakeClient:
    def __init__(self):
        self.answers = []

    def answer_approval(self, request, decision):
        request.answered = True
        self.answers.append((request.id, decision))


def test_one_card_answers_only_its_own_request(tmp_path):
    adapter = AppServerCodexAdapter(transport_factory=InMemoryStubFactory(),
                                    workdir_root=str(tmp_path / "runs"))
    turn = _Turn("t", "u")
    first = ServerRequest(id="srv-1", method="item/commandExecution/requestApproval", params={"command": "echo one"})
    second = ServerRequest(id="srv-2", method="item/commandExecution/requestApproval", params={"command": "echo two"})
    turn.cards = {"srv-1": (first, "card-a"), "srv-2": (second, "card-b")}
    gate = FakeGate({"card-a": "approved", "card-b": "pending"})
    client = FakeClient()
    adapter._answer_decided(client, turn, gate)
    assert client.answers == [("srv-1", "accept")]
    assert second.answered is False and list(turn.cards) == ["srv-2"]
    gate.statuses["card-b"] = "expired"
    turn.last_card_poll = 0
    adapter._answer_decided(client, turn, gate)
    assert client.answers == [("srv-1", "accept"), ("srv-2", "decline")]
    assert gate.delivered_calls == [("card-a", "accept", True), ("card-b", "decline", True)]
