"""T-1c: token budgets enforced at the consume gate (offline stubs only).

Critical-path invariants (AGENTS.md): a refused call starts its adapter 0
times, leaves its approval unconsumed and holds no slot or reservation; two
calls cannot together cross the same remaining budget; unknown spend keeps
its reservation; a reservation survives a restart.
"""

import threading

import pytest
from sqlalchemy import select

from agenthub.budget import BudgetDecision
from agenthub_adapters.appserver_stub import InMemoryStubFactory
from agenthub_kernel import budget as budget_module
from agenthub_kernel.adapters import describe
from agenthub_kernel.db import ApprovalRow, TokenBudgetItemRow, TreeQuotaRow, TreeTokenBudgetRow
from agenthub_kernel.service import PolicyService
from tests.http.test_conversation_stream import (  # noqa: F401  (make_client is a fixture)
    held_slots, make_client, submit, turn_and_run,
)
from tests.http.test_conversations import USER, approve, conversation, run

S3_TOKENS = 30539 + 79      # usage-multi-request: one turn, two requests
PROMPT = "budget check"


def root_of(client, cid):
    return client.get(f"/api/conversations/{cid}/messages", headers=USER).json()["root_task_id"]


def set_budget(client, root, *, total=None, per_call=None, mode="strict", warn=80):
    r = client.patch(f"/api/tasks/{root}/policy", headers=USER, json={
        "token_budget_total": total, "token_budget_per_call": per_call,
        "token_budget_mode": mode, "token_budget_warn_percent": warn})
    assert r.status_code == 200, r.text
    return r.json()


def budgeted(client, **budget):
    """A conversation whose tree has this budget (its first, revoked call opened the tree)."""
    cid = conversation(client)
    submit(client, cid, key="opener", text=PROMPT)
    result = set_budget(client, root_of(client, cid), **budget)
    assert result["revoked_approvals"] == 1  # a budget change voids unconsumed approvals
    return cid


def counters(client, cid):
    with client.app.state.db.session() as s:
        row = s.get(TreeTokenBudgetRow, root_of(client, cid))
        return None if row is None else (row.known, row.unsettled, row.reserved, row.unbounded)


def item(client, invocation_id):
    with client.app.state.db.session() as s:
        row = s.get(TokenBudgetItemRow, invocation_id)
        return None if row is None else {c.name: getattr(row, c.name) for c in row.__table__.columns}


def approval_status(client, turn):
    with client.app.state.db.session() as s:
        return s.get(ApprovalRow, turn["approval_id"]).status


def adapter(client):
    return client.app.state.worker.adapter


def refused(client, cid, key, code, text=PROMPT):
    turn = submit(client, cid, key=key, text=text)
    approve(client, turn)
    starts, slots, before = adapter(client).start_count, held_slots(client), counters(client, cid)
    invocations = tree_invocations(client, cid)
    r = run(client, turn)
    assert r.status_code == 409 and r.json()["detail"]["code"] == code, r.text
    assert adapter(client).start_count == starts          # never launched
    assert approval_status(client, turn) == "approved"    # not consumed
    assert item(client, turn["invocation_id"]) is None    # nothing reserved
    assert held_slots(client) == slots                    # no slot taken
    assert tree_invocations(client, cid) == invocations   # no invocation quota used
    assert counters(client, cid) == before                # the refusal rolled everything back
    return turn


def tree_invocations(client, cid):
    with client.app.state.db.session() as s:
        row = s.get(TreeQuotaRow, root_of(client, cid))
        return None if row is None else row.total_invocations


def spend(client, cid):
    """The tree's spend as the views compute it (no write)."""
    b = client.get(f"/api/conversations/{cid}/usage", headers=USER).json()["budget"]
    return b["known"], b["unsettled"], b["reserved"], b["unbounded"]


