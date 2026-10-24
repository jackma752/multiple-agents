"""App-server transport: deltas in the conversation event stream, native resume.

Offline only: an in-memory scripted server or the bundled stdio stub. No model
call, no Codex process, no credentials.
"""

import hashlib
import json
import threading
import time
from dataclasses import asdict

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from agenthub.chat import ConversationTurn
from agenthub.contracts import (
    AdapterStartResult, InvocationSnapshot, STREAM_DELTA, execution_admission,
)
from agenthub_adapters.appserver_adapter import AppServerCodexAdapter, CodexAppServerFactory
from agenthub_adapters.appserver_stub import STREAM_PIECES, InMemoryStubFactory
from agenthub_adapters.mock import MockAdapter
from agenthub_adapters.store import ExecutionStore
from agenthub_kernel import events as events_module
from agenthub_kernel import worker as worker_module
from agenthub_kernel.db import (
    ApprovalRow, ChatMessageRow, ConversationEventCursorRow, ConversationEventRow, Database,
    EventRow, InvocationRow, QuotaReservationRow, StreamChunkRow, TaskRow,
)
from agenthub_kernel.events import StreamSink, attach_stream_text, read_events
from agenthub_rt.api import create_app
from agenthub_rt.auth import Principal, authenticate
from tests.http.test_conversations import AGENT, USER, approve, conversation, messages, run
from tests.http.test_conversation_events import events, parse_sse, stream, types

SECRET_TEXT = "user wording 机密 that must never enter the event stream"
STREAMED = "".join(STREAM_PIECES)


def make_adapter(tmp_path, factory, **kwargs):
    return AppServerCodexAdapter(
        transport_factory=factory, workdir_root=str(tmp_path / "runs"),
        store=ExecutionStore(str(tmp_path / "appserver.db"), table="appserver_executions"),
        **kwargs,
    )


@pytest.fixture
def make_client(tmp_path):
    apps = []

    def _make(factory=None, adapter=None, **kwargs):
        adapter = adapter or make_adapter(tmp_path, factory or InMemoryStubFactory("stream"), **kwargs)
        app = create_app(db=Database(f"sqlite:///{(tmp_path / 'chat.db').as_posix()}"),
                         adapter=adapter)
        client = TestClient(app)
        client.__enter__()
        apps.append((client, app))
        return client

    yield _make
    for client, app in apps:
        client.__exit__(None, None, None)
        app.state.db.engine.dispose()


def submit(client, cid, key="one", text="first task", **extra):
    r = client.post(f"/api/conversations/{cid}/invocations", headers=USER, json={
        "text": text, "agent_id": "codex", "client_request_id": key, **extra,
    })
    assert r.status_code == 200, r.text
    return r.json()


def turn_and_run(client, cid, key, **extra):
    turn = submit(client, cid, key=key, **extra)
    approve(client, turn)
    r = run(client, turn)
    assert r.status_code == 200, r.text
    return turn, r.json()


def stream_events(page, invocation_id=None):
    return [e for e in page["events"] if e["type"].startswith("stream.")
            and (invocation_id is None or e["payload"]["invocation_id"] == invocation_id)]


def held_slots(client):
    with client.app.state.db.session() as s:
        return s.scalar(select(func.count()).select_from(QuotaReservationRow)
                        .where(QuotaReservationRow.released_at.is_(None)))


def adapter_of(client):
    return client.app.state.worker.adapter


def stored(client, model, cid=None):
    with client.app.state.db.session() as s:
        stmt = select(model)
        if cid is not None:
            stmt = stmt.where(model.conversation_id == cid)
        return s.scalars(stmt).all()


@pytest.fixture
def unbuffered(monkeypatch):
    """One stream.delta event per provider delta, so reconnects can split them."""
    monkeypatch.setattr(worker_module, "StreamSink",
                        lambda db, inv: StreamSink(db, inv, flush_seconds=0))


# --------------------------------------------------------------------------- #
# deltas in the event stream
# --------------------------------------------------------------------------- #


