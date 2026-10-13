"""The P2 demo flow required by AGENT_HUB_PLAN §10.

"第一个演示应做到：用户聊天 → 手动分派两个 Mock 子 Agent → 未同意不启动 →
同意后执行 → 达到上限拒绝新增 → 停止按钮生效。"

This drives the real HTTP surface (the API the UI will call) against a
file-backed database with a Mock adapter, and asserts the whole chain.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from agenthub.contracts import InvocationStatus
from agenthub_kernel.db import Database, InvocationRow, TreeQuotaRow
from agenthub_kernel.service import PolicyService
from agenthub_rt.api import create_app

USER = {"Authorization": "Bearer dev-user-token"}


@pytest.fixture
def client(tmp_path):
    url = f"sqlite:///{(tmp_path / 'demo.db').as_posix()}"
    app = create_app(db=Database(url))
    with TestClient(app) as c:
        yield c


def _starts(client) -> int:
    return client.app.state.worker.adapter.start_count


def _create_root_invocation(client, goal="build a login page") -> tuple[str, str]:
    r = client.post(
        "/api/invocations",
        json={"task_id": "ignored", "agent_id": "mock", "task_input": goal},
    )
    assert r.status_code == 200, r.text
    return r.json()["task_id"], r.json()["invocation_id"]


def _approve(client, invocation_id) -> str:
    db = client.app.state.db
    with db.session() as s:
        appr = PolicyService(s).create_approval(invocation_id=invocation_id)
        appr_id = appr.id
    r = client.post(f"/api/approvals/{appr_id}/approve", headers=USER)
    assert r.status_code == 200, r.text
    return appr_id


def _propose_delegation(client, parent_task_id, goal) -> dict:
    r = client.post(
        f"/api/tasks/{parent_task_id}/delegations",
        json={"agent_id": "mock", "goal": goal, "parent_task_id": parent_task_id},
    )
    assert r.status_code == 200, r.text
    return r.json()


def test_p2_demo_flow(client):
    """User chat -> dispatch two sub-agents -> approve -> hit the cap -> stop."""
    # ---- 1. user chat -------------------------------------------------
    r = client.post("/api/conversations/c1/messages", json={"text": "build a login page"})
    assert r.status_code == 200 and r.json()["acked"] is True

    # ---- 2. main agent call, approved by the user ---------------------
    root_task_id, main_inv = _create_root_invocation(client)
    _approve(client, main_inv)
    client.post("/api/worker/drain")
    assert _starts(client) == 1, "approved main call should have started"

    # ---- 3. manually dispatch two Mock sub-agents ---------------------
    d1 = _propose_delegation(client, root_task_id, "write the HTML")
    d2 = _propose_delegation(client, root_task_id, "write the CSS")
    assert d1["status"] == "awaiting_approval"
    assert d2["status"] == "awaiting_approval"

    # ---- 4. NOT approved yet -> nothing starts ------------------------
    client.post("/api/worker/drain")
    assert _starts(client) == 1, "unapproved sub-agents must not start"

    # ---- 5. user approves both -> they execute ------------------------
    for d in (d1, d2):
        r = client.post(f"/api/approvals/{d['approval_id']}/approve", headers=USER)
        assert r.status_code == 200, r.text
    client.post("/api/worker/drain")
    assert _starts(client) == 3, "both approved sub-agents should have started"

    # ---- 6. cap reached (total 2 sub-agents) -> new one is refused ----
    d3 = _propose_delegation(client, root_task_id, "write the JS")
    client.post(f"/api/approvals/{d3['approval_id']}/approve", headers=USER)
    client.post("/api/worker/drain")
    assert _starts(client) == 3, "the total sub-agent cap should have blocked the 3rd"

    with client.app.state.db.session() as s:
        q = s.get(TreeQuotaRow, root_task_id)
        assert q is not None
        # concurrent counts sub-agents only (the main agent is depth 0)
        assert q.launched_total == 2, "exactly two sub-agents should have been launched"
        assert q.concurrent == 2

    # ---- 7. stop button: cancels running, blocks new scheduling -------
    r = client.post(f"/api/tasks/{root_task_id}/stop", headers=USER)
    assert r.status_code == 200, r.text
    assert r.json()["stopped"] is True
    # Stops the WHOLE tree: the main agent call and both sub-agents are running.
    assert r.json()["cancelled"] == 3, r.json()

    with client.app.state.db.session() as s:
        q = s.get(TreeQuotaRow, root_task_id)
        assert q.concurrent == 0, "stop should release the concurrent slots"
        assert q.launched_total == 2, "cumulative total must NOT be returned on stop"
        # Every invocation in the tree is terminal.
        for inv_id in (
            main_inv,
            d1["invocation_id"],
            d2["invocation_id"],
        ):
            assert s.get(InvocationRow, inv_id).status == InvocationStatus.CANCELED.value

    # Nothing new starts after a stop, even with a fresh approved approval.
    d4 = _propose_delegation(client, root_task_id, "more work")
    client.post(f"/api/approvals/{d4['approval_id']}/approve", headers=USER)
    client.post("/api/worker/drain")
    assert _starts(client) == 3, "a stopped tree must not start anything new"


def test_demo_flow_rejection_and_expiry_never_start(client):
    """Reject and expire paths also keep the adapter at zero."""
    root_task_id, main_inv = _create_root_invocation(client)

    d1 = _propose_delegation(client, root_task_id, "rejected work")
    r = client.post(f"/api/approvals/{d1['approval_id']}/reject", headers=USER)
    assert r.status_code == 200

    d2 = _propose_delegation(client, root_task_id, "expiring work")
    r = client.post(f"/api/approvals/{d2['approval_id']}/approve", headers=USER)
    assert r.status_code == 200
    # Force expiry.
    import datetime as dt

    from agenthub_kernel.db import ApprovalRow

    with client.app.state.db.session() as s:
        row = s.get(ApprovalRow, d2["approval_id"])
        row.expires_at = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None) - dt.timedelta(seconds=5)
        s.commit()

    client.post("/api/worker/drain")
    assert _starts(client) == 0
