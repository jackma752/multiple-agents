"""HTTP authorization tests: only a logged-in user may approve (plan §4.1).

Acceptance (from the P2 task list):
* unauthenticated / agent / worker / other-identity approval attempts are
  refused and the adapter start count stays 0;
* a logged-in user CAN approve, and only then does the call become runnable.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from agenthub.contracts import ApprovalStatus
from agenthub_kernel.service import PolicyService
from agenthub_rt.api import create_app

USER = {"Authorization": "Bearer dev-user-token"}
AGENT = {"Authorization": "Bearer dev-agent-token"}
WORKER = {"Authorization": "Bearer dev-worker-token"}


@pytest.fixture
def client():
    # Explicit in-memory DB so HTTP tests never touch the shared runtime file.
    from agenthub_kernel.db import Database

    app = create_app(db=Database("sqlite:///:memory:"))
    with TestClient(app) as c:
        yield c


def _make_pending_approval(client) -> tuple[str, str]:
    """Create a root task + invocation + pending approval via the API."""
    r = client.post(
        "/api/invocations",
        json={"task_id": "ignored", "agent_id": "mock", "task_input": "do a thing"},
    )
    assert r.status_code == 200, r.text
    inv_id = r.json()["invocation_id"]
    # Reuse the service directly to attach a pending approval (the delegation
    # endpoint requires a parent task; this keeps the fixture small).
    db = client.app.state.db
    with db.session() as s:
        appr = PolicyService(s).create_approval(invocation_id=inv_id)
        appr_id = appr.id
    return inv_id, appr_id


@pytest.mark.parametrize(
    "headers",
    [
        {},  # no token at all
        AGENT,
        WORKER,
        {"Authorization": "Bearer not-a-real-token"},
    ],
)
def test_non_user_cannot_approve(client, headers):
    _inv_id, appr_id = _make_pending_approval(client)

    r = client.post(f"/api/approvals/{appr_id}/approve", headers=headers)

    assert r.status_code in (401, 403), r.text

    # The approval must still be pending...
    db = client.app.state.db
    from agenthub_kernel.db import ApprovalRow

    with db.session() as s:
        row = s.get(ApprovalRow, appr_id)
        assert row.status == ApprovalStatus.PENDING.value

    # ...and the adapter must never have been started.
    assert client.app.state.worker.adapter.start_count == 0


def test_user_can_approve(client):
    _inv_id, appr_id = _make_pending_approval(client)

    r = client.post(f"/api/approvals/{appr_id}/approve", headers=USER)

    assert r.status_code == 200, r.text
    assert r.json()["status"] == ApprovalStatus.APPROVED.value
    assert r.json()["approved_by"] == "local-user"


@pytest.mark.parametrize("headers", [{}, AGENT, WORKER])
def test_non_user_cannot_change_policy(client, headers):
    # Create a root task to target.
    r = client.post(
        "/api/invocations",
        json={"task_id": "ignored", "agent_id": "mock", "task_input": "x"},
    )
    task_id = r.json()["task_id"]

    r = client.post(
        f"/api/tasks/{task_id}/policy",
        json={"max_subagents_total": 99, "max_subagents_concurrent": 99, "max_depth": 99},
        headers=headers,
    )
    assert r.status_code in (401, 403), r.text


@pytest.mark.parametrize("headers", [{}, AGENT, WORKER])
def test_non_user_cannot_stop_tree(client, headers):
    r = client.post(
        "/api/invocations",
        json={"task_id": "ignored", "agent_id": "mock", "task_input": "x"},
    )
    task_id = r.json()["task_id"]

    r = client.post(f"/api/tasks/{task_id}/stop", headers=headers)
    assert r.status_code in (401, 403), r.text