def test_strict_refuses_a_reservation_that_would_cross_the_total(make_client):
    client = make_client(InMemoryStubFactory("usage-multi-request"))
    cid = budgeted(client, total=50000, per_call=40000)
    turn, result = turn_and_run(client, cid, "a", text=PROMPT)
    assert result["status"] == "succeeded"
    assert item(client, turn["invocation_id"])["reservation"] == 40000
    assert counters(client, cid) == (S3_TOKENS, 0, 0, 0)  # reservation returned, spend known
    refused(client, cid, "b", "token_budget_exceeded")    # 30,618 + 40,000 > 50,000
    assert counters(client, cid) == (S3_TOKENS, 0, 0, 0)


def test_the_reservation_itself_is_atomic_against_a_stale_read(make_client, monkeypatch):
    client = make_client(InMemoryStubFactory("usage-multi-request"))
    cid = budgeted(client, total=50000, per_call=40000)
    turn_and_run(client, cid, "a", text=PROMPT)
    # A decision made on stale numbers says yes; the conditional UPDATE must still say no.
    monkeypatch.setattr(budget_module, "decide",
                        lambda *a, **k: BudgetDecision(True, "admitted", reserve=40000))
    refused(client, cid, "b", "token_budget_exceeded")


def test_two_concurrent_calls_cannot_both_take_the_last_budget(make_client):
    client = make_client(InMemoryStubFactory("happy"))
    cid = budgeted(client, total=50000, per_call=40000)
    turns = [submit(client, cid, key=k, text=PROMPT) for k in ("a", "b")]
    for t in turns:
        approve(client, t)
    codes, barrier = [], threading.Barrier(2)

    def go(t):
        barrier.wait()
        codes.append(run(client, t).status_code)

    threads = [threading.Thread(target=go, args=(t,)) for t in turns]
    for th in threads:
        th.start()
    for th in threads:
        th.join(30)
    assert sorted(codes) == [200, 409]
    assert adapter(client).start_count == 1


def test_an_agent_without_usage_reporting(make_client):
    client = make_client(InMemoryStubFactory("happy"))
    adapter(client).usage_report = None  # as the Mock: no usage channel
    cid = budgeted(client, total=10000, per_call=1000)
    refused(client, cid, "a", "budget_unmeterable")
    set_budget(client, root_of(client, cid), total=10000, mode="soft")
    turn, result = turn_and_run(client, cid, "b", text=PROMPT)
    assert result["status"] == "succeeded"
    rec = item(client, turn["invocation_id"])
    assert "unmeterable" in rec["warnings"] and "reservation_is_estimate" in rec["warnings"]
    # Its spend is unknown, so the estimate it reserved stays charged.
    assert rec["state"] == "unsettled" and rec["unsettled"] == rec["reservation"] == 3


def test_strict_needs_a_per_call_reservation(make_client):
    client = make_client(InMemoryStubFactory("happy"))
    cid = budgeted(client, total=10000)
    refused(client, cid, "a", "budget_needs_per_call")


def test_a_known_exhausted_budget_refuses_even_in_soft_mode(make_client):
    client = make_client(InMemoryStubFactory("usage-multi-request"))
    cid = budgeted(client, total=30000, mode="soft")
    turn, result = turn_and_run(client, cid, "a", text=PROMPT)
    assert result["status"] == "succeeded"
    assert "projected_over_budget" not in item(client, turn["invocation_id"])["warnings"]
    refused(client, cid, "b", "token_budget_exhausted")  # 30,618 >= 30,000


def test_spend_before_the_budget_was_set_counts(make_client):
    client = make_client(InMemoryStubFactory("usage-multi-request"))
    cid = conversation(client)
    turn_and_run(client, cid, "a", text=PROMPT)  # no budget yet
    set_budget(client, root_of(client, cid), total=40000, per_call=20000)
    assert spend(client, cid) == (S3_TOKENS, 0, 0, 0)
    refused(client, cid, "b", "token_budget_exceeded")
    assert counters(client, cid) is None  # built lazily; this refusal rolled the build back too


def test_an_unknown_call_keeps_its_reservation(make_client):
    client = make_client(InMemoryStubFactory("slow-terminal"), timeout_seconds=0.5)
    cid = budgeted(client, total=100000, per_call=1000)
    turn, result = turn_and_run(client, cid, "a", text=PROMPT)
    assert result["status"] == "unknown"
    assert item(client, turn["invocation_id"])["state"] == "unsettled"
    assert counters(client, cid) == (0, 1000, 0, 0)  # charged, not returned, not zero


