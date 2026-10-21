"""File-database chat, approval and recovery tests; no network/model calls."""

import datetime as dt
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from agenthub.contracts import AdapterStartResult, ExecutionAdmission, InvocationSnapshot
from agenthub_adapters.mock import MockAdapter
from agenthub_kernel.db import (
    ApprovalRow, Base, ChatMessageRow, ConversationRow, Database, InvocationRow,
    QuotaReservationRow, TaskRow, TreeQuotaRow,
)
from agenthub_kernel.service import PolicyService, now
from agenthub_rt.api import create_app
from agenthub_rt.auth import Principal, authenticate

USER = {"Authorization": "Bearer dev-user-token"}
AGENT = {"Authorization": "Bearer dev-agent-token"}


class ReplyAdapter(MockAdapter):
    """Synthetic persisted replies, not a product-facing fake chat agent."""

    def __init__(self, path):
        super().__init__(store_path=str(path))
        self.outcome = "finished"
        self.unreadable = False
        self.inputs = []

    def start(self, invocation_id, snapshot, *, gate_token=None):
        assert gate_token
        self.inputs.append(snapshot.task_input)
        result = super().start(invocation_id, snapshot, gate_token=gate_token)
        self.store.set_payload(invocation_id, {
            "final_message": "  synthetic reply\n", "usage": {"output_tokens": 2},
            "thread_id": result.adapter_invocation_id,
        })
        self.store.set_status(invocation_id, self.outcome)
        return AdapterStartResult(
            adapter_invocation_id=result.adapter_invocation_id,
            ok=self.outcome == "finished", terminal=self.outcome != "unknown",
        )

    def last_result(self, invocation_id):
        if self.unreadable:
            raise OSError("synthetic store outage")
        record = self.store.get(invocation_id)
        return record["payload"] if record else None


@pytest.fixture
def client(tmp_path):
    app = create_app(
        db=Database(f"sqlite:///{(tmp_path / 'chat.db').as_posix()}"),
        adapter=ReplyAdapter(tmp_path / "adapter.db"),
    )
    with TestClient(app) as c:
        yield c
    app.state.db.engine.dispose()


def conversation(client):
    r = client.post("/api/conversations", json={"title": "test chat"}, headers=USER)
    assert r.status_code == 200, r.text
    return r.json()["conversation_id"]


def submit(client, cid, key="one", **overrides):
    r = client.post(f"/api/conversations/{cid}/invocations", headers=USER, json={
        "text": "first task", "agent_id": "mock", "client_request_id": key, **overrides,
    })
    assert r.status_code == 200, r.text
    return r.json()


def approve(client, turn):
    r = client.post(f"/api/approvals/{turn['approval_id']}/approve", headers=USER)
    assert r.status_code == 200, r.text


def run(client, turn):
    """Run and wait for the outcome (the same background path as a 202 run)."""
    return client.post(f"/api/invocations/{turn['invocation_id']}/run",
                       params={"wait": "true"}, headers=USER)


def messages(client, cid):
    r = client.get(f"/api/conversations/{cid}/messages", headers=USER)
    assert r.status_code == 200, r.text
    return r.json()["messages"]


def count(client, model):
    with client.app.state.db.session() as s:
        return s.scalar(select(func.count()).select_from(model))


@pytest.mark.parametrize("headers,status", [({}, 401), (AGENT, 403)])
def test_chat_routes_require_user(client, headers, status):
    cid = conversation(client)
    for method, path, body in [
        ("post", "/api/conversations", {}),
        ("get", "/api/conversations", None),
        ("get", f"/api/conversations/{cid}/messages", None),
        ("post", f"/api/conversations/{cid}/messages", {"text": "x"}),
        ("post", f"/api/conversations/{cid}/invocations",
         {"text": "x", "agent_id": "mock", "client_request_id": "x"}),
        ("post", "/api/invocations/no-such-call/run", None),
    ]:
        response = client.request(method, path, headers=headers, **({"json": body} if body else {}))
        assert response.status_code == status, (path, response.text)
    assert client.app.state.adapter.start_count == 0


def test_history_survives_new_app_and_does_not_create_calls(client):
    cid = conversation(client)
    original = "  test\n中文  "
    r = client.post(f"/api/conversations/{cid}/messages", headers=USER, json={"text": original})
    assert r.json()["acked"] is True
    app = create_app(db_url=client.app.state.db.url, adapter=MockAdapter())
    with TestClient(app) as restarted:
        assert messages(restarted, cid)[0]["text"] == original
        listed = restarted.get("/api/conversations", headers=USER).json()
        assert listed["conversations"][0]["conversation_id"] == cid
        assert listed["conversations"][0]["context_mode"] == "independent"
        assert listed["conversations"][0]["root_task_id"] is None
    assert count(client, InvocationRow) == 0