def test_deltas_join_the_same_ordered_stream_before_settlement(make_client):
    client = make_client()
    cid = conversation(client)
    turn, result = turn_and_run(client, cid, "one", text=SECRET_TEXT)
    assert result["status"] == "succeeded"
    assert result["stream_events"]["dropped"] == 0
    page = events(client, cid)
    assert [e["seq"] for e in page["events"]] == list(range(1, len(page["events"]) + 1))
    names = types(page)
    started, ended = names.index("stream.turn_started"), names.index("stream.turn_ended")
    deltas = [i for i, n in enumerate(names) if n == STREAM_DELTA]
    assert deltas and all(started < i < ended for i in deltas)
    statuses = [(i, e["payload"]["to"]) for i, e in enumerate(page["events"])
                if e["type"] == "invocation.status"]
    to_starting = next(i for i, s in statuses if s == "starting")
    to_succeeded = next(i for i, s in statuses if s == "succeeded")
    assert to_starting < started and ended < to_succeeded
    text = "".join(page["events"][i]["payload"]["text"] for i in deltas)
    assert text == STREAMED  # the foreign turn's delta is not attributed
    assert all(page["events"][i]["payload"]["item_id"] == "item-msg" for i in deltas)
    assert page["events"][ended]["payload"] == {
        "invocation_id": turn["invocation_id"], "turn_status": "completed",
        "verdict": "finished", "terminal": True, "native_unfinished": [],
        "tool_process_state": "none",
    }
    assert names[-1] == "message.created"
    reply = messages(client, cid)[-1]
    assert reply["role"] == "assistant" and reply["text"] == STREAMED
    assert SECRET_TEXT not in json.dumps(page, ensure_ascii=False)


def test_stored_events_hold_ids_only_and_text_lives_in_chunks(make_client):
    client = make_client()
    cid = conversation(client)
    turn, _ = turn_and_run(client, cid, "one", text=SECRET_TEXT)
    rows = stored(client, ConversationEventRow, cid)
    dumped = json.dumps([r.payload for r in rows], ensure_ascii=False)
    for secret in (SECRET_TEXT, STREAMED, "流式", "world"):
        assert secret not in dumped
    deltas = [r for r in rows if r.type == STREAM_DELTA]
    assert deltas and all(set(r.payload) == {"invocation_id", "item_id", "chunk_id"} for r in deltas)
    chunks = stored(client, StreamChunkRow, cid)
    assert [c.id for c in chunks] == [r.payload["chunk_id"] for r in deltas]
    assert "".join(c.text for c in chunks) == STREAMED
    assert {c.invocation_id for c in chunks} == {turn["invocation_id"]}
    # Model output also stays out of tree audit events and invocation errors.
    audit = json.dumps([r.payload for r in stored(client, EventRow)], ensure_ascii=False)
    assert "流式" not in audit and SECRET_TEXT not in audit


def test_sse_replays_stream_frames(make_client):
    client = make_client()
    cid = conversation(client)
    turn_and_run(client, cid, "one")
    status, ctype, body = stream(client, cid)
    assert status == 200 and ctype.startswith("text/event-stream")
    frames = parse_sse(body)
    assert [f["id"] for f in frames] == [e["seq"] for e in events(client, cid)["events"]]
    assert "".join(f["data"]["payload"]["text"] for f in frames if f["event"] == STREAM_DELTA) == STREAMED


def test_reconnect_mid_stream_neither_loses_nor_repeats_text(make_client, unbuffered):
    client = make_client()
    cid = conversation(client)
    turn_and_run(client, cid, "one")
    full = parse_sse(stream(client, cid)[2])
    delta_frames = [f for f in full if f["event"] == STREAM_DELTA]
    assert len(delta_frames) == len(STREAM_PIECES)
    cut = delta_frames[1]["id"]  # disconnected after the second delta
    before = [f for f in full if f["id"] <= cut]
    status, _ctype, body = stream(client, cid, headers={**USER, "Last-Event-ID": str(cut)})
    after = parse_sse(body)
    assert status == 200 and [f["id"] for f in after] == [f["id"] for f in full if f["id"] > cut]
    joined = "".join(f["data"]["payload"]["text"] for f in before + after if f["event"] == STREAM_DELTA)
    assert joined == STREAMED == messages(client, cid)[-1]["text"]
    # The JSON cursor reader assembles the same text.
    page = events(client, cid, after_seq=cut)
    assert [e["payload"].get("text") for e in page["events"] if e["type"] == STREAM_DELTA] == \
        list(STREAM_PIECES[2:])


