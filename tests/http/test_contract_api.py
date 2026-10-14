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


def test_post_invocations_raises_a_pending_approval(client):
    """Plan §5: POST /api/invocations creates an invocation AWAITING approval.

    Without this, a call created through the API could never be approved.
    """
    task_id, inv_id = _invocation(client, "needs approval")

    with client.app.state.db.session() as s:
        row = s.get(InvocationRow, inv_id)
        assert row.status == InvocationStatus.AWAITING_APPROVAL.value

    # The response must expose the approval id so the user can act on it.
    r = client.post(
        "/api/invocations",
        json={"task_id": "-", "agent_id": "mock", "task_input": "again"},
    )
    assert r.status_code == 200
    assert r.json()["approval_id"]
    assert r.json()["status"] == "awaiting_approval"

    # And that approval is genuinely usable: approve -> queued -> runs.
    appr = r.json()["approval_id"]
    assert client.post(f"/api/approvals/{appr}/approve", headers=USER).status_code == 200
    client.post("/api/worker/drain", headers=USER)
    assert client.app.state.worker.adapter.start_count == 1


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
    client.post("/api/worker/drain", headers=USER)
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
    client.post("/api/worker/drain", headers=USER)
    assert client.app.state.worker.adapter.start_count == 1

    # The already-running call was NOT cancelled by the pause.
    with client.app.state.db.session() as s:
        assert s.get(InvocationRow, first_inv).status == InvocationStatus.RUNNING.value

    r = client.post(f"/api/tasks/{task_id}/resume", headers=USER)
    assert r.status_code == 200
    assert r.json()["paused"] is False
    client.post("/api/worker/drain", headers=USER)
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
    client.post("/api/worker/drain", headers=USER)
    assert client.app.state.worker.adapter.start_count == 0
    client.post(f"/api/approvals/{new_appr}/approve", headers=USER)
    client.post("/api/worker/drain", headers=USER)
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


def test_drain_requires_user(client):
    """drain() can start approved calls, so it is a privileged endpoint too."""
    assert client.post("/api/worker/drain").status_code == 401
    assert client.post("/api/worker/drain", headers=AGENT).status_code == 403
    assert client.post("/api/worker/drain", headers=USER).status_code == 200


def test_mock_activity_requires_user(client):
    """The Mock introspection endpoint is user-only."""
    assert client.get("/api/dev/mock-activity").status_code == 401
    r = client.get("/api/dev/mock-activity", headers=USER)
    assert r.status_code == 200
    assert r.json()["mock_only"] is True


def test_shutdown_requires_user_and_reports_missing_hook(client):
    """Dev shutdown is user-only and inert without serve.py's hook.

    Unit tests build the app via create_app(), which installs no shutdown hook,
    so the endpoint must say so rather than pretend it stopped something.
    """
    assert client.post("/api/dev/shutdown").status_code == 401
    assert client.post("/api/dev/shutdown", headers=AGENT).status_code == 403

    r = client.post("/api/dev/shutdown", headers=USER)
    assert r.status_code == 200, r.text
    assert r.json()["stopping"] is False
    assert "no shutdown hook" in r.json()["reason"]


def test_shutdown_invokes_the_hook_when_installed(client):
    """With a hook installed (as tools/demo/serve.py does), it is called."""
    calls: list[int] = []
    client.app.state.request_shutdown = lambda: calls.append(1)

    r = client.post("/api/dev/shutdown", headers=USER)
    assert r.status_code == 200
    assert r.json()["stopping"] is True
    assert calls == [1]


# --------------------------------------------------------------------------- #
# adapter-result: launch evidence, and which agent_id the API will serve
# --------------------------------------------------------------------------- #


