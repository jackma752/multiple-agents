"""HTTP policy endpoint: persistence, versioning and invalidation via the API.

Complements tests/http/test_auth.py (which covers the negative/authorization
cases) with the positive path: a logged-in user updates the policy and the
change is persisted, versioned and revokes outstanding approvals.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from agenthub.contracts import ApprovalStatus
from agenthub_kernel.db import ApprovalRow, Database, TaskRow
from agenthub_kernel.service import PolicyService
from agenthub_rt.api import create_app

USER = {"Authorization": "Bearer dev-user-token"}
AGENT = {"Authorization": "Bearer dev-agent-token"}


@pytest.fixture
def client(tmp_path):
    url = f"sqlite:///{(tmp_path / 'api_policy.db').as_posix()}"
    app = create_app(db=Database(url))
    with TestClient(app) as c:
        yield c


def _root_with_approval(client):
    """Create an invocation (which now raises its own pending approval) + approve it."""
    r = client.post(
        "/api/invocations",
        json={"task_id": "ignored", "agent_id": "mock", "task_input": "x"},
    )
    task_id = r.json()["task_id"]
    inv_id = r.json()["invocation_id"]
    appr_id = r.json()["approval_id"]
    db = client.app.state.db
    with db.session() as s:
        PolicyService(s).approve(appr_id)
    return task_id, inv_id, appr_id


def test_user_policy_update_persists_and_versions(client):
    task_id, _inv_id, appr_id = _root_with_approval(client)

    r = client.post(
        f"/api/tasks/{task_id}/policy",
        json={"max_subagents_total": 5, "max_subagents_concurrent": 3, "max_depth": 2},
        headers=USER,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["version"] == 2
    assert body["policy"]["max_subagents_total"] == 5
    assert body["revoked_approvals"] == 1

    db = client.app.state.db
    with db.session() as s:
        root = s.get(TaskRow, body["root_task_id"])
        assert root.policy_version == 2
        assert root.policy["max_subagents_total"] == 5
        # The outstanding approval was voided by the policy change.
        assert s.get(ApprovalRow, appr_id).status == ApprovalStatus.REVOKED.value


def test_illegal_policy_via_api_writes_nothing(client):
    task_id, _inv_id, appr_id = _root_with_approval(client)

    r = client.post(
        f"/api/tasks/{task_id}/policy",
        json={"max_subagents_total": 1, "max_subagents_concurrent": 9},  # concurrent > total
        headers=USER,
    )
    assert r.status_code == 422, r.text
    assert r.json()["detail"]["code"] == "invalid_policy"

    db = client.app.state.db
    with db.session() as s:
        root = s.get(TaskRow, task_id)
        assert root.policy_version == 1
        # The live approval survived the rejected update.
        assert s.get(ApprovalRow, appr_id).status == ApprovalStatus.APPROVED.value


def test_agent_cannot_update_policy_via_api(client):
    task_id, _inv_id, appr_id = _root_with_approval(client)

    r = client.post(
        f"/api/tasks/{task_id}/policy",
        json={"max_subagents_total": 99, "max_subagents_concurrent": 99},
        headers=AGENT,
    )
    assert r.status_code == 403, r.text

    db = client.app.state.db
    with db.session() as s:
        assert s.get(TaskRow, task_id).policy_version == 1
        assert s.get(ApprovalRow, appr_id).status == ApprovalStatus.APPROVED.value
