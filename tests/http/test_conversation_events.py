"""Ordered conversation events, cursor replay and SSE; offline, no model calls."""

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from agenthub_adapters.mock import MockAdapter
from agenthub_kernel.db import (
    Base, ChatMessageRow, ConversationEventCursorRow, ConversationEventRow,
    ConversationRow, Database, QuotaReservationRow,
)
from agenthub_rt.api import create_app
from agenthub_rt.auth import Principal, authenticate
from tests.http.test_conversations import (  # noqa: F401  (client is a fixture)
    AGENT, USER, ReplyAdapter, approve, client, conversation, messages, run, submit,
)

SECRET_TEXT = "private wording 机密 that must stay in chat_messages only"


def events(client, cid, after_seq=0, limit=500, headers=USER):
    r = client.get(f"/api/conversations/{cid}/events",
                   params={"after_seq": after_seq, "limit": limit}, headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


def types(page):
    return [e["type"] for e in page["events"]]


def parse_sse(raw):
    frames = []
    for block in raw.split("\n\n"):
        fields = {}
        for line in block.splitlines():
            if line.startswith(":") or ":" not in line:
                continue
            key, _, value = line.partition(":")
            fields[key] = value[1:] if value.startswith(" ") else value
        if "data" in fields:
            frames.append({"id": int(fields["id"]), "event": fields["event"],
                           "data": json.loads(fields["data"])})
    return frames


def stream(client, cid, headers=USER, **params):
    params.setdefault("timeout", 0)
    with client.stream("GET", f"/api/conversations/{cid}/events/stream",
                       params=params, headers=headers) as r:
        body = r.read().decode("utf-8")
        return r.status_code, r.headers.get("content-type", ""), body


def test_lifecycle_is_ordered_dense_and_carries_no_chat_text(client):
    cid = conversation(client)
    turn = submit(client, cid, text=SECRET_TEXT)
    approve(client, turn)
    assert run(client, turn).json()["status"] == "succeeded"
    page = events(client, cid)
    assert [e["seq"] for e in page["events"]] == list(range(1, len(page["events"]) + 1))
    assert page["last_seq"] == len(page["events"]) and page["has_more"] is False
    assert types(page) == [
        "conversation.created", "message.created", "approval.status",
        "invocation.status", "invocation.status", "invocation.status",
        "invocation.status", "message.created",
    ]
    statuses = [(e["payload"]["from"], e["payload"]["to"]) for e in page["events"]
                if e["type"] == "invocation.status"]
    assert statuses == [("awaiting_approval", "queued"), ("queued", "starting"),
                        ("starting", "running"), ("running", "succeeded")]
    created = page["events"][1]["payload"]
    assert created["invocation_id"] == turn["invocation_id"]
    assert created["invocation_status"] == "awaiting_approval"
    assert created["approval_status"] == "pending"
    # Consumption is a Core CAS; the stored status is reported, not invented.
    assert page["events"][4]["payload"]["approval_status"] == "consumed"
    assert page["events"][-1]["payload"]["role"] == "assistant"
    dumped = json.dumps(page, ensure_ascii=False)
    assert SECRET_TEXT not in dumped and "synthetic reply" not in dumped
    # The events agree with the message history they point at.
    history = messages(client, cid)
    assert [m["message_id"] for m in history] == [
        e["payload"]["message_id"] for e in page["events"] if e["type"] == "message.created"
    ]


def test_cursor_paging_replays_exact_tail_and_rejects_cursor_ahead(client):
    cid = conversation(client)
    for i in range(5):
        client.post(f"/api/conversations/{cid}/messages", json={"text": f"m{i}"}, headers=USER)
    full = events(client, cid)["events"]
    assert len(full) == 6
    first = events(client, cid, limit=2)
    assert first["has_more"] is True and first["next_after_seq"] == 2
    collected, cursor = list(first["events"]), first["next_after_seq"]
    while True:
        page = events(client, cid, after_seq=cursor, limit=2)
        collected += page["events"]
        cursor = page["next_after_seq"]
        if not page["has_more"]:
            break
    assert collected == full
    empty = events(client, cid, after_seq=6)
    assert empty["events"] == [] and empty["next_after_seq"] == 6
    r = client.get(f"/api/conversations/{cid}/events", params={"after_seq": 7}, headers=USER)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "cursor_ahead"
    assert r.json()["detail"]["last_seq"] == 6
    for bad in ({"after_seq": -1}, {"limit": 0}, {"limit": 501}):
        assert client.get(f"/api/conversations/{cid}/events", params=bad,
                          headers=USER).status_code == 422


@pytest.mark.parametrize("headers,status", [({}, 401), (AGENT, 403)])
def test_event_routes_require_user(client, headers, status):
    cid = conversation(client)
    assert client.get(f"/api/conversations/{cid}/events", headers=headers).status_code == status
    assert stream(client, cid, headers=headers)[0] == status


def test_other_owner_and_unknown_conversation_events_are_hidden(client):
    cid = conversation(client)
    client.app.dependency_overrides[authenticate] = lambda: Principal("other-user", "user")
    try:
        for target in (cid, "missing"):
            assert client.get(f"/api/conversations/{target}/events", headers=USER).status_code == 404
            assert stream(client, target)[0] == 404
    finally:
        client.app.dependency_overrides.clear()


def test_rolled_back_change_leaves_no_event_and_no_seq_gap(client):
    cid = conversation(client)
    db = client.app.state.db
    with pytest.raises(RuntimeError):
        with db.session() as s:
            s.add(ChatMessageRow(conversation_id=cid, role="user", text="lost"))
            s.flush()  # The event row is written by this flush ...
            raise RuntimeError("synthetic failure")  # ... and must vanish with it.
    page = events(client, cid)
    assert types(page) == ["conversation.created"] and page["last_seq"] == 1
    client.post(f"/api/conversations/{cid}/messages", json={"text": "kept"}, headers=USER)
    assert [e["seq"] for e in events(client, cid)["events"]] == [1, 2]


def test_failed_submission_rolls_back_its_events(client, monkeypatch):
    from agenthub_kernel.service import PolicyService

    cid = conversation(client)
    monkeypatch.setattr(PolicyService, "create_approval",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("synthetic")))
    with pytest.raises(RuntimeError):
        submit(client, cid)
    assert types(events(client, cid)) == ["conversation.created"]


