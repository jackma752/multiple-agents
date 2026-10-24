"""T-1a: the kernel usage ledger through the Hub (offline stubs only).

One row per call that reached its adapter, written at settlement whether or
not there is a reply; views add known numbers only and count unknown calls.
"""

import json

import pytest
from sqlalchemy import delete, select

from agenthub.usage import COMPLETE, UNKNOWN, unknown_usage
from agenthub_adapters.appserver_stub import S3_REQUESTS, InMemoryStubFactory, add_usage
from agenthub_adapters.claude_code import (
    ClaudeCodeAdapter, FakeClaudeRunner, StubClaudeLauncher, result_object, scripted,
)
from agenthub_adapters.routing import RoutingAdapter
from agenthub_adapters.store import ExecutionStore
from agenthub_kernel.db import ChatMessageRow, UsageLedgerRow
from agenthub_kernel.usage_ledger import record_usage, restore_usage
from agenthub_rt.auth import Principal, authenticate
from tests.http.test_conversation_stream import (  # noqa: F401  (make_client is a fixture)
    AGENT, make_adapter, make_client, turn_and_run,
)
from tests.http.test_conversations import USER, conversation

S3_TURN = add_usage(*S3_REQUESTS)
SECRET = "prompt-secret-7f3a 用户原文"
CC_USAGE = {"input_tokens": 2, "cache_creation_input_tokens": 635, "cache_read_input_tokens": 1161,
            "output_tokens": 15}


def rows(client):
    with client.app.state.db.session() as s:
        return s.scalars(select(UsageLedgerRow)).all()


def usage_page(client, cid, headers=USER):
    return client.get(f"/api/conversations/{cid}/usage", headers=headers)


def turn_usage_of(client, cid, invocation_id):
    msgs = client.get(f"/api/conversations/{cid}/messages", headers=USER).json()["messages"]
    return next(m["invocation"]["usage"] for m in msgs
                if m["role"] == "user" and m["invocation"]["invocation_id"] == invocation_id)


def test_a_two_request_turn_reaches_the_ledger_whole(make_client):
    client = make_client(InMemoryStubFactory("usage-multi-request"))
    cid = conversation(client)
    turn, result = turn_and_run(client, cid, "one")
    assert result["status"] == "succeeded"
    (row,) = rows(client)
    assert (row.invocation_id, row.completeness, row.input_total, row.cache_read, row.output_total) == \
        (turn["invocation_id"], COMPLETE, 30539, 24832, 79)
    assert row.conversation_id == cid and row.agent_id == "codex" and row.raw == S3_TURN
    view = turn_usage_of(client, cid, turn["invocation_id"])
    assert view["input_total"] == 30539 and view["completeness"] == COMPLETE and "raw" not in view
    total = usage_page(client, cid).json()["total"]
    assert total["known"]["input_total"] == 30539 and total["complete_calls"] == 1


def test_a_failed_call_without_a_reply_is_still_recorded_d_t0_3(make_client):
    client = make_client(InMemoryStubFactory("turn-failed"))
    cid = conversation(client)
    turn, result = turn_and_run(client, cid, "one")
    assert result["status"] == "failed"
    msgs = client.get(f"/api/conversations/{cid}/messages", headers=USER).json()["messages"]
    assert [m["role"] for m in msgs] == ["user"]  # no reply
    view = turn_usage_of(client, cid, turn["invocation_id"])
    assert view["completeness"] == COMPLETE and view["output_total"] is not None


def test_an_unconfirmed_call_is_recorded_as_unknown_not_zero(make_client):
    client = make_client(InMemoryStubFactory("slow-terminal"), timeout_seconds=0.5)
    cid = conversation(client)
    turn, result = turn_and_run(client, cid, "one")
    assert result["status"] == "unknown"
    view = turn_usage_of(client, cid, turn["invocation_id"])
    assert view["completeness"] == UNKNOWN and view["input_total"] is None
    assert {"no_usage_reported", "turn_not_terminal"} <= set(view["notes"])
    total = usage_page(client, cid).json()["total"]
    assert total["unknown_calls"] == 1 and total["calls_missing"]["input_total"] == 1
    assert total["known"]["input_total"] == 0  # a sum of known numbers, labelled with the unknown count


def test_a_known_row_is_never_downgraded_and_never_duplicated(make_client):
    client = make_client(InMemoryStubFactory("usage-multi-request"))
    cid = conversation(client)
    turn, _ = turn_and_run(client, cid, "one")
    inv = turn["invocation_id"]
    adapter = client.app.state.worker.adapter
    with client.app.state.db.session() as s:
        record_usage(s, adapter, inv, record=unknown_usage("no_usage_reported"))
        record_usage(s, adapter, inv)  # the same settlement again
    (row,) = rows(client)
    assert row.completeness == COMPLETE and row.input_total == 30539
    assert restore_usage(client.app.state.db, adapter) == 0  # nothing missing