def test_unknown_and_other_owner_chat_not_visible(client):
    cid = conversation(client)
    turn = submit(client, cid)
    client.app.dependency_overrides[authenticate] = lambda: Principal("other-user", "user")
    try:
        assert client.get("/api/conversations", headers=USER).json()["conversations"] == []
        for target in (cid, "missing"):
            assert client.get(f"/api/conversations/{target}/messages", headers=USER).status_code == 404
            assert client.post(f"/api/conversations/{target}/messages", json={"text": "x"}, headers=USER).status_code == 404
            assert client.post(f"/api/conversations/{target}/invocations", json={
                "text": "x", "agent_id": "mock", "client_request_id": "x",
            }, headers=USER).status_code == 404
        assert run(client, turn).status_code == 404
    finally:
        client.app.dependency_overrides.clear()


@pytest.mark.parametrize("change", [
    {"text": " "}, {"text": "x" * 100001}, {"text": 123},
    {"agent_id": " "}, {"client_request_id": ""}, {"client_request_id": None},
    {"execution_mode": "unsafe"}, {"approved": True},
])
def test_invalid_turn_is_atomic(client, change):
    cid = conversation(client)
    response = client.post(f"/api/conversations/{cid}/invocations", headers=USER, json={
        "text": "x", "agent_id": "mock", "client_request_id": "x", **change,
    })
    assert response.status_code == 422
    for model in (ChatMessageRow, TaskRow, InvocationRow, ApprovalRow):
        assert count(client, model) == 0


def test_pagination_and_bounds(client):
    cid = conversation(client)
    for text in ["a", "b", "c"]:
        client.post(f"/api/conversations/{cid}/messages", json={"text": text}, headers=USER)
    first = client.get(f"/api/conversations/{cid}/messages?limit=2", headers=USER).json()
    assert [m["text"] for m in first["messages"]] == ["a", "b"]
    assert first["has_more"]
    second = client.get(f"/api/conversations/{cid}/messages?after_id={first['next_after_id']}", headers=USER).json()
    assert [m["text"] for m in second["messages"]] == ["c"]
    assert not second["has_more"]
    assert client.get(f"/api/conversations/{cid}/messages?limit=201", headers=USER).status_code == 422
    assert client.get("/api/conversations?offset=-1", headers=USER).status_code == 422


def test_run_is_targeted_and_replay_never_launches_again(client):
    cid = conversation(client)
    first = submit(client, cid)
    second = submit(client, cid, "two", text="second task")
    assert first["task_id"] == second["task_id"]
    assert run(client, first).status_code == 409
    assert client.app.state.adapter.start_count == 0
    approve(client, first)
    approve(client, second)
    result = run(client, second)
    assert result.status_code == 200 and result.json()["status"] == "succeeded"
    assert client.app.state.adapter.started_invocations == {second["invocation_id"]}
    assert run(client, second).status_code == 409
    assert client.app.state.adapter.start_count == 1
    history = messages(client, cid)
    assert [m["role"] for m in history] == ["user", "user", "assistant"]
    assert history[-1]["text"] == "  synthetic reply\n"
    assert history[-1]["result"]["usage"] == {"output_tokens": 2}
    assert history[0]["invocation"]["status"] == "queued"
    assert history[1]["invocation"]["approval_status"] == "consumed"
    assert client.app.state.adapter.inputs == ["second task"]  # No implicit history sharing.


@pytest.mark.parametrize("condition", ["pending", "rejected", "revoked", "expired", "tampered", "paused", "stopped"])
def test_run_keeps_all_existing_gates(client, condition):
    cid = conversation(client)
    turn = submit(client, cid)
    if condition not in {"pending", "rejected"}:
        approve(client, turn)
    if condition in {"rejected", "revoked"}:
        action = {"rejected": "reject", "revoked": "revoke"}[condition]
        assert client.post(f"/api/approvals/{turn['approval_id']}/{action}", headers=USER).status_code == 200
    if condition in {"paused", "stopped"}:
        action = "pause" if condition == "paused" else "stop"
        assert client.post(f"/api/tasks/{turn['task_id']}/{action}", headers=USER).status_code == 200
    with client.app.state.db.session() as s:
        if condition == "expired":
            s.get(ApprovalRow, turn["approval_id"]).expires_at = now() - dt.timedelta(seconds=1)
        if condition == "tampered":
            inv = s.get(InvocationRow, turn["invocation_id"])
            inv.snapshot = {**inv.snapshot, "task_input": "modified"}
    assert run(client, turn).status_code == 409
    assert client.app.state.adapter.start_count == 0
    assert count(client, QuotaReservationRow) == 0


