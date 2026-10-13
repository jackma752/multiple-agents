"""HTTP stop / cancel and recovery endpoints.

Verifies the wiring: the stop endpoint persists the stop AND cancels in-flight
calls; the recover endpoint reconciles and requires the user role.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from agenthub.contracts import ApprovalStatus, InvocationStatus, Policy
from agenthub_kernel.db import ApprovalRow, Database, InvocationRow, TreeQuotaRow
from agenthub_kernel.service import PolicyService
from agenthub_rt.api import create_app

USER = {"Authorization": "Bearer dev-user-token"}
AGENT = {"Authorization": "Bearer dev-agent-token"}


@pytest.fixture
def client(tmp_path):
    url = f"sqlite:///{(tmp_path / 'api_stop.db').as_posix()}"
    app = create_app(db=Database(url))
    with TestClient(app) as c:
        yield c


def _make_invocation(client):
    r = client.post(
        "/api/invocations",
        json={"task_id": "ignored", "agent_id": "mock", "task_input": "x"},
    )
    body = r.json()
    return body["task_id"], body["invocation_id"]


def _approve(client, invocation_id):
    db = client.app.state.db
    with db.session() as s:
        appr = PolicyService(s).create_approval(invocation_id=invocation_id)
        appr_id = appr.id
    with db.session() as s:
        PolicyService(s).approve(appr_id)
    return appr_id


def test_stop_endpoint_cancels_running_call(client):
    task_id, inv_id = _make_invocation(client)
    _approve(client, inv_id)

    # Run it so it is RUNNING.
    client.post("/api/worker/drain")

    db = client.app.state.db
    with db.session() as s:
        assert s.get(InvocationRow, inv_id).status == InvocationStatus.RUNNING.value

    r = client.post(f"/api/tasks/{task_id}/stop", headers=USER)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["stopped"] is True
    assert body["cancelled"] == 1
    assert body["already_stopped"] is False

    with db.session() as s:
        assert s.get(InvocationRow, inv_id).status == InvocationStatus.CANCELED.value
        root = body["root_task_id"]
        # The stop is persisted.
        from agenthub_kernel.db import EventRow
        from sqlalchemy import select

        stops = s.scalars(select(EventRow).where(EventRow.type == "tree_stop")).all()
        assert stops and stops[-1].payload["requested_by"] == "local-user"

    # Repeated stop is idempotent and starts nothing.
    r2 = client.post(f"/api/tasks/{task_id}/stop", headers=USER)
    assert r2.status_code == 200
    assert r2.json()["already_stopped"] is True
    assert r2.json()["cancelled"] == 0


def test_agent_cannot_stop_via_api(client):
    task_id, inv_id = _make_invocation(client)
    _approve(client, inv_id)
    r = client.post(f"/api/tasks/{task_id}/stop", headers=AGENT)
    assert r.status_code == 403, r.text


def test_recover_endpoint_requires_user_and_reports_unknown(client):
    task_id, inv_id = _make_invocation(client)
    _approve(client, inv_id)

    # Reach STARTING without ever calling the adapter (crash window A) by
    # consuming directly through the service.
    db = client.app.state.db
    with db.session() as s:
        PolicyService(s).consume(inv_id)
        s.commit()
    with db.session() as s:
        assert s.get(InvocationRow, inv_id).status == InvocationStatus.STARTING.value

    # Non-user identities are refused.
    r_agent = client.post("/api/worker/recover", headers=AGENT)
    assert r_agent.status_code == 403

    r = client.post("/api/worker/recover", headers=USER)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["unknown"] == [inv_id]
    assert body["restarted"] == []

    with db.session() as s:
        assert s.get(InvocationRow, inv_id).status == InvocationStatus.UNKNOWN.value