@pytest.mark.parametrize("headers,status", [({}, 401), (AGENT, 403)])
def test_stream_text_requires_user(make_client, headers, status):
    client = make_client()
    cid = conversation(client)
    turn_and_run(client, cid, "one")
    for path in (f"/api/conversations/{cid}/events", f"/api/conversations/{cid}/events/stream"):
        r = client.get(path, headers=headers, params={"timeout": 0})
        assert r.status_code == status and "流式" not in r.text


def test_other_users_cannot_read_stream_text(make_client):
    client = make_client()
    cid = conversation(client)
    turn_and_run(client, cid, "one")
    client.app.dependency_overrides[authenticate] = lambda: Principal("other-user", "user")
    try:
        for path in (f"/api/conversations/{cid}/events", f"/api/conversations/{cid}/events/stream"):
            r = client.get(path, headers=USER, params={"timeout": 0})
            assert r.status_code == 404 and "流式" not in r.text
    finally:
        client.app.dependency_overrides.clear()


def test_chunk_ids_resolve_only_within_their_conversation(make_client):
    client = make_client()
    a, b = conversation(client), conversation(client)
    turn_and_run(client, b, "one")
    foreign = stored(client, StreamChunkRow, b)[0]
    forged = [{"seq": 1, "type": STREAM_DELTA,
               "payload": {"invocation_id": "x", "item_id": "i", "chunk_id": foreign.id}}]
    with client.app.state.db.session() as s:
        assert "text" not in attach_stream_text(s, a, forged)[0]["payload"]
        assert attach_stream_text(s, b, forged)[0]["payload"]["text"] == foreign.text


def test_chunk_and_event_roll_back_together(make_client, monkeypatch):
    client = make_client()
    cid = conversation(client)
    turn = submit(client, cid)
    db = client.app.state.db
    before = len(stored(client, ConversationEventRow, cid))
    sink = StreamSink(db, turn["invocation_id"], flush_seconds=0)
    real_append = events_module._append

    def failing_append(conn, out):
        real_append(conn, out)
        raise RuntimeError("synthetic failure after both writes")

    monkeypatch.setattr(events_module, "_append", failing_append)
    sink.emit(STREAM_DELTA, {"item_id": "i", "text": "lost"})
    assert sink.dropped == 1 and sink.written == 0
    assert stored(client, StreamChunkRow) == []
    assert len(stored(client, ConversationEventRow, cid)) == before
    monkeypatch.setattr(events_module, "_append", real_append)
    sink.emit(STREAM_DELTA, {"item_id": "i", "text": "kept"})
    rows = stored(client, ConversationEventRow, cid)
    assert [r.seq for r in rows] == list(range(1, len(rows) + 1))  # no hole
    with db.session() as s:
        assert s.get(ConversationEventCursorRow, cid).last_seq == len(rows)
    assert [c.text for c in stored(client, StreamChunkRow)] == ["kept"]


class ProbeStreamingAdapter(MockAdapter):
    """Emits, then looks at the database while still inside start."""

    def __init__(self, path):
        super().__init__(store_path=str(path))
        self.db = None
        self.seen_during_run = None

    def start_streaming(self, invocation_id, snapshot, *, gate_token=None, emit=None):
        emit(STREAM_DELTA, {"item_id": "i", "text": "partial"})
        emit("stream.turn_started", {"thread_id": "t", "turn_id": "u", "resumed": False,
                                     "smuggled": SECRET_TEXT})
        emit("not.a.stream.type", {"text": "x"})
        with self.db.session() as s:
            cid = s.scalar(select(ChatMessageRow.conversation_id)
                           .where(ChatMessageRow.invocation_id == invocation_id))
            status = s.get(InvocationRow, invocation_id).status
            self.seen_during_run = (status, read_events(s, cid, 0, 500)["events"])
        return super().start(invocation_id, snapshot, gate_token=gate_token)


def test_deltas_are_visible_while_the_call_runs(make_client, tmp_path):
    adapter = ProbeStreamingAdapter(tmp_path / "mock.db")
    client = make_client(adapter=adapter)
    adapter.db = client.app.state.db
    cid = conversation(client)
    turn = client.post(f"/api/conversations/{cid}/invocations", headers=USER, json={
        "text": "x", "agent_id": "mock", "client_request_id": "k"}).json()
    approve(client, turn)
    result = run(client, turn).json()
    assert result["stream_events"] == {"written": 2, "dropped": 0, "rejected": 1}
    status, seen = adapter.seen_during_run
    assert status == "starting"
    assert [e["type"] for e in seen][-2:] == [STREAM_DELTA, "stream.turn_started"]
    assert "text" not in seen[-2]["payload"]  # stored rows carry the chunk id only
    assert stored(client, StreamChunkRow, cid)[0].text == "partial"
    assert events(client, cid)["events"][len(seen) - 2]["payload"]["text"] == "partial"
    assert "smuggled" not in seen[-1]["payload"]  # payload keys are whitelisted