def test_restart_backfills_a_missing_row_and_shows_not_recorded_until_then(make_client):
    client = make_client(InMemoryStubFactory("happy"))
    cid = conversation(client)
    turn, _ = turn_and_run(client, cid, "one")
    with client.app.state.db.session() as s:
        s.execute(delete(UsageLedgerRow))
    view = turn_usage_of(client, cid, turn["invocation_id"])
    assert view["completeness"] == UNKNOWN and view["notes"] == ["not_recorded"]
    summary = client.post("/api/worker/recover", headers=USER).json()
    assert summary["usage_restored"] == 1
    assert turn_usage_of(client, cid, turn["invocation_id"])["completeness"] == COMPLETE
    assert len(rows(client)) == 1


def test_an_adapter_without_usage_reports_unknown(make_client):
    class NoUsage:
        """An adapter with no usage channel (as the Mock)."""

    client = make_client(InMemoryStubFactory("happy"))
    cid = conversation(client)
    turn, _ = turn_and_run(client, cid, "one")
    with client.app.state.db.session() as s:
        s.execute(delete(UsageLedgerRow))
        record_usage(s, NoUsage(), turn["invocation_id"])
    (row,) = rows(client)
    assert row.completeness == UNKNOWN and row.notes == ["adapter_reports_no_usage"]


def test_a_launch_that_raised_is_unknown(make_client, monkeypatch):
    client = make_client(InMemoryStubFactory("happy"))
    adapter = client.app.state.worker.adapter

    def boom(*_a, **_k):
        raise RuntimeError("transport exploded")

    monkeypatch.setattr(adapter, "start_streaming", boom)
    cid = conversation(client)
    turn, result = turn_and_run(client, cid, "one")
    assert result["status"] == "unknown"
    (row,) = rows(client)
    assert row.completeness == UNKNOWN and row.notes == ["adapter_start_raised"]


def test_no_record_after_a_refusal_in_this_process_is_a_known_zero(make_client):
    class Refuser:
        def usage_report(self, invocation_id):
            return None  # records every call before launching; has none

    client = make_client(InMemoryStubFactory("happy"))
    cid = conversation(client)
    turn, _ = turn_and_run(client, cid, "one")
    with client.app.state.db.session() as s:
        s.execute(delete(UsageLedgerRow))
        record_usage(s, Refuser(), turn["invocation_id"], refused_before_launch=True)
    (row,) = rows(client)
    assert row.completeness == COMPLETE and row.source == "not_launched" and row.input_total == 0
    with client.app.state.db.session() as s:
        s.execute(delete(UsageLedgerRow))
        record_usage(s, Refuser(), turn["invocation_id"])  # e.g. after a restart: no inference
    (row,) = rows(client)
    assert row.completeness == UNKNOWN and row.notes == ["no_adapter_record"]


def test_two_agents_are_summed_per_agent_with_their_own_mapping(make_client, tmp_path):
    claude = ClaudeCodeAdapter(
        launcher=StubClaudeLauncher("success-usage"), workdir_root=str(tmp_path / "cc-runs"),
        store=ExecutionStore(str(tmp_path / "cc.db"), table="claude_code_executions"),
        runner=FakeClaudeRunner(scripted(result_object(usage=CC_USAGE))))
    router = RoutingAdapter({"codex": make_adapter(tmp_path, InMemoryStubFactory("usage-multi-request")),
                             "claude-code": claude})
    client = make_client(adapter=router)
    cid = conversation(client)
    turn_and_run(client, cid, "one")
    turn_and_run(client, cid, "two", agent_id="claude-code")
    page = usage_page(client, cid).json()
    assert page["by_agent"]["claude-code"]["known"]["input_total"] == 1798  # not the "2" the page showed
    assert page["by_agent"]["codex"]["known"]["input_total"] == 30539
    assert page["total"]["known"]["input_total"] == 30539 + 1798
    assert page["total"]["known"]["cache_read"] == 24832 + 1161


@pytest.mark.parametrize("headers, status", [({}, 401), (AGENT, 403)])
def test_usage_requires_the_user(make_client, headers, status):
    client = make_client()
    cid = conversation(client)
    turn_and_run(client, cid, "one")
    assert usage_page(client, cid, headers).status_code == status


def test_other_users_cannot_read_usage(make_client):
    client = make_client()
    cid = conversation(client)
    turn_and_run(client, cid, "one")
    client.app.dependency_overrides[authenticate] = lambda: Principal("other-user", "user")
    try:
        assert usage_page(client, cid).status_code == 404
    finally:
        client.app.dependency_overrides.clear()


def test_the_ledger_holds_no_prompt_or_reply_text(make_client):
    client = make_client(InMemoryStubFactory("stream"))
    cid = conversation(client)
    turn_and_run(client, cid, "one", text=SECRET)
    with client.app.state.db.session() as s:
        dump = json.dumps([{c.name: str(getattr(r, c.name)) for c in UsageLedgerRow.__table__.columns}
                           for r in s.scalars(select(UsageLedgerRow)).all()], ensure_ascii=False)
        reply = s.scalar(select(ChatMessageRow.text).where(ChatMessageRow.role == "assistant"))
    assert reply and reply not in dump and "流式" not in dump
    assert "prompt-secret" not in dump and "用户原文" not in dump
