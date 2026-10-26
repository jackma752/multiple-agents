"""P4: controlled collaboration - plan, dispatch in dependency order, collect (offline stubs).

Codex is the in-memory app-server stub (replies "OK"); Claude Code an
in-process fake (replies CC_REPLY and records its prompts). Every sub-task is
its own depth-1 call with its own approval; nothing runs by itself.
"""

import datetime as dt

from sqlalchemy import func, select

from agenthub.collab import COLLECT_INSTRUCTION
from agenthub_adapters.appserver_stub import InMemoryStubFactory
from agenthub_kernel.db import ChatMessageRow, InvocationRow, TaskRow
from tests.http import test_multi_agent as multi
from tests.http.test_conversation_stream import held_slots, make_client  # noqa: F401  (fixture)
from tests.http.test_conversations import USER
from tests.http.test_multi_agent import CC_REPLY, hub  # noqa: F401  (fixture)

PLAN = [{"agent_id": "codex", "goal": "Write the parser tests"},
        {"agent_id": "claude-code", "goal": "Review the tests", "depends_on": [1]}]


def started(client, cid):
    """A conversation with one finished main-agent call (so the task tree exists)."""
    multi.approve_and_run(client, multi.submit(client, cid, "codex", "Let us start", "k0").json())
    return client.get(f"/api/conversations/{cid}/messages", headers=USER).json()["root_task_id"]


def set_policy(client, root, **changes):
    r = client.patch(f"/api/tasks/{root}/policy", headers=USER, json={"collaboration_enabled": True, **changes})
    assert r.status_code == 200, r.text


def save_plan(client, cid, items=PLAN, **extra):
    return client.post(f"/api/conversations/{cid}/plans", headers=USER, json={"items": items, **extra})


def dispatch(client, cid, plan_id, index, key):
    return client.post(f"/api/conversations/{cid}/plans/{plan_id}/items/{index}/dispatch", headers=USER,
                       json={"client_request_id": key})


def approve_run(client, turn):
    assert client.post(f"/api/approvals/{turn['approval_id']}/approve", headers=USER).status_code == 200
    return client.post(f"/api/invocations/{turn['invocation_id']}/run", params={"wait": "true"}, headers=USER)


def plan_of(client, cid):
    return client.get(f"/api/conversations/{cid}/plans", headers=USER).json()


def calls(client):
    with client.app.state.db.session() as s:
        return s.scalar(select(func.count()).select_from(InvocationRow))


def test_the_switch_must_be_on_to_save_or_dispatch(hub):
    client, _router, _prompts = hub
    cid = multi.conversation(client)
    r = save_plan(client, cid)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "collaboration_disabled"  # no task yet
    root = started(client, cid)
    assert save_plan(client, cid).json()["detail"]["code"] == "collaboration_disabled"  # off by default
    set_policy(client, root)
    plan = save_plan(client, cid).json()
    set_policy(client, root, collaboration_enabled=False)
    before = calls(client)
    r = dispatch(client, cid, plan["plan_id"], 1, "d1")
    assert r.status_code == 409 and r.json()["detail"]["code"] == "collaboration_disabled"
    assert calls(client) == before