def test_sink_coalesces_and_never_breaks_on_write_failure(tmp_path):
    db = Database(f"sqlite:///{(tmp_path / 's.db').as_posix()}")
    db.create_all()
    sink = StreamSink(db, "inv-not-in-a-conversation")
    sink.emit(STREAM_DELTA, {"text": "x"})
    assert not sink.enabled and sink.close() == {"written": 0, "dropped": 0, "rejected": 0}

    class Broken:
        def session(self):
            raise RuntimeError("database gone")

    broken = StreamSink(Broken(), "inv")
    broken._conversation_id = "conv"  # force enabled to exercise the failure path
    broken.emit(STREAM_DELTA, {"text": "x"})
    assert broken.close()["dropped"] >= 1
    db.engine.dispose()


# --------------------------------------------------------------------------- #
# outcomes: only this turn's terminal status settles
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("scenario", ["slow-terminal", "disconnect"])
def test_unconfirmed_turn_is_unknown_keeps_slot_and_has_no_reply(make_client, scenario):
    client = make_client(InMemoryStubFactory(scenario), timeout_seconds=0.5)
    cid = conversation(client)
    turn, result = turn_and_run(client, cid, "one")
    assert result["status"] == "unknown" and result["terminal"] is False
    assert held_slots(client) == 1
    assert [m["role"] for m in messages(client, cid)] == ["user"]
    ended = [e for e in events(client, cid)["events"] if e["type"] == "stream.turn_ended"]
    assert ended[0]["payload"]["terminal"] is False
    record = adapter_of(client).lookup_by_invocation(turn["invocation_id"])
    assert record["status"] == "running" and "native_session" not in record["payload"]
    partial = "".join(c.text for c in stored(client, StreamChunkRow, cid))
    assert partial in ("waiting", "half")
    with client.app.state.db.session() as s:
        assert partial not in (s.get(InvocationRow, turn["invocation_id"]).error or "")


def test_failed_turn_is_terminal_and_frees_slot(make_client):
    client = make_client(InMemoryStubFactory("turn-failed"))
    cid = conversation(client)
    _turn, result = turn_and_run(client, cid, "one")
    assert result["status"] == "failed" and held_slots(client) == 0


def test_unanswered_tool_request_expires_into_decline_never_accept(make_client):
    factory = InMemoryStubFactory("approval-allow")
    client = make_client(factory)
    client.app.state.worker.tool_approval_ttl = 0.3  # nobody answers the card
    cid = conversation(client)
    _turn, result = turn_and_run(client, cid, "one")
    assert factory.servers[0].answered_approvals() == [{"id": "srv-1", "decision": "decline"}]
    statuses = [(e["payload"]["from"], e["payload"]["to"]) for e in events(client, cid)["events"]
                if e["type"] == "tool_approval.status"]
    assert statuses == [("pending", "expired")]
    delivery = [e["payload"] for e in events(client, cid)["events"] if e["type"] == "tool_approval.delivery"]
    # Claimed (sending) right before the write, then sent.
    assert [d["delivery"] for d in delivery] == ["sending", "sent"]
    assert delivery[-1]["decision_sent"] == "decline"
    assert not [e for e in events(client, cid)["events"] if e["type"] == STREAM_DELTA]
    assert result["status"] == "succeeded"


def test_without_a_gate_tool_requests_are_still_declined(tmp_path):
    """Direct adapter use (no Worker gate) keeps the old fail-closed answer."""
    factory = InMemoryStubFactory("approval-allow")
    adapter = make_adapter(tmp_path, factory)
    snap = InvocationSnapshot(agent_id="codex", task_input="x", parent_task_id=None, depth=0,
                              adapter_version=adapter.binding_identity())
    seen = []
    result = adapter.start_streaming("inv-direct", snap, gate_token="t",
                                     emit=lambda etype, payload: seen.append((etype, payload)))
    assert factory.servers[0].answered_approvals() == [{"id": "srv-1", "decision": "decline"}]
    declined = [p for etype, p in seen if etype == "stream.request_declined"]
    assert declined[0]["method"] == "item/commandExecution/requestApproval"
    assert declined[0]["decision"] == "decline" and "command" not in declined[0]
    assert result.ok is True