def test_idempotent_replay_does_not_emit_again(client):
    cid = conversation(client)
    submit(client, cid)
    before = events(client, cid)["last_seq"]
    assert submit(client, cid)["replayed"] is True
    assert events(client, cid)["last_seq"] == before


def test_concurrent_writers_get_unique_dense_seq_in_commit_order(client):
    cid = conversation(client)
    def post(i):
        r = client.post(f"/api/conversations/{cid}/messages", json={"text": f"c{i}"}, headers=USER)
        assert r.status_code == 200, r.text
    with ThreadPoolExecutor(8) as pool:
        list(pool.map(post, range(24)))
    page = events(client, cid)
    assert [e["seq"] for e in page["events"]] == list(range(1, 26))
    ids = [e["payload"]["message_id"] for e in page["events"][1:]]
    assert ids == sorted(ids) and len(set(ids)) == 24
    with client.app.state.db.session() as s:
        assert s.get(ConversationEventCursorRow, cid).last_seq == 25


def test_streams_are_per_conversation(client):
    a, b = conversation(client), conversation(client)
    client.post(f"/api/conversations/{a}/messages", json={"text": "a"}, headers=USER)
    assert events(client, a)["last_seq"] == 2
    assert types(events(client, b)) == ["conversation.created"]


def test_unknown_stop_and_retry_are_visible_without_inventing_outcomes(client):
    cid = conversation(client)
    turn = submit(client, cid)
    approve(client, turn)
    client.app.state.adapter.outcome = "unknown"
    assert run(client, turn).json()["status"] == "unknown"
    page = events(client, cid)
    assert page["events"][-1]["type"] == "invocation.status"
    assert page["events"][-1]["payload"]["to"] == "unknown"
    assert all(e["payload"].get("role") != "assistant" for e in page["events"])
    with client.app.state.db.session() as s:
        assert s.scalar(select(QuotaReservationRow)).released_at is None
    # External terminal evidence permits a user retry: new approval, no start.
    client.app.state.adapter.store.set_status(turn["invocation_id"], "failed")
    r = client.post(f"/api/invocations/{turn['invocation_id']}/reconcile",
                    headers=USER, json={"action": "retry"})
    assert r.status_code == 200, r.text
    tail = events(client, cid, after_seq=page["last_seq"])["events"]
    by_type = {e["type"]: e["payload"] for e in tail}
    assert by_type["invocation.status"]["to"] == "failed"
    retry_msg = by_type["message.created"]
    assert retry_msg["invocation_id"] == r.json()["retry_invocation_id"]
    assert retry_msg["approval_status"] == "pending"
    assert by_type["task.retry_requested"]["retry_invocation_id"] == r.json()["retry_invocation_id"]
    assert "stop_evidence" not in by_type["task.retry_requested"]
    assert client.app.state.adapter.start_count == 1
    stop = client.post(f"/api/tasks/{turn['task_id']}/stop", headers=USER)
    assert stop.status_code == 200, stop.text
    last = events(client, cid)["events"]
    assert "task.tree_stop" in [e["type"] for e in last]