def test_plan_dispatch_in_dependency_order_then_collect(hub):
    client, router, prompts = hub
    cid = multi.conversation(client)
    root = started(client, cid)
    set_policy(client, root)
    plan = save_plan(client, cid).json()
    assert [i["state"] for i in plan["items"]] == ["pending", "pending"]
    assert [i["ready"] for i in plan["items"]] == [True, False]
    pid = plan["plan_id"]

    before = calls(client)
    r = dispatch(client, cid, pid, 2, "early")  # depends on 1
    assert r.status_code == 409 and r.json()["detail"]["code"] == "dependency_pending"
    assert calls(client) == before

    first = dispatch(client, cid, pid, 1, "d1").json()
    assert first["status"] == "awaiting_approval" and first["plan_item"] == {"plan_id": pid, "index": 1, "attempt": 1}
    assert first["depth"] == 1 and first["sent_text"] is None  # just its goal: nothing else sent
    assert plan_of(client, cid)["plans"][0]["items"][0]["state"] == "awaiting"
    assert approve_run(client, first).json()["status"] == "succeeded"
    assert dispatch(client, cid, pid, 1, "again").json()["detail"]["code"] == "item_done"

    second = dispatch(client, cid, pid, 2, "d2").json()
    sent = second["sent_text"]  # the result it depends on, handed over, then its goal
    assert "--- Handoff: reply of codex" in sent and "\nOK\n" in sent and sent.endswith("\n\nReview the tests")
    assert approve_run(client, second).json()["status"] == "succeeded"
    assert prompts == [sent]

    view = plan_of(client, cid)["plans"][0]
    assert view["all_done"] and [i["state"] for i in view["items"]] == ["done", "done"]
    collect = client.post(f"/api/conversations/{cid}/plans/{pid}/collect", headers=USER,
                          json={"agent_id": "codex", "client_request_id": "c1"}).json()
    assert collect["depth"] == 0 and collect["sent_text"].endswith(COLLECT_INSTRUCTION)
    assert "\nOK\n" in collect["sent_text"] and CC_REPLY in collect["sent_text"]
    assert approve_run(client, collect).json()["status"] == "succeeded"
    with client.app.state.db.session() as s:
        kinds = [t.kind for t in s.scalars(select(TaskRow).where(TaskRow.parent_task_id == root))]
    assert kinds == ["user", "user"]  # a hand-written plan: user-dispatched sub-tasks


def test_a_failed_item_may_be_retried_once(hub):
    client, _router, _prompts = hub
    cid = multi.conversation(client)
    set_policy(client, started(client, cid))
    pid = save_plan(client, cid, items=PLAN[:1]).json()["plan_id"]
    for attempt, key in ((1, "a1"), (2, "a2")):
        turn = dispatch(client, cid, pid, 1, key).json()
        assert turn["plan_item"]["attempt"] == attempt
        assert client.post(f"/api/approvals/{turn['approval_id']}/reject", headers=USER).status_code == 200
        assert plan_of(client, cid)["plans"][0]["items"][0]["state"] == "failed"
    r = dispatch(client, cid, pid, 1, "a3")
    assert r.status_code == 409 and r.json()["detail"]["code"] == "retry_limit"


def test_same_second_retry_is_latest_even_when_its_random_id_sorts_first(hub, monkeypatch):
    from agenthub_kernel import service

    client, _router, _prompts = hub
    cid = multi.conversation(client)
    set_policy(client, started(client, cid))
    pid = save_plan(client, cid, items=PLAN[:1]).json()["plan_id"]
    original_id = service.new_id
    ids = iter(("inv_z_first", "inv_a_retry"))
    monkeypatch.setattr(service, "new_id", lambda prefix: next(ids) if prefix == "inv" else original_id(prefix))
    first = dispatch(client, cid, pid, 1, "failed").json()
    client.post(f"/api/approvals/{first['approval_id']}/reject", headers=USER)
    retry = dispatch(client, cid, pid, 1, "retry").json()
    with client.app.state.db.session() as session:
        for turn in (first, retry):
            session.get(InvocationRow, turn["invocation_id"]).created_at = dt.datetime(2026, 10, 26)
    item = plan_of(client, cid)["plans"][0]["items"][0]
    assert item["invocation_id"] == retry["invocation_id"] and item["state"] == "awaiting"
    assert approve_run(client, retry).json()["status"] == "succeeded"
    assert plan_of(client, cid)["plans"][0]["all_done"]