def _codex_app(tmp_path, name: str = "codex.db", adapter_name: str = "codex-exec.db"):
    """An app whose adapter is the real Codex adapter driven by a fake runner."""
    from agenthub_adapters.codex import CodexAdapter, FakeCodexRunner, success_script
    from agenthub_adapters.store import ExecutionStore

    adapter = CodexAdapter(
        codex_path=__file__,  # any existing file; identity is not pinned here
        codex_home=str(tmp_path / "home"),
        workdir_root=str(tmp_path / "runs"),
        store=ExecutionStore(path=str(tmp_path / adapter_name), table="codex_executions"),
        runner=FakeCodexRunner(success_script()),
    )
    app = create_app(db=Database(f"sqlite:///{(tmp_path / name).as_posix()}"), adapter=adapter)
    return adapter, app


def test_adapter_result_reports_launch_evidence(tmp_path):
    """A launch ATTEMPT and a process that really RAN must be distinguishable.

    The verification script (`tools/demo/codex_verify.py`) counts launches from
    this endpoint, so "an execution record exists" alone is not enough: it also
    needs the fields that prove a process produced output.
    """
    adapter, app = _codex_app(tmp_path)
    with TestClient(app) as c:
        created = c.post(
            "/api/invocations",
            json={"task_id": "-", "agent_id": "codex", "task_input": "say OK"},
            headers=USER,
        ).json()
        inv_id, appr_id = created["invocation_id"], created["approval_id"]
        assert created["adapter_agent_ids"] == ["codex"]
        assert created["adapter_warning"] is None, created["adapter_warning"]

        # Before approval: no record at all, so nothing can be counted as a launch.
        assert c.get(f"/api/invocations/{inv_id}/adapter-result", headers=USER).json()[
            "available"
        ] is False

        c.post("/api/worker/drain", headers=USER)
        assert adapter.start_count == 0

        c.post(f"/api/approvals/{appr_id}/approve", headers=USER)
        c.post("/api/worker/drain", headers=USER)

        res = c.get(f"/api/invocations/{inv_id}/adapter-result", headers=USER).json()
        assert res["available"] is True
        assert res["launch_attempted"] is True
        assert res["status"] == "finished"
        evidence = res["evidence"]
        assert evidence["exit_code"] == 0
        assert evidence["timed_out"] is False
        assert evidence["verdict"] == "finished"
        assert evidence["event_count"] >= 4
        assert evidence["error_count"] == 0
        # A capability probe (and the console) needs the session count and the item
        # types; both are derived, non-content fields.
        assert evidence["thread_ids"] == ["01a13a34-63e6-7cc2-85d1-ddfbbec0e2ce"]
        assert evidence["item_types"] == {"agent_message": 1}


def test_api_adapter_reports_the_agent_ids_it_serves(tmp_path):
    """The console fills its agent field from here instead of hardcoding one."""
    _adapter, app = _codex_app(tmp_path)
    with TestClient(app) as c:
        info = c.get("/api/adapter", headers=USER).json()
    assert info["kind"] == "codex"
    assert info["agent_ids"] == ["codex"]


def test_mock_adapter_declares_no_agent_restriction(client):
    """The inert Mock adapter serves any agent id, so it warns about none."""
    info = client.get("/api/adapter", headers=USER).json()
    assert info["agent_ids"] == []

    created = client.post(
        "/api/invocations",
        json={"task_id": "-", "agent_id": "whatever", "task_input": "x"},
        headers=USER,
    ).json()
    assert created["adapter_agent_ids"] == []
    assert created["adapter_warning"] is None


def test_creating_a_call_for_an_unserved_agent_warns_before_the_approval(tmp_path):
    """A call the adapter cannot serve must be flagged at creation time.

    It is still created (the adapter refuses it at launch, loudly and terminally),
    but a user must not have to approve a call that can never run to find out.
    """
    _adapter, app = _codex_app(tmp_path)
    with TestClient(app) as c:
        r = c.post(
            "/api/invocations",
            json={"task_id": "-", "agent_id": "mock", "task_input": "wrong agent"},
            headers=USER,
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["status"] == "awaiting_approval"
        assert body["adapter_agent_ids"] == ["codex"]
        assert "mock" in body["adapter_warning"]
        assert "codex" in body["adapter_warning"]