def test_sse_replays_backlog_and_resumes_from_last_event_id(client):
    cid = conversation(client)
    for i in range(3):
        client.post(f"/api/conversations/{cid}/messages", json={"text": f"s{i}"}, headers=USER)
    code, ctype, body = stream(client, cid)
    assert code == 200 and ctype.startswith("text/event-stream")
    assert body.startswith("retry: 3000\n\n")
    frames = parse_sse(body)
    assert [f["id"] for f in frames] == [1, 2, 3, 4]
    assert frames == [{"id": e["seq"], "event": e["type"], "data": e}
                      for e in events(client, cid)["events"]]
    # Last-Event-ID (browser reconnect) wins over the query cursor.
    _, _, resumed = stream(client, cid, headers={**USER, "Last-Event-ID": "2"}, after_seq=0)
    assert [f["id"] for f in parse_sse(resumed)] == [3, 4]
    assert parse_sse(stream(client, cid, after_seq=4)[2]) == []
    assert stream(client, cid, headers={**USER, "Last-Event-ID": "abc"})[0] == 400
    assert stream(client, cid, after_seq=9)[0] == 409


def test_sse_follows_new_events_live_then_closes(client):
    client.app.state.sse_poll_seconds = 0.05
    cid = conversation(client)
    result = {}
    def reader():
        result["body"] = stream(client, cid, after_seq=1, timeout=3)[2]
    t = threading.Thread(target=reader)
    t.start()
    time.sleep(0.3)
    turn = submit(client, cid)
    approve(client, turn)
    run(client, turn)
    t.join(10)
    assert not t.is_alive()
    got = parse_sse(result["body"])
    expected = events(client, cid, after_seq=1)["events"]
    assert [f["id"] for f in got] == [e["seq"] for e in expected]
    assert got[-1]["data"]["payload"]["role"] == "assistant"


def test_sse_backlog_larger_than_one_batch(client):
    cid = conversation(client)
    with client.app.state.db.session() as s:
        for i in range(450):
            s.add(ChatMessageRow(conversation_id=cid, role="user", text=f"b{i}"))
    frames = parse_sse(stream(client, cid)[2])
    assert [f["id"] for f in frames] == list(range(1, 452))


def test_events_survive_restart_and_continue_numbering(client, tmp_path):
    cid = conversation(client)
    client.post(f"/api/conversations/{cid}/messages", json={"text": "before"}, headers=USER)
    restarted = create_app(db_url=client.app.state.db.url, adapter=MockAdapter())
    with TestClient(restarted) as c:
        assert events(c, cid)["last_seq"] == 2
        c.post(f"/api/conversations/{cid}/messages", json={"text": "after"}, headers=USER)
        assert [e["seq"] for e in events(c, cid)["events"]] == [1, 2, 3]
    restarted.state.db.engine.dispose()


def test_events_schema_is_additive_for_existing_conversations(tmp_path):
    url = f"sqlite:///{(tmp_path / 'old.db').as_posix()}"
    db = Database(url)
    Base.metadata.create_all(db.engine, tables=[
        t for t in Base.metadata.sorted_tables
        if t.name not in {"conversation_events", "conversation_event_cursors"}
    ])
    with db.session() as s:  # Written before the events schema exists: no capture.
        s.add(ConversationRow(id="conv-old", user_id="local-user", title="old"))
        s.flush()
        s.add(ChatMessageRow(conversation_id="conv-old", role="user", text="old text"))
    db.engine.dispose()
    app = create_app(db_url=url, adapter=MockAdapter())
    with TestClient(app) as c:
        assert events(c, "conv-old")["events"] == []
        assert len(messages(c, "conv-old")) == 1
        c.post("/api/conversations/conv-old/messages", json={"text": "new"}, headers=USER)
        page = events(c, "conv-old")
        assert [e["seq"] for e in page["events"]] == [1]
        assert page["events"][0]["payload"]["message_id"] == 2
        with app.state.db.session() as s:
            assert s.scalar(select(func.count()).select_from(ConversationEventRow)) == 1
    app.state.db.engine.dispose()


def test_event_capture_never_launches_or_approves(client):
    cid = conversation(client)
    turn = submit(client, cid)
    for _ in range(3):
        events(client, cid)
        stream(client, cid)
    assert client.app.state.adapter.start_count == 0
    page = events(client, cid)
    assert "approval.status" not in types(page)
    r = client.get(f"/api/conversations/{cid}/messages", headers=USER).json()
    assert r["messages"][0]["invocation"]["approval_status"] == "pending"
    assert turn["status"] == "awaiting_approval"