def test_an_unknown_call_is_unconfirmed_and_not_retried_until_adjudicated(make_client):
    client = make_client(InMemoryStubFactory("slow-terminal"), timeout_seconds=0.5)
    cid = multi.conversation(client)
    assert client.post(f"/api/conversations/{cid}/invocations", headers=USER, json={
        "text": "start", "agent_id": "codex", "client_request_id": "k0"}).status_code == 200  # creates the task
    set_policy(client, client.get(f"/api/conversations/{cid}/messages", headers=USER).json()["root_task_id"])
    pid = save_plan(client, cid, items=[{"agent_id": "codex", "goal": "Do it"}]).json()["plan_id"]
    turn = dispatch(client, cid, pid, 1, "u1").json()
    assert approve_run(client, turn).json()["status"] == "unknown"
    item = plan_of(client, cid)["plans"][0]["items"][0]
    assert item["state"] == "unconfirmed" and item["ready"] is False  # neither a failure nor retryable
    adapter = client.app.state.worker.adapter
    starts, before, slots = adapter.start_count, calls(client), held_slots(client)
    r = dispatch(client, cid, pid, 1, "u2")
    assert r.status_code == 409 and r.json()["detail"]["code"] == "item_unconfirmed"
    assert calls(client) == before and adapter.start_count == starts
    assert held_slots(client) == slots  # the unknown call keeps its slot
    # Only once the user confirms how it ended may it be dispatched again.
    r = client.post(f"/api/invocations/{turn['invocation_id']}/reconcile", headers=USER,
                    json={"action": "settle", "outcome": "failed"})
    assert r.status_code == 200, r.text
    item = plan_of(client, cid)["plans"][0]["items"][0]
    assert item["state"] == "failed" and item["ready"] is True
    again = dispatch(client, cid, pid, 1, "u3").json()
    assert again["status"] == "awaiting_approval" and again["plan_item"]["attempt"] == 2


def test_the_sub_agent_limits_still_apply_when_it_runs(hub):
    client, router, _prompts = hub
    cid = multi.conversation(client)
    set_policy(client, started(client, cid), max_subagents_total=1, max_subagents_concurrent=1)
    pid = save_plan(client, cid, items=[{"agent_id": "codex", "goal": "a"}, {"agent_id": "codex", "goal": "b"}]).json()["plan_id"]
    assert approve_run(client, dispatch(client, cid, pid, 1, "x1").json()).json()["status"] == "succeeded"
    codex = router.for_agent("codex")
    starts = codex.start_count
    second = dispatch(client, cid, pid, 2, "x2").json()
    r = approve_run(client, second)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "max_subagents_total"
    assert codex.start_count == starts


def test_the_main_agents_proposal_reads_back_and_its_items_are_auto(hub):
    client, _router, _prompts = hub
    cid = multi.conversation(client)
    root = started(client, cid)
    set_policy(client, root)
    instruction = plan_of(client, cid)["instruction"]
    assert "claude-code" in instruction and "codex" in instruction
    with client.app.state.db.session() as s:
        reply = ChatMessageRow(conversation_id=cid, role="assistant",
                               text="1. codex: Draft it\n2. claude-code: Check it (after 1)\n3. other: x")
        s.add(reply)
        s.commit()
        reply_id = reply.id
    draft = client.get(f"/api/conversations/{cid}/plan-draft", headers=USER, params={"message_id": reply_id}).json()
    assert [i["agent_id"] for i in draft["items"]] == ["codex", "claude-code", "other"]
    assert draft["items"][2]["unknown_agent"] is True
    assert save_plan(client, cid, items=draft["items"]).status_code == 422  # unknown agent must be fixed
    items = [{k: i[k] for k in ("agent_id", "goal", "depends_on")} for i in draft["items"][:2]]
    plan = save_plan(client, cid, items=items, from_message_id=reply_id).json()
    assert plan["source"] == f"reply:{reply_id}"
    approve_run(client, dispatch(client, cid, plan["plan_id"], 1, "p1").json())
    with client.app.state.db.session() as s:
        assert [t.kind for t in s.scalars(select(TaskRow).where(TaskRow.parent_task_id == root))] == ["auto"]
