"""Continue an existing plan after a pre-launch refusal, without replaying work."""
import pytest
from sqlalchemy import select

from agenthub_kernel.db import ApprovalRow, InvocationRow, StageGrantRow
from tests.http.test_autopilot import (
    USER, make_hub, conv, prove, save_plan, start, stage_of, run_stage,
)

PLAN = [{"agent_id": "codex", "goal": "Write one sentence"},
        {"agent_id": "claude-code", "goal": "Translate the sentence", "depends_on": [1]}]


def view(client, cid):
    return client.get(f"/api/conversations/{cid}/plans", headers=USER).json()["plans"][0]


def test_raise_limit_and_start_current_plan_skips_done_step_and_replaces_revoked_draft(make_hub):
    client, _ = make_hub()
    cid = conv(client)
    prove(client, cid)
    save_plan(client, cid, PLAN)
    root = client.get(f"/api/conversations/{cid}/messages", headers=USER).json()["root_task_id"]
    response = client.patch(f"/api/tasks/{root}/policy", headers=USER,
                            json={"collaboration_enabled": True, "max_agent_invocations": 3})
    assert response.status_code == 200
    assert start(client, cid, plan="current", collect_agent="codex").status_code == 200
    assert run_stage(client, cid) == "blocked"
    first = view(client, cid)
    assert first["items"][0]["state"] == "done"
    assert first["items"][1]["state"] == "failed" and first["items"][1]["ready"]
    old_id = first["items"][1]["invocation_id"]
    old_reply = first["items"][0]["reply_message_id"]
    with client.app.state.db.session() as session:
        old = session.get(InvocationRow, old_id)
        old_approval = session.scalar(select(ApprovalRow).where(ApprovalRow.invocation_id == old_id))
        assert old.status == "draft" and old.started_at is None and not old.adapter_invocation_id
        assert old_approval.status == "revoked"
        approval_id = old_approval.id
    assert client.patch(f"/api/tasks/{root}/policy", headers=USER,
                        json={"collaboration_enabled": True, "max_agent_invocations": 20}).status_code == 200
    assert start(client, cid, plan="current", collect_agent="codex").status_code == 200
    assert run_stage(client, cid) == "completed"
    current = stage_of(client, cid)
    final = view(client, cid)
    assert final["all_done"]
    assert final["items"][0]["invocation_id"] == first["items"][0]["invocation_id"]
    assert final["items"][1]["invocation_id"] != old_id
    with client.app.state.db.session() as session:
        assert session.get(ApprovalRow, approval_id).status == "revoked"
        assert session.get(InvocationRow, old_id).status == "draft"
        grants = session.scalars(select(StageGrantRow).where(StageGrantRow.stage_id == current["stage_id"])).all()
        assert {g.step for g in grants} == {"item:2", "collect"}
        collect = next(g for g in grants if g.step == "collect")
        handoff = session.get(InvocationRow, collect.invocation_id).snapshot["handoff"]
        assert len(handoff) == 2 and any(h["message_id"] == old_reply for h in handoff)
    denied = client.post(f"/api/invocations/{old_id}/run", headers=USER, params={"wait": "true"})
    assert denied.status_code == 409


@pytest.mark.parametrize("closed", ["revoked", "expired", "rejected"])
def test_never_started_closed_approval_is_retryable_but_not_reused(make_hub, closed):
    client, _ = make_hub()
    cid = conv(client)
    prove(client, cid)
    save_plan(client, cid, PLAN[:1])
    pid = view(client, cid)["plan_id"]
    turn = client.post(f"/api/conversations/{cid}/plans/{pid}/items/1/dispatch", headers=USER,
                       json={"client_request_id": "old"}).json()
    with client.app.state.db.session() as session:
        session.get(InvocationRow, turn["invocation_id"]).status = "draft"
        session.get(ApprovalRow, turn["approval_id"]).status = closed
    assert view(client, cid)["items"][0]["state"] == "failed"
    assert start(client, cid, plan="current", collect_agent="codex").status_code == 200
    assert run_stage(client, cid) == "completed"
    assert view(client, cid)["items"][0]["invocation_id"] != turn["invocation_id"]
    with client.app.state.db.session() as session:
        assert session.get(ApprovalRow, turn["approval_id"]).status == closed


@pytest.mark.parametrize("status,evidence", [
    ("awaiting_approval", False), ("draft", True), ("starting", True), ("running", True), ("unknown", True),
])
def test_live_approval_or_launch_evidence_still_prevents_duplicate_dispatch(make_hub, status, evidence):
    client, _ = make_hub()
    cid = conv(client)
    prove(client, cid)
    save_plan(client, cid, PLAN[:1])
    pid = view(client, cid)["plan_id"]
    turn = client.post(f"/api/conversations/{cid}/plans/{pid}/items/1/dispatch", headers=USER,
                       json={"client_request_id": "old"}).json()
    with client.app.state.db.session() as session:
        inv = session.get(InvocationRow, turn["invocation_id"])
        inv.status = status
        if evidence:
            inv.adapter_invocation_id = "external-evidence"
            session.get(ApprovalRow, turn["approval_id"]).status = "revoked"
    item = view(client, cid)["items"][0]
    assert not item["ready"]
    response = client.post(f"/api/conversations/{cid}/plans/{pid}/items/1/dispatch", headers=USER,
                           json={"client_request_id": "duplicate"})
    assert response.status_code == 409