def test_idempotency_survives_completion_and_conflicts_are_rejected(client):
    cid = conversation(client)
    turn = submit(client, cid)
    approve(client, turn)
    assert run(client, turn).status_code == 200
    replay = submit(client, cid)
    assert replay["invocation_id"] == turn["invocation_id"] and replay["replayed"]
    assert replay["status"] == "succeeded" and replay["approval_status"] == "consumed"
    for change in ({"text": "different"}, {"agent_id": "another"}, {"execution_mode": "supervised"}):
        r = client.post(f"/api/conversations/{cid}/invocations", headers=USER, json={
            "text": "first task", "agent_id": "mock", "client_request_id": "one", **change,
        })
        assert r.status_code == 409 and r.json()["detail"]["code"] == "idempotency_conflict"
    assert count(client, InvocationRow) == count(client, ApprovalRow) == 1
    assert client.app.state.adapter.start_count == 1


def test_concurrent_submission_and_execution_launch_once(client):
    cid = conversation(client)
    barrier = threading.Barrier(4)
    def send(_):
        barrier.wait(timeout=10)
        return submit(client, cid)
    with ThreadPoolExecutor(max_workers=4) as pool:
        turns = list(pool.map(send, range(4)))
    assert len({t["invocation_id"] for t in turns}) == 1
    assert count(client, TaskRow) == count(client, ApprovalRow) == 1
    approve(client, turns[0])
    def execute(_):
        barrier.wait(timeout=10)
        return run(client, turns[0]).status_code
    with ThreadPoolExecutor(max_workers=4) as pool:
        codes = list(pool.map(execute, range(4)))
    assert sorted(codes) == [200, 409, 409, 409]
    assert client.app.state.adapter.start_count == 1
    assert len(messages(client, cid)) == 2


def test_policy_changes_and_followups_keep_same_quota(client):
    cid = conversation(client)
    first = submit(client, cid)
    approve(client, first)
    run(client, first)
    assert client.patch(f"/api/tasks/{first['task_id']}/policy", headers=USER,
                        json={"max_agent_invocations": 2}).status_code == 200
    second = submit(client, cid, "two")
    with client.app.state.db.session() as s:
        assert s.get(InvocationRow, second["invocation_id"]).snapshot["policy_version"] == 2
        assert s.get(ApprovalRow, second["approval_id"]).policy_version == 2
    assert run(client, second).status_code == 409  # First approval cannot approve a followup.
    approve(client, second)
    assert run(client, second).status_code == 200
    third = submit(client, cid, "three")
    approve(client, third)
    assert run(client, third).status_code == 409
    assert count(client, TaskRow) == 1
    with client.app.state.db.session() as s:
        assert s.get(TreeQuotaRow, first["task_id"]).total_invocations == 2
    client.post(f"/api/tasks/{first['task_id']}/stop", headers=USER)
    assert client.post(f"/api/conversations/{cid}/invocations", headers=USER, json={
        "text": "x", "agent_id": "mock", "client_request_id": "four",
    }).status_code == 409


def test_reply_persists_after_restart_without_adapter_store(client):
    cid = conversation(client)
    turn = submit(client, cid)
    approve(client, turn)
    run(client, turn)
    restarted = create_app(db_url=client.app.state.db.url, adapter=MockAdapter())
    with TestClient(restarted) as c:
        assert messages(c, cid)[-1]["text"] == "  synthetic reply\n"
        assert run(c, turn).status_code == 409
        assert restarted.state.adapter.start_count == 0


def test_store_outage_does_not_undo_settlement_and_recovery_backfills(client, tmp_path):
    cid = conversation(client)
    turn = submit(client, cid)
    approve(client, turn)
    client.app.state.adapter.unreadable = True
    assert run(client, turn).json()["status"] == "succeeded"
    assert len(messages(client, cid)) == 1
    restarted = create_app(db_url=client.app.state.db.url, adapter=ReplyAdapter(tmp_path / "adapter.db"))
    with TestClient(restarted) as c:
        assert c.post("/api/worker/recover", headers=USER).json()["replies_restored"] == 1
        assert c.post("/api/worker/recover", headers=USER).json()["replies_restored"] == 0
        assert len(messages(c, cid)) == 2
        assert restarted.state.adapter.start_count == 0
    with client.app.state.db.session() as s:
        assert s.scalar(select(QuotaReservationRow)).released_at is not None