# --------------------------------------------------------------------------- #
# gates are unchanged
# --------------------------------------------------------------------------- #


def test_unapproved_call_launches_nothing(make_client):
    factory = InMemoryStubFactory("stream")
    client = make_client(factory)
    cid = conversation(client)
    turn = submit(client, cid)
    assert run(client, turn).status_code == 409
    assert adapter_of(client).start_count == 0 and factory.servers == []


def test_real_appserver_is_blocked_in_strict_and_supervised(make_client, tmp_path):
    """Strict never admits it; supervised only when every pinned condition holds (S6)."""
    real = CodexAppServerFactory(codex_path=str(tmp_path / "missing.exe"),
                                 codex_home=str(tmp_path / "home"), expected_identity=None)
    adapter = make_adapter(tmp_path, real)
    assert execution_admission(adapter, "strict").code == "appserver_real_unverified"
    supervised = execution_admission(adapter, "supervised")
    assert not supervised.allowed and supervised.code == "appserver_supervised_conditions"
    assert "identity_not_pinned_constant" in supervised.reason
    assert "executable_missing" in supervised.reason

    class LookalikeStub(InMemoryStubFactory):
        pass

    assert not execution_admission(make_adapter(tmp_path, LookalikeStub())).allowed
    client = make_client(adapter=adapter)
    cid = conversation(client)
    turn = submit(client, cid, execution_mode="supervised")
    consent = client.post(f"/api/approvals/{turn['approval_id']}/approve", headers=USER,
                          json={"accept_unmanaged_native_agents": True})
    assert consent.status_code == 200, consent.text
    r = run(client, turn)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "appserver_supervised_conditions"
    assert adapter.start_count == 0
    with client.app.state.db.session() as s:
        assert s.get(ApprovalRow, turn["approval_id"]).status == "approved"
    assert not (tmp_path / "home").exists()


def test_direct_start_without_gate_token_is_refused(tmp_path):
    adapter = make_adapter(tmp_path, InMemoryStubFactory("stream"))
    snap = InvocationSnapshot(agent_id="codex", task_input="x", parent_task_id=None, depth=0)
    result = adapter.start("inv-x", snap)
    assert result.ok is False and result.terminal is True and adapter.start_count == 0


def test_local_stop_closes_only_our_transport_and_keeps_unknown(make_client):
    factory = InMemoryStubFactory("slow-terminal")
    client = make_client(factory, timeout_seconds=20)
    cid = conversation(client)
    turn = submit(client, cid)
    approve(client, turn)
    worker = client.app.state.worker
    box = {}
    runner = threading.Thread(target=lambda: box.update(r=worker.run_one(turn["invocation_id"])))
    runner.start()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and "stream.turn_started" not in types(events(client, cid)):
        time.sleep(0.02)
    stopped = worker.stop_local(turn["invocation_id"])
    runner.join(timeout=10)
    assert not runner.is_alive()
    assert stopped["remote_stop_confirmed"] is False and stopped["quota_retained"] is True
    assert stopped["local_process_exited"] is True
    with client.app.state.db.session() as s:
        assert s.get(InvocationRow, turn["invocation_id"]).status == "unknown"
    assert held_slots(client) == 1
    assert [m["role"] for m in messages(client, cid)] == ["user"]


# --------------------------------------------------------------------------- #
# native resume
# --------------------------------------------------------------------------- #


