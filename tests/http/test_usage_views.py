"""T-1b: usage views over the ledger - root task, per agent, per day, instance.

Offline stubs; extra tree rows (a sub-task, another user's conversation) are
written straight into the database. Every figure is a query over ledger rows:
nothing is booked twice, and unknown calls are counted, never added as 0.
"""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import delete

from agenthub.usage import COMPLETE, MAPPING_CLAUDE_CODE, UsageRecord
from agenthub_adapters.appserver_stub import InMemoryStubFactory
from agenthub_kernel.db import ConversationRow, InvocationRow, TaskRow, UsageLedgerRow
from agenthub_kernel.usage_ledger import record_usage
from tests.http.test_conversation_stream import (  # noqa: F401  (make_client is a fixture)
    AGENT, make_client, turn_and_run,
)
from tests.http.test_conversations import USER, conversation

TODAY = datetime.now(timezone.utc).date()


def summary(client, headers=USER, **params):
    return client.get("/api/usage/summary", headers=headers, params=params)


def conv_usage(client, cid):
    return client.get(f"/api/conversations/{cid}/usage", headers=USER).json()


def known(total, field="input_total"):
    return total["known"][field]


def add_call(session, root_task_id, *, task_id, depth, agent, record, parent=None, status="succeeded"):
    """A call of the tree that has no chat message (e.g. a sub-agent task)."""
    if session.get(TaskRow, task_id) is None:
        session.add(TaskRow(id=task_id, root_task_id=root_task_id, parent_task_id=parent, depth=depth,
                            kind="user", goal="hidden goal text", agent_id=agent, status="running"))
    inv_id = f"inv-{task_id}"
    session.add(InvocationRow(id=inv_id, task_id=task_id, payload_hash="h", status=status,
                              snapshot={"agent_id": agent, "task_input": "hidden input"}))
    session.flush()
    record_usage(session, None, inv_id, record=record)
    return inv_id


def cc_record(total_in=1798):
    return UsageRecord(mapping=MAPPING_CLAUDE_CODE, completeness=COMPLETE, source="cli_result",
                       input_total=total_in, input_uncached=2, cache_read=1161, cache_write=635,
                       output_total=15)


def test_root_task_view_includes_sub_task_calls_the_chat_view_does_not(make_client):
    client = make_client(InMemoryStubFactory("usage-multi-request"))
    cid = conversation(client)
    turn_and_run(client, cid, "one")
    root = conv_usage(client, cid)["root_task"]["root_task_id"]
    with client.app.state.db.session() as s:
        add_call(s, root, task_id="sub-1", depth=1, parent=root, agent="claude-code", record=cc_record())
    page = conv_usage(client, cid)
    assert page["total"]["calls"] == 1 and known(page["total"]) == 30539
    tree = page["root_task"]
    assert tree["total"]["calls"] == 2 and known(tree["total"]) == 30539 + 1798
    assert [(t["depth"], t["agent_id"], t["total"]["calls"]) for t in tree["by_task"]] == \
        [(0, "codex", 1), (1, "claude-code", 1)]
    assert known(tree["by_agent"]["claude-code"]) == 1798
    assert "hidden goal text" not in str(page) and "hidden input" not in str(page)


def test_instance_view_covers_only_this_users_conversations(make_client):
    client = make_client(InMemoryStubFactory("usage-multi-request"))
    first, second = conversation(client), conversation(client)
    turn_and_run(client, first, "one")
    turn_and_run(client, second, "two")
    with client.app.state.db.session() as s:  # another user's conversation and call
        s.add(TaskRow(id="other-root", root_task_id="other-root", parent_task_id=None, depth=0,
                      kind="user", goal="g", agent_id="codex", status="running"))
        s.add(ConversationRow(id="conv-other", user_id="other-user", title="theirs", root_task_id="other-root"))
        s.flush()
        add_call(s, "other-root", task_id="other-root", depth=0, agent="codex", record=cc_record(999999))
    page = summary(client).json()
    assert page["scope"] == "instance" and "projects" in page["scope_note"]
    assert page["total"]["calls"] == 2 and known(page["total"]) == 2 * 30539
    assert {c["conversation_id"] for c in page["by_conversation"]} == {first, second}
    assert page["by_day"] == [{"day": TODAY.isoformat(), **page["total"]}]
    assert known(page["by_agent"]["codex"]) == 2 * 30539
    assert "theirs" not in str(page) and "999999" not in str(page)


def test_breakdowns_add_up_to_the_total_without_double_counting(make_client):
    client = make_client(InMemoryStubFactory("usage-multi-request"))
    cid = conversation(client)
    turn_and_run(client, cid, "one")
    turn_and_run(client, cid, "two")
    for _ in range(2):  # restarts re-run settlement backfills; rows are per call
        client.post("/api/worker/recover", headers=USER)
    page = summary(client).json()
    total = known(page["total"])
    assert total == 2 * 30539
    assert sum(known(t) for t in page["by_agent"].values()) == total
    assert sum(d["known"]["input_total"] for d in page["by_day"]) == total
    assert sum(known(c["total"]) for c in page["by_conversation"]) == total


def test_unknown_and_unrecorded_calls_are_counted_never_zero(make_client):
    client = make_client(InMemoryStubFactory("slow-terminal"), timeout_seconds=0.5)
    cid = conversation(client)
    turn, result = turn_and_run(client, cid, "one")
    assert result["status"] == "unknown"
    page = summary(client).json()
    assert page["total"]["unknown_calls"] == 1 and page["total"]["calls_missing"]["input_total"] == 1
    with client.app.state.db.session() as s:
        s.execute(delete(UsageLedgerRow))  # e.g. settled before T-1a
    page = summary(client).json()
    assert page["total"]["calls"] == 1 and page["total"]["unknown_calls"] == 1


def test_day_filters(make_client):
    client = make_client(InMemoryStubFactory("usage-multi-request"))
    cid = conversation(client)
    turn_and_run(client, cid, "one")
    tomorrow, yesterday = TODAY + timedelta(days=1), TODAY - timedelta(days=1)
    assert summary(client, since=TODAY.isoformat()).json()["total"]["calls"] == 1
    assert summary(client, since=tomorrow.isoformat()).json()["total"]["calls"] == 0
    assert summary(client, until=yesterday.isoformat()).json()["total"]["calls"] == 0
    assert summary(client, since=tomorrow.isoformat(), until=yesterday.isoformat()).status_code == 422
    assert summary(client, since="not-a-date").status_code == 422


@pytest.mark.parametrize("headers, status", [({}, 401), (AGENT, 403)])
def test_instance_view_requires_the_user(make_client, headers, status):
    client = make_client()
    assert summary(client, headers=headers).status_code == status


def test_page_has_the_usage_views():
    from pathlib import Path

    page = (Path(__file__).resolve().parents[2] / "apps" / "api" / "agenthub_rt" / "static"
            / "chat.html").read_text(encoding="utf-8")
    for needle in ('id="usageConv"', 'id="usageAll"', "/api/usage/summary", "另有 ${t.unknown_calls} 次未知",
                   "根任务（含子任务调用）", "未区分项目"):
        assert needle in page