def test_unknown_retains_reservation_and_has_no_invented_reply(client):
    cid = conversation(client)
    turn = submit(client, cid)
    approve(client, turn)
    client.app.state.adapter.outcome = "unknown"
    assert run(client, turn).json()["status"] == "unknown"
    assert client.post("/api/worker/recover", headers=USER).json()["replies_restored"] == 0
    assert len(messages(client, cid)) == 1
    with client.app.state.db.session() as s:
        assert s.scalar(select(QuotaReservationRow)).released_at is None


def test_mock_does_not_invent_assistant_message(client):
    mock = MockAdapter()
    client.app.state.worker.adapter = mock
    client.app.state.adapter = mock
    cid = conversation(client)
    turn = submit(client, cid)
    approve(client, turn)
    assert run(client, turn).json()["status"] == "running"
    assert [m["role"] for m in messages(client, cid)] == ["user"]


def test_atomic_draft_rolls_back_on_approval_failure(client, monkeypatch):
    cid = conversation(client)
    def fail(*args, **kwargs):
        raise RuntimeError("synthetic interruption")
    monkeypatch.setattr(PolicyService, "create_approval", fail)
    with pytest.raises(RuntimeError, match="synthetic interruption"):
        submit(client, cid)
    for model in (TaskRow, InvocationRow, ApprovalRow, ChatMessageRow):
        assert count(client, model) == 0
    with client.app.state.db.session() as s:
        assert s.get(ConversationRow, cid).root_task_id is None


def test_target_is_found_past_global_queue_limit(client):
    cid = conversation(client)
    turns = [submit(client, cid, str(i)) for i in range(52)]
    for turn in turns:
        approve(client, turn)
    assert run(client, turns[-1]).status_code == 200
    assert client.app.state.adapter.started_invocations == {turns[-1]["invocation_id"]}


def test_admission_refusal_happens_before_consumption(client, monkeypatch):
    cid = conversation(client)
    turn = submit(client, cid)
    approve(client, turn)
    monkeypatch.setattr(client.app.state.adapter, "execution_admission", lambda:
        ExecutionAdmission(False, "blocked", "test_blocked", "synthetic refusal"))
    response = run(client, turn)
    assert response.status_code == 409
    assert client.app.state.adapter.start_count == 0
    with client.app.state.db.session() as s:
        assert s.get(ApprovalRow, turn["approval_id"]).status == "approved"
    assert count(client, QuotaReservationRow) == 0


def test_supervised_followup_still_requires_own_boundary_consent(client):
    cid = conversation(client)
    turn = submit(client, cid, execution_mode="supervised")
    path = f"/api/approvals/{turn['approval_id']}/approve"
    assert client.post(path, headers=USER).status_code == 409
    assert run(client, turn).status_code == 409
    assert client.app.state.adapter.start_count == 0
    assert client.post(path, headers=USER, json={"accept_unmanaged_native_agents": True}).status_code == 200
    assert run(client, turn).status_code == 200


def test_crash_after_adapter_completed_recovers_reply_without_relaunch(client, tmp_path):
    cid = conversation(client)
    turn = submit(client, cid)
    approve(client, turn)
    with client.app.state.db.session() as s:
        plan = PolicyService(s).consume(turn["invocation_id"])
    client.app.state.adapter.start(
        turn["invocation_id"], InvocationSnapshot(**plan["snapshot"]), gate_token=plan["gate_token"],
    )  # Simulated crash: no Phase 3 settlement.
    app = create_app(db_url=client.app.state.db.url, adapter=ReplyAdapter(tmp_path / "adapter.db"))
    with TestClient(app) as restarted:
        recovered = restarted.post("/api/worker/recover", headers=USER).json()
        assert recovered["settled"] == [{"invocation_id": turn["invocation_id"], "status": "succeeded"}]
        assert len(messages(restarted, cid)) == 2
        assert app.state.adapter.start_count == 0