def test_native_resume_binds_thread_and_sends_thread_resume(make_client):
    factory = InMemoryStubFactory("stream", thread_id="thread-abc")
    client = make_client(factory)
    cid = conversation(client)
    first, _ = turn_and_run(client, cid, "one")
    assert first["context_mode"] == "independent" and first["resume_thread_id"] is None
    reply = messages(client, cid)[-1]
    assert reply["result"]["native_session"] == {"transport": "app-server", "thread_id": "thread-abc"}

    second = submit(client, cid, key="two", text="follow up", context_mode="native_resume")
    assert second["context_mode"] == "native_resume"
    assert second["resume_thread_id"] == "thread-abc"
    independent = InvocationSnapshot(agent_id="codex", task_input="follow up",
                                     parent_task_id=None, depth=0, policy_version=1)
    assert second["payload_hash"] != independent.payload_hash()
    approve(client, second)
    assert run(client, second).json()["status"] == "succeeded"

    first_methods = factory.servers[0].received_methods()
    second_methods = factory.servers[1].received_methods()
    assert "thread/start" in first_methods and "thread/resume" not in first_methods
    assert "thread/resume" in second_methods and "thread/start" not in second_methods
    resume = next(m for m in factory.servers[1].received if m.get("method") == "thread/resume")
    start = next(m for m in factory.servers[0].received if m.get("method") == "thread/start")
    # Same cwd as the thread's first call, and the fixed settings again (D2, D6).
    assert resume["params"] == {"threadId": "thread-abc", "cwd": start["params"]["cwd"],
                                "sandbox": "read-only", "approvalPolicy": "on-request",
                                "approvalsReviewer": "user"}
    assert start["params"]["cwd"].endswith(first["invocation_id"])
    started = [e for e in stream_events(events(client, cid), second["invocation_id"])
               if e["type"] == "stream.turn_started"]
    assert started[0]["payload"]["resumed"] is True
    # Only the new text goes to the provider; history is not replayed.
    turn_start = next(m for m in factory.servers[1].received if m.get("method") == "turn/start")
    assert turn_start["params"]["input"] == [{"type": "text", "text": "follow up"}]


def test_native_resume_without_recorded_thread_is_refused_and_leaves_nothing(make_client):
    client = make_client()
    cid = conversation(client)
    r = client.post(f"/api/conversations/{cid}/invocations", headers=USER, json={
        "text": "x", "agent_id": "codex", "client_request_id": "k",
        "context_mode": "native_resume"})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "no_native_session"
    with client.app.state.db.session() as s:
        assert s.scalar(select(func.count()).select_from(InvocationRow)) == 0
        assert s.scalar(select(func.count()).select_from(TaskRow)) == 0
    bad = client.post(f"/api/conversations/{cid}/invocations", headers=USER, json={
        "text": "x", "agent_id": "codex", "client_request_id": "k", "context_mode": "resume"})
    assert bad.status_code == 422


def test_native_resume_refused_while_conversation_has_unknown_call(make_client):
    factory = InMemoryStubFactory(["stream", "slow-terminal"])
    client = make_client(factory, timeout_seconds=0.5)
    cid = conversation(client)
    turn_and_run(client, cid, "one")
    _turn, result = turn_and_run(client, cid, "two")
    assert result["status"] == "unknown"
    r = client.post(f"/api/conversations/{cid}/invocations", headers=USER, json={
        "text": "x", "agent_id": "codex", "client_request_id": "three",
        "context_mode": "native_resume"})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "native_session_busy"


def test_worker_refuses_resume_on_busy_thread_before_consuming(make_client):
    factory = InMemoryStubFactory(["stream", "slow-terminal", "stream"])
    client = make_client(factory, timeout_seconds=0.5)
    cid = conversation(client)
    turn_and_run(client, cid, "one")
    a = submit(client, cid, key="a", context_mode="native_resume")
    b = submit(client, cid, key="b", context_mode="native_resume")
    approve(client, a)
    approve(client, b)
    assert run(client, a).json()["status"] == "unknown"
    starts = adapter_of(client).start_count
    r = run(client, b)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "native_session_busy"
    assert adapter_of(client).start_count == starts
    with client.app.state.db.session() as s:
        assert s.get(ApprovalRow, b["approval_id"]).status == "approved"


class NoResumeAdapter(AppServerCodexAdapter):
    def supports_native_resume(self):
        return False


def test_adapter_without_resume_never_runs_a_resume_call(make_client, tmp_path):
    client = make_client()
    cid = conversation(client)
    turn_and_run(client, cid, "one")
    second = submit(client, cid, key="two", context_mode="native_resume")
    approve(client, second)
    client.app.state.worker.adapter = NoResumeAdapter(
        transport_factory=InMemoryStubFactory("stream"), workdir_root=str(tmp_path / "r2"))
    r = run(client, second)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "native_resume_unsupported"
    assert client.app.state.worker.adapter.start_count == 0
    with client.app.state.db.session() as s:
        assert s.get(ApprovalRow, second["approval_id"]).status == "approved"