def test_unknown_spend_without_a_reservation_blocks_strict_until_soft(make_client):
    client = make_client(InMemoryStubFactory("slow-terminal"), timeout_seconds=0.5)
    cid = conversation(client)
    turn, result = turn_and_run(client, cid, "a", text=PROMPT)  # no budget: nothing reserved
    assert result["status"] == "unknown"
    set_budget(client, root_of(client, cid), total=100000, per_call=1000)
    assert spend(client, cid) == (0, 0, 0, 1)
    refused(client, cid, "b", "budget_unknown_unbounded")
    set_budget(client, root_of(client, cid), total=100000, per_call=1000, mode="soft")
    turn, _ = turn_and_run(client, cid, "c", text=PROMPT)
    assert "unknown_unbounded" in item(client, turn["invocation_id"])["warnings"]
    assert counters(client, cid)[3] == 1  # the first call is still unbounded, now on record


def test_a_reservation_survives_a_restart_as_unsettled(make_client):
    client = make_client(InMemoryStubFactory("happy"))
    cid = budgeted(client, total=10000, per_call=700)
    turn = submit(client, cid, key="a", text=PROMPT)
    approve(client, turn)
    with client.app.state.db.session() as s:  # consumed, then the process "dies" before launching
        PolicyService(s).consume(turn["invocation_id"], metered=True)
    assert counters(client, cid) == (0, 0, 700, 0)
    summary = client.post("/api/worker/recover", headers=USER).json()
    assert turn["invocation_id"] in summary["unknown"]
    assert counters(client, cid) == (0, 700, 0, 0)  # still charged after the restart
    assert item(client, turn["invocation_id"])["state"] == "unsettled"


def test_the_card_shows_estimate_used_reserved_and_unknown(make_client):
    client = make_client(InMemoryStubFactory("usage-multi-request"))
    cid = budgeted(client, total=50000, per_call=40000, warn=50)
    turn_and_run(client, cid, "a", text=PROMPT)
    pending = submit(client, cid, key="b", text="x" * 400)
    msgs = client.get(f"/api/conversations/{cid}/messages", headers=USER).json()["messages"]
    card = next(m["invocation"]["budget"] for m in msgs
                if m["invocation"] and m["invocation"]["invocation_id"] == pending["invocation_id"])
    assert card["active"] and card["mode"] == "strict" and card["provider_context"] == "unknown"
    assert card["estimate_visible"] == 100 and card["known"] == S3_TOKENS and card["reserved"] == 0
    assert card["remaining"] == 50000 - S3_TOKENS
    assert card["preview"]["allowed"] is False and card["preview"]["code"] == "token_budget_exceeded"
    page = client.get(f"/api/conversations/{cid}/usage", headers=USER).json()["budget"]
    assert page["total"] == 50000 and page["policy"]["token_budget_per_call"] == 40000


def test_without_a_budget_nothing_changes(make_client):
    client = make_client(InMemoryStubFactory("happy"))
    cid = conversation(client)
    turn, result = turn_and_run(client, cid, "a", text=PROMPT)
    assert result["status"] == "succeeded"
    assert item(client, turn["invocation_id"]) is None and counters(client, cid) is None


@pytest.mark.parametrize("body", [
    {"token_budget_total": 100, "token_budget_per_call": 101},
    {"token_budget_mode": "hard"},
    {"token_budget_total": "100"},
    {"token_budget_total": 0},
])
def test_invalid_budgets_are_refused_without_a_new_version(make_client, body):
    client = make_client(InMemoryStubFactory("happy"))
    cid = conversation(client)
    submit(client, cid, key="opener", text=PROMPT)
    r = client.patch(f"/api/tasks/{root_of(client, cid)}/policy", headers=USER, json=body)
    assert r.status_code == 422


def test_describe_says_who_reports_usage(make_client):
    client = make_client(InMemoryStubFactory("happy"))
    assert describe(adapter(client))["usage_reporting"] is True