def test_codex_adapter_capture_is_persisted_through_chat_api(tmp_path):
    from agenthub_adapters.codex import CodexAdapter, FakeCodexRunner, success_script
    from agenthub_adapters.store import ExecutionStore

    adapter = CodexAdapter(
        codex_path=__file__, codex_home=str(tmp_path / "home"),
        workdir_root=str(tmp_path / "runs"), runner=FakeCodexRunner(success_script()),
        store=ExecutionStore(path=str(tmp_path / "codex.db"), table="codex_executions"),
    )
    app = create_app(db=Database(f"sqlite:///{(tmp_path / 'hub.db').as_posix()}"), adapter=adapter)
    with TestClient(app) as client:
        cid = conversation(client)
        turn = submit(client, cid, text="Reply OK", agent_id="codex")
        assert run(client, turn).status_code == 409
        approve(client, turn)
        assert run(client, turn).json()["status"] == "succeeded"
        reply = messages(client, cid)[-1]
        assert reply["text"] == "OK"
        assert reply["result"]["thread_id"] == "01a13a34-63e6-7cc2-85d1-ddfbbec0e2ce"
        assert reply["result"]["usage"]["output_tokens"] == 5
        assert adapter.start_count == 1  # Fake runner: no codex.exe or network.


def test_retry_uses_failed_message_not_initial_goal_and_stays_in_history(client):
    cid = conversation(client)
    first = submit(client, cid)
    approve(client, first)
    run(client, first)
    second = submit(client, cid, "two", text="different message", agent_id="other-agent")
    approve(client, second)
    client.app.state.adapter.outcome = "unknown"
    run(client, second)
    # External terminal evidence allows a retry; uncertainty alone does not.
    client.app.state.adapter.store.set_status(second["invocation_id"], "failed")
    response = client.post(f"/api/invocations/{second['invocation_id']}/reconcile",
                           headers=USER, json={"action": "retry"})
    assert response.status_code == 200, response.text
    retry = {"invocation_id": response.json()["retry_invocation_id"],
             "approval_id": response.json()["retry_approval_id"]}
    with client.app.state.db.session() as s:
        inv = s.get(InvocationRow, retry["invocation_id"])
        assert inv.snapshot["task_input"] == "different message"
        assert inv.snapshot["agent_id"] == "other-agent"
        assert s.get(TaskRow, inv.task_id).status == "awaiting_approval"
    history = messages(client, cid)
    assert history[-1]["invocation"]["invocation_id"] == retry["invocation_id"]
    assert history[-1]["text"] == "different message"
    assert run(client, retry).status_code == 409
    assert client.app.state.adapter.start_count == 2
    client.app.state.adapter.outcome = "finished"
    approve(client, retry)
    assert run(client, retry).status_code == 200
    assert client.app.state.adapter.inputs == ["first task", "different message", "different message"]


def test_additive_schema_preserves_legacy_call_and_approval(tmp_path):
    db = Database(f"sqlite:///{(tmp_path / 'legacy.db').as_posix()}")
    Base.metadata.create_all(db.engine, tables=[
        t for t in Base.metadata.sorted_tables if t.name not in {"conversations", "chat_messages"}
    ])
    with db.session() as s:
        svc = PolicyService(s)
        task = svc.create_task(agent_id="mock", goal="existing task")
        inv = svc.create_invocation(task_id=task.id, snapshot=InvocationSnapshot(
            agent_id="mock", task_input="existing task", parent_task_id=None, depth=0, policy_version=1,
        ))
        appr = svc.create_approval(invocation_id=inv.id)
        original = (inv.id, inv.payload_hash, appr.id)
    app = create_app(db=db, adapter=MockAdapter())
    with TestClient(app) as c:
        conversation(c)
        assert count(c, InvocationRow) == count(c, ApprovalRow) == 1
    with db.session() as s:
        assert s.get(InvocationRow, original[0]).payload_hash == original[1]
        assert s.get(ApprovalRow, original[2]).status == "pending"


def test_recovery_does_not_overwrite_concurrent_unknown(client, monkeypatch):
    cid = conversation(client)
    turn = submit(client, cid)
    approve(client, turn)
    with client.app.state.db.session() as s:
        plan = PolicyService(s).consume(turn["invocation_id"])
    client.app.state.adapter.start(turn["invocation_id"], InvocationSnapshot(**plan["snapshot"]),
                                   gate_token=plan["gate_token"])
    lookup = client.app.state.adapter.lookup_by_invocation
    def raced_lookup(inv_id):
        with client.app.state.db.session() as s:
            PolicyService(s).mark_unknown(inv_id, reason="concurrent local stop")
        return lookup(inv_id)  # Late terminal evidence must not undo the stop decision.
    monkeypatch.setattr(client.app.state.adapter, "lookup_by_invocation", raced_lookup)
    response = client.post("/api/worker/recover", headers=USER)
    assert response.json()["settled"] == []
    assert len(messages(client, cid)) == 1
    with client.app.state.db.session() as s:
        assert s.get(InvocationRow, turn["invocation_id"]).status == "unknown"
        assert s.scalar(select(QuotaReservationRow)).released_at is None