def test_mock_replies_record_no_native_session(make_client, tmp_path):
    from tests.http.test_conversations import ReplyAdapter

    client = make_client(adapter=ReplyAdapter(tmp_path / "reply.db"))
    cid = conversation(client)
    turn = client.post(f"/api/conversations/{cid}/invocations", headers=USER, json={
        "text": "x", "agent_id": "mock", "client_request_id": "k"}).json()
    approve(client, turn)
    run(client, turn)
    assert "native_session" not in messages(client, cid)[-1]["result"]
    r = client.post(f"/api/conversations/{cid}/invocations", headers=USER, json={
        "text": "x", "agent_id": "mock", "client_request_id": "k2", "context_mode": "native_resume"})
    assert r.json()["detail"]["code"] == "no_native_session"


# --------------------------------------------------------------------------- #
# contracts
# --------------------------------------------------------------------------- #


def test_snapshot_context_fields_are_validated_and_hash_compatible():
    base = dict(agent_id="codex", task_input="x", parent_task_id=None, depth=0)
    plain = InvocationSnapshot(**base)
    assert "context_mode" not in plain.to_dict() and "resume_thread_id" not in plain.to_dict()
    resumed = InvocationSnapshot(**base, context_mode="native_resume", resume_thread_id="t")
    assert resumed.to_dict()["resume_thread_id"] == "t"
    assert InvocationSnapshot(**resumed.to_dict()) == resumed
    assert resumed.payload_hash() != InvocationSnapshot(
        **base, context_mode="native_resume", resume_thread_id="other").payload_hash()
    for bad in ({"context_mode": "native_resume"}, {"resume_thread_id": "t"},
                {"context_mode": "native_resume", "resume_thread_id": " "},
                {"context_mode": "other"}):
        with pytest.raises(ValueError):
            InvocationSnapshot(**base, **bad)


def test_turn_request_hash_unchanged_for_independent_turns():
    turn = ConversationTurn(text="x", agent_id="codex", client_request_id="k")
    # The fields a turn had before context_mode (and later handoff_message_ids, file_refs,
    # summary_id, include_history) existed.
    legacy = {k: v for k, v in asdict(turn).items()
              if k not in ("context_mode", "handoff_message_ids", "file_refs", "summary_id", "include_history",
                           "output_style", "plan_item_id", "collect_plan_id")}
    expected = hashlib.sha256(json.dumps(legacy, sort_keys=True, ensure_ascii=False)
                              .encode("utf-8")).hexdigest()
    assert turn.request_hash() == expected
    assert ConversationTurn(text="x", agent_id="codex", client_request_id="k",
                            context_mode="native_resume").request_hash() != expected


# --------------------------------------------------------------------------- #
# configuration + a real child process (bundled stdio stub)
# --------------------------------------------------------------------------- #


def test_configured_appserver_transport_runs_bundled_stdio_stub(make_client, tmp_path, monkeypatch):
    from agenthub_kernel import adapters, config

    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv(config.ENV_CODEX_TRANSPORT, "app-server")
    monkeypatch.setenv(config.ENV_APPSERVER_STUB, "stream")
    adapter = adapters.build_adapter("codex")
    assert isinstance(adapter, AppServerCodexAdapter)
    info = adapters.describe(adapter)
    assert info["transport"] == "app-server" and info["native_resume"] is True
    assert info["execution_admission"]["mode"] == "simulation"
    assert info["real_process"] is False

    monkeypatch.delenv(config.ENV_APPSERVER_STUB)
    real = adapters.build_adapter("codex")
    assert adapters.describe(real)["execution_admission"]["allowed"] is False

    client = make_client(adapter=adapter)
    cid = conversation(client)
    turn, result = turn_and_run(client, cid, "one")
    assert result["status"] == "succeeded"
    assert messages(client, cid)[-1]["text"] == STREAMED
    record = adapter.lookup_by_invocation(turn["invocation_id"])
    assert record["payload"]["local_process_exit_confirmed"] is True
    second, result = turn_and_run(client, cid, "two", context_mode="native_resume")
    assert result["status"] == "succeeded" and second["resume_thread_id"] == "stub-thread-0001"
