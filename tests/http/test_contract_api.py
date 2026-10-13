"""HTTP contract alignment with AGENT_HUB_PLAN §5.

Covers the endpoints the plan names that P2 previously did not expose, or
exposed with the wrong method:

* ``POST /api/conversations``                (was missing)
* ``PATCH /api/tasks/{id}/policy``           (was POST-only)
* ``POST /api/tasks/{id}/pause``             (was missing)
* ``POST /api/invocations/{id}/reconcile``   (new: user-only unknown resolution)
* ``POST /api/worker/reap``                  (run-time limit enforcement)

Authorization is re-checked here because these are new privileged endpoints.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from agenthub.contracts import ApprovalStatus, InvocationStatus
from agenthub_kernel.db import ApprovalRow, Database, InvocationRow
from agenthub_kernel.service import PolicyService
from agenthub_rt.api import create_app

USER = {"Authorization": "Bearer dev-user-token"}
AGENT = {"Authorization": "Bearer dev-agent-token"}


@pytest.fixture
def client(tmp_path):
    url = f"sqlite:///{(tmp_path / 'contract.db').as_posix()}"
    app = create_app(db=Database(url))
    with TestClient(app) as c:
        yield c


def _invocation(client, goal="x", parent_task_id=None):
    payload = {"task_id": "ignored", "agent_id": "mock", "task_input": goal}
    if parent_task_id is not None:
        payload["parent_task_id"] = parent_task_id
    r = client.post("/api/invocations", json=payload)
    assert r.status_code == 200, r.text
    return r.json()["task_id"], r.json()["invocation_id"]


def _pending_approval(client, inv_id) -> str:
    with client.app.state.db.session() as s:
        appr = PolicyService(s).create_approval(invocation_id=inv_id)
        return appr.id


# --------------------------------------------------------------------------- #
# conversations
# --------------------------------------------------------------------------- #


def test_post_conversations_returns_id(client):
    r = client.post("/api/conversations")
    assert r.status_code == 200, r.text
    assert r.json()["conversation_id"].startswith("conv-")


def test_message_still_works(client):
    r = client.post("/api/conversations/c1/messages", json={"text": "hi"})
    assert r.status_code == 200
    assert r.json()["acked"] is True


# --------------------------------------------------------------------------- #
# PATCH policy
# --------------------------------------------------------------------------- #


def test_patch_policy_is_supported(client):
    task_id, _inv = _invocation(client)
    r = client.patch(
        f"/api/tasks/{task_id}/policy",
        json={"max_subagents_total": 4, "max_subagents_concurrent": 2},
        headers=USER,
    )
    assert r.status_code == 200, r.text
    assert r.json()["version"] == 2


def test_patch_policy_requires_user(client):
    task_id, _inv = _invocation(client)
    r = client.patch(
        f"/api/tasks/{task_id}/policy",
        json={"max_subagents_total": 4, "max_subagents_concurrent": 2},
        headers=AGENT,
    )
    assert r.status_code == 403


# --------------------------------------------------------------------------- #
# pause / resume
# --------------------------------------------------------------------------- #


def _running(client, goal="run me"):
    task_id, inv_id = _invocation(client, goal)
    appr = _pending_approval(client, inv_id)
    client.post(f"/api/approvals/{appr}/approve", headers=USER)
    client.post("/api/worker/drain")
    return task_id, inv_id


def test_pause_blocks_new_start_and_resume_unblocks(client):
    task_id, first_inv = _running(client, "first")
    assert client.app.state.worker.adapter.start_count == 1

    r = client.post(f"/api/tasks/{task_id}/pause", headers=USER)
    assert r.status_code == 200, r.text
    assert r.json()["paused"] is True

    # A second approved call IN THE SAME TREE must not start while paused.
    _t2, inv2 = _invocation(client, "second", parent_task_id=task_id)
    appr2 = _pending_approval(client, inv2)
    client.post(f"/api/approvals/{appr2}/approve", headers=USER)
    client.post("/api/worker/drain")
    assert client.app.state.worker.adapter.start_count == 1

    # The already-running call was NOT cancelled by the pause.
    with client.app.state.db.session() as s:
        assert s.get(InvocationRow, first_inv).status == InvocationStatus.RUNNING.value

    r = client.post(f"/api/tasks/{task_id}/resume", headers=USER)
    assert r.status_code == 200
    assert r.json()["paused"] is False
    client.post("/api/worker/drain")
    assert client.app.state.worker.adapter.start_count == 2


def test_pause_and_resume_require_user(client):
    task_id, _inv = _invocation(client)
    assert client.post(f"/api/tasks/{task_id}/pause", headers=AGENT).status_code == 403
    assert client.post(f"/api/tasks/{task_id}/resume", headers=AGENT).status_code == 403


# --------------------------------------------------------------------------- #
# reconcile (user-only unknown resolution)
# --------------------------------------------------------------------------- #


def _unknown(client, goal="unknown"):
    task_id, inv_id = _invocation(client, goal)
    appr = _pending_approval(client, inv_id)
    client.post(f"/api/approvals/{appr}/approve", headers=USER)
    # Crash window A: consume directly, never call the adapter.
    with client.app.state.db.session() as s:
        PolicyService(s).consume(inv_id)
        s.commit()
    client.post("/api/worker/recover", headers=USER)
    with client.app.state.db.session() as s:
        assert s.get(InvocationRow, inv_id).status == InvocationStatus.UNKNOWN.value
    return task_id, inv_id


def test_reconcile_settle_via_api(client):
    _task, inv_id = _unknown(client)

    r = client.post(
        f"/api/invocations/{inv_id}/reconcile",
        json={"action": "settle", "outcome": "canceled"},
        headers=USER,
    )
    assert r.status_code == 200, r.text
    assert r.json()["action"] == "settle"
    assert r.json()["outcome"] == "canceled"
    with client.app.state.db.session() as s:
        assert s.get(InvocationRow, inv_id).status == InvocationStatus.CANCELED.value


def test_reconcile_retry_requires_new_approval_via_api(client):
    _task, inv_id = _unknown(client)

    r = client.post(
        f"/api/invocations/{inv_id}/reconcile",
        json={"action": "retry"},
        headers=USER,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["requires_approval"] is True
    new_inv = body["retry_invocation_id"]
    new_appr = body["retry_approval_id"]

    with client.app.state.db.session() as s:
        assert s.get(InvocationRow, new_inv).status == InvocationStatus.AWAITING_APPROVAL.value
        assert s.get(ApprovalRow, new_appr).status == ApprovalStatus.PENDING.value

    # Nothing starts until the user approves the retry.
    client.post("/api/worker/drain")
    assert client.app.state.worker.adapter.start_count == 0
    client.post(f"/api/approvals/{new_appr}/approve", headers=USER)
    client.post("/api/worker/drain")
    assert client.app.state.worker.adapter.start_count == 1


def test_reconcile_requires_user_and_valid_action(client):
    _task, inv_id = _unknown(client)

    assert (
        client.post(
            f"/api/invocations/{inv_id}/reconcile",
            json={"action": "settle", "outcome": "canceled"},
            headers=AGENT,
        ).status_code
        == 403
    )

    r = client.post(
        f"/api/invocations/{inv_id}/reconcile",
        json={"action": "explode"},
        headers=USER,
    )
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "bad_action"


def test_reconcile_refuses_non_unknown(client):
    _task, inv_id = _running(client, "not unknown")

    r = client.post(
        f"/api/invocations/{inv_id}/reconcile",
        json={"action": "settle", "outcome": "failed"},
        headers=USER,
    )
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "not_unknown"


def test_reconcile_retry_refused_while_execution_unconfirmed(client):
    """retry must be refused (409 not_stopped) until the old execution is over."""
    task_id, inv_id = _running(client, "unconfirmed")

    # Time it out with an external record that still says running.
    import datetime as dt

    class StuckAdapter:
        def cancel_invocation(self, invocation_id):
            return {"ok": False, "reason": "still running"}

        def lookup_by_invocation(self, invocation_id):
            return {"status": "running", "adapter_invocation_id": "a1"}

    client.app.state.worker.adapter = StuckAdapter()
    with client.app.state.db.session() as s:
        row = s.get(InvocationRow, inv_id)
        row.started_at = dt.datetime.now(dt.timezone.utc).replace(
            tzinfo=None
        ) - dt.timedelta(seconds=5000)
        s.commit()

    reap = client.post("/api/worker/reap", headers=USER)
    assert reap.status_code == 200
    assert reap.json()["timed_out"][0]["outcome"] == "unknown"
    assert reap.json()["timed_out"][0]["slot_released"] is False

    r = client.post(
        f"/api/invocations/{inv_id}/reconcile",
        json={"action": "retry"},
        headers=USER,
    )
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["code"] == "not_stopped"

    # The old call is untouched and still holds its slot.
    with client.app.state.db.session() as s:
        assert s.get(InvocationRow, inv_id).status == InvocationStatus.UNKNOWN.value


def test_settle_via_api_frees_slot(client):
    """A user settle is an explicit confirmation and may release the slot."""
    task_id, inv_id = _running(client, "to settle")

    class StuckAdapter:
        def cancel_invocation(self, invocation_id):
            return {"ok": False, "reason": "still running"}

        def lookup_by_invocation(self, invocation_id):
            return {"status": "running", "adapter_invocation_id": "a1"}

    client.app.state.worker.adapter = StuckAdapter()
    import datetime as dt

    with client.app.state.db.session() as s:
        row = s.get(InvocationRow, inv_id)
        row.started_at = dt.datetime.now(dt.timezone.utc).replace(
            tzinfo=None
        ) - dt.timedelta(seconds=5000)
        s.commit()
    client.post("/api/worker/reap", headers=USER)

    r = client.post(
        f"/api/invocations/{inv_id}/reconcile",
        json={"action": "settle", "outcome": "timed_out"},
        headers=USER,
    )
    assert r.status_code == 200, r.text
    with client.app.state.db.session() as s:
        assert s.get(InvocationRow, inv_id).status == InvocationStatus.TIMED_OUT.value


# --------------------------------------------------------------------------- #
# reap (run-time limit)
# --------------------------------------------------------------------------- #


def test_reap_endpoint_times_out_overdue(client):
    _task, inv_id = _running(client, "long runner")

    # Backdate the start so it exceeds the default 600s limit.
    import datetime as dt

    with client.app.state.db.session() as s:
        row = s.get(InvocationRow, inv_id)
        row.started_at = dt.datetime.now(dt.timezone.utc).replace(
            tzinfo=None
        ) - dt.timedelta(seconds=5000)
        s.commit()

    r = client.post("/api/worker/reap", headers=USER)
    assert r.status_code == 200, r.text
    assert r.json()["examined"] == 1
    with client.app.state.db.session() as s:
        assert s.get(InvocationRow, inv_id).status == InvocationStatus.TIMED_OUT.value


def test_reap_requires_user(client):
    assert client.post("/api/worker/reap", headers=AGENT).status_code == 403
