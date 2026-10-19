"""User-supervised operation does not waive the Hub approval gate.

Real processes here are two local sleeping Python children only; no Codex or network.
"""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import os
import sys
import threading
import time
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from agenthub.contracts import AdapterStartResult, InvocationSnapshot, Policy, execution_admission
from agenthub_adapters.codex import (
    CodexAdapter, SubprocessCodexRunner, CodexRun, classify_outcome, parse_events,
    file_identity, success_script,
)
from agenthub_adapters.mock import MockAdapter
from agenthub_kernel.db import ApprovalRow, Database, InvocationRow, QuotaReservationRow, TaskRow
from agenthub_kernel.service import PolicyService, QuotaError
from agenthub_rt.api import create_app
from tests.helpers import KernelHarness

USER = {"Authorization": "Bearer dev-user-token"}
AGENT = {"Authorization": "Bearer dev-agent-token"}


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(db=Database(f"sqlite:///{tmp_path / 'api.db'}"))) as c:
        yield c


def create(client, mode="strict", **kwargs):
    r = client.post("/api/invocations", json={"task_id": "-", "agent_id": "mock",
        "task_input": "offline test", "execution_mode": mode, **kwargs})
    assert r.status_code == 200, r.text
    return r.json()


def test_supervised_requires_explicit_user_consent(client):
    call = create(client, "supervised")
    approve = f"/api/approvals/{call['approval_id']}/approve"
    assert client.post(approve, headers=USER).status_code == 409
    assert client.post(approve, headers=USER, json={"accept_unmanaged_native_agents": False}).status_code == 409
    assert client.post(approve, headers=AGENT, json={"accept_unmanaged_native_agents": True}).status_code == 403
    assert client.post(approve, headers=USER, json={"accept_unmanaged_native_agents": "true"}).status_code == 422
    assert client.post("/api/worker/drain", headers=USER).json() == []
    assert client.app.state.adapter.start_count == 0
    assert client.post(approve, headers=USER, json={"accept_unmanaged_native_agents": True}).status_code == 200
    assert client.post("/api/worker/drain", headers=USER).json()[0]["ok"]
    assert client.post("/api/worker/drain", headers=USER).json() == []
    assert client.app.state.adapter.start_count == 1


def test_execution_mode_is_hash_bound_and_strict_hash_compatible():
    import hashlib
    import json
    snap = InvocationSnapshot("codex", "test", None, 0)
    assert "execution_mode" not in snap.to_dict()
    assert snap.payload_hash() == hashlib.sha256(json.dumps(snap.to_dict(), sort_keys=True).encode()).hexdigest()
    assert replace(snap, execution_mode="supervised").payload_hash() != snap.payload_hash()
    with pytest.raises(ValueError):
        replace(snap, execution_mode="anything")


def test_mode_tamper_after_approval_does_not_launch(client):
    call = create(client)
    client.post(f"/api/approvals/{call['approval_id']}/approve", headers=USER)
    with client.app.state.db.session() as s:
        inv = s.get(InvocationRow, call["invocation_id"])
        inv.snapshot = {**inv.snapshot, "execution_mode": "supervised"}
    result = client.post("/api/worker/drain", headers=USER).json()
    assert not result[0]["ok"]
    assert client.app.state.adapter.start_count == 0


@pytest.mark.parametrize("approval_action", ["reject", "revoke", "expire"])
def test_supervised_negative_approvals_never_launch(client, approval_action):
    call = create(client, "supervised")
    if approval_action == "expire":
        import datetime as dt
        client.post(f"/api/approvals/{call['approval_id']}/approve", headers=USER,
                    json={"accept_unmanaged_native_agents": True})
        with client.app.state.db.session() as s:
            s.get(ApprovalRow, call["approval_id"]).expires_at = dt.datetime(2000, 1, 1)
    else:
        client.post(f"/api/approvals/{call['approval_id']}/{approval_action}", headers=USER)
    client.post("/api/worker/drain", headers=USER)
    assert client.app.state.adapter.start_count == 0


def test_collaboration_extension_defaults_off_and_only_user_can_enable(client):
    assert Policy().collaboration_enabled is False
    root = create(client)["task_id"]
    path = f"/api/tasks/{root}/delegations"
    child = {"agent_id": "mock", "goal": "child", "parent_task_id": root}
    assert client.post(path, json=child).json()["detail"]["code"] == "collaboration_disabled"
    assert client.post("/api/invocations", json={"task_id": "-", "agent_id": "mock",
        "task_input": "child", "parent_task_id": root}).status_code == 409
    policy = f"/api/tasks/{root}/policy"
    assert client.patch(policy, json={"collaboration_enabled": True}, headers=AGENT).status_code == 403
    assert client.patch(policy, json={"collaboration_enabled": "true"}, headers=USER).status_code == 422
    enabled = client.patch(policy, json={"collaboration_enabled": True}, headers=USER).json()
    assert enabled["version"] == 2
    delegated = client.post(path, json=child).json()
    assert delegated["status"] == "awaiting_approval"
    assert client.post("/api/worker/drain", headers=USER).json() == []
    assert client.app.state.adapter.start_count == 0
    client.post(f"/api/approvals/{delegated['approval_id']}/approve", headers=USER)
    assert client.post("/api/worker/drain", headers=USER).json()[0]["ok"]


def test_disabling_extension_revokes_pending_but_keeps_running_child():
    h = KernelHarness(policy=Policy(collaboration_enabled=True))
    root = h.create_root()
    ids = []
    for n in range(2):
        child = h.create_subtask(root.id, str(n))
        inv = h.create_invocation(child.id)
        a = h.create_approval(inv.id)
        h.approve(a.id)
        ids.append((inv.id, a.id))
    h.worker.drain_once(max_calls=1)
    h.update_policy(root.id, Policy(collaboration_enabled=False))
    assert h.drain() == []
    assert h.adapter.start_count == 1
    with h.open_session() as s:
        assert s.get(InvocationRow, ids[0][0]).status == "running"
        assert s.get(ApprovalRow, ids[1][1]).status == "revoked"
        assert s.scalar(select(QuotaReservationRow).where(QuotaReservationRow.invocation_id == ids[0][0])).released_at is None


def test_consume_rechecks_extension_even_if_legacy_pending_approval_survived():
    h = KernelHarness(policy=Policy(collaboration_enabled=True))
    root = h.create_root()
    child = h.create_subtask(root.id, "child")
    inv = h.create_invocation(child.id)
    a = h.create_approval(inv.id)
    h.approve(a.id)
    with h.open_session() as s:
        row = s.get(TaskRow, root.id)
        row.policy = {**row.policy, "collaboration_enabled": False}
    assert h.drain()[0]["code"] == "collaboration_disabled"
    assert h.adapter.start_count == 0


def real_adapter(tmp_path, **overrides):
    args = dict(codex_path=__file__, expected_identity=file_identity(__file__),
        codex_home=str(tmp_path / "home"), workdir_root=str(tmp_path / "runs"))
    return CodexAdapter(**{**args, **overrides})


def test_supervised_admission_is_separate_from_strict(tmp_path, monkeypatch):
    launch = Mock(side_effect=AssertionError("no real process in this test"))
    monkeypatch.setattr("agenthub_adapters.codex.subprocess.Popen", launch)
    adapter = real_adapter(tmp_path)
    assert execution_admission(adapter).allowed is False
    assert execution_admission(adapter, "supervised").mode == "supervised"
    assert adapter.capabilities()["control_subagent"] is False
    assert adapter.capabilities()["cancel"] is False
    assert adapter.capabilities()["stop_local_process"] is True
    snap = InvocationSnapshot("codex", "test", None, 0, execution_mode="supervised")
    assert not adapter.start("x", snap).ok  # no gate token
    adapter.expected_identity = {"size": 0, "sha256": "wrong"}
    assert not adapter.start("x", snap, gate_token="test").ok  # identity drift
    launch.assert_not_called()


@pytest.mark.parametrize("change", [
    {"sandbox": "workspace-write"}, {"disable_subagents": False},
    {"timeout_seconds": 601}, {"timeout_seconds": float("nan")},
    {"timeout_seconds": 0}, {"expected_identity": None}, {"command_prefix": ["other"]},
])
def test_supervised_preconditions_fail_closed(tmp_path, change):
    assert not execution_admission(real_adapter(tmp_path, **change), "supervised").allowed


def test_supervised_codex_path_launches_once_after_consent_only(tmp_path, monkeypatch):
    """Exercise the real admission/runner path with ONLY the OS launch replaced."""
    class CompletedProcess:
        def wait(self, timeout=None):
            return 0
        def poll(self):
            return 0
    def launch(argv, **kwargs):
        kwargs["stdout"].write(success_script()(argv, {}).stdout_text)
        kwargs["stdout"].flush()
        return CompletedProcess()
    popen = Mock(side_effect=launch)
    monkeypatch.setattr("agenthub_adapters.codex.subprocess.Popen", popen)
    adapter = real_adapter(tmp_path)
    with TestClient(create_app(db=Database(f"sqlite:///{tmp_path / 'admission.db'}"), adapter=adapter)) as c:
        call = create(c, "supervised", agent_id="codex")
        assert c.post("/api/worker/drain", headers=USER).json() == []
        popen.assert_not_called()
        assert c.post(f"/api/approvals/{call['approval_id']}/approve", headers=USER).status_code == 409
        assert c.post(f"/api/approvals/{call['approval_id']}/approve", headers=USER,
                      json={"accept_unmanaged_native_agents": True}).status_code == 200
        result = c.post("/api/worker/drain", headers=USER).json()
        assert result[0]["ok"] and result[0]["terminal"]
        assert c.post("/api/worker/drain", headers=USER).json() == []
        assert popen.call_count == 1
        argv = popen.call_args.args[0]
        assert argv[argv.index("--sandbox") + 1] == "read-only"
        view = c.get(f"/api/tasks/{call['task_id']}/view", headers=USER).json()
        assert view["quota"]["open_reservations"] == 0
        assert view["invocations"][0]["status"] == "succeeded"


def test_stop_before_launch_prevents_even_popen(tmp_path, monkeypatch):
    runner = SubprocessCodexRunner(str(tmp_path))
    launch = Mock(side_effect=AssertionError("stop was recorded before launch"))
    monkeypatch.setattr("agenthub_adapters.codex.subprocess.Popen", launch)
    runner.stop_local("inv-a")
    run = runner.run_for_invocation("inv-a", ["unused"], {}, 1)
    assert run.local_stop_requested and run.exit_code is None
    assert classify_outcome(run, parse_events(run.stdout_text))[1] is False
    launch.assert_not_called()


def test_stop_does_not_claim_remote_terminal_even_with_complete_stdout():
    run = success_script()([], {})
    run.local_stop_requested = True
    assert classify_outcome(run, parse_events(run.stdout_text))[:2] == ("running", False)


def test_only_owned_python_entry_process_stops(tmp_path):
    runner = SubprocessCodexRunner(str(tmp_path))
    argv = [sys.executable, "-c", "import time; time.sleep(30)"]
    with ThreadPoolExecutor(2) as pool:
        a = pool.submit(runner.run_for_invocation, "a", argv, dict(os.environ), 10)
        b = pool.submit(runner.run_for_invocation, "b", argv, dict(os.environ), 10)
        try:
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                with runner._lock:
                    ready = len(runner._processes) == 2
                if ready:
                    break
                time.sleep(.02)
            assert ready, "test Python children did not start"
            result = runner.stop_local("a")
            assert result["local_process_exited"] and not result["remote_stop_confirmed"]
            assert a.result(timeout=3).local_stop_requested
            assert not b.done(), "unrelated invocation was terminated"
            assert runner.stop_local("missing")["local_process_exited"] is False
            assert not b.done()
        finally:
            runner.stop_local("a")
            runner.stop_local("b")
        assert b.result(timeout=3).local_stop_requested


class BlockingLocalAdapter(MockAdapter):
    def __init__(self, path):
        super().__init__(store_path=path)
        self.entered = threading.Event()
        self.release_start = threading.Event()
        self.stopped_ids = []

    def start(self, invocation_id, snapshot, **kwargs):
        super().start(invocation_id, snapshot, **kwargs)
        self.entered.set()
        assert self.release_start.wait(8)
        # Simulates a success response already in flight when stop arrives.
        self.store.set_status(invocation_id, "finished")
        return AdapterStartResult("external-test", ok=True, terminal=True)

    def stop_local_invocation(self, invocation_id):
        self.stopped_ids.append(invocation_id)
        self.release_start.set()
        return {"local_stop_requested": True, "local_process_exited": True}


def test_stop_racing_completion_keeps_unknown_reservation_and_blocks_retry(tmp_path):
    adapter = BlockingLocalAdapter(str(tmp_path / "adapter.db"))
    h = KernelHarness(url=f"sqlite:///{tmp_path / 'kernel.db'}", adapter=adapter)
    root = h.create_root()
    inv = h.create_invocation(root.id)
    appr = h.create_approval(inv.id)
    h.approve(appr.id)
    with ThreadPoolExecutor(1) as pool:
        future = pool.submit(h.drain)
        try:
            assert adapter.entered.wait(3)
            result = h.worker.stop_local(inv.id)
            assert result["status"] == "unknown" and result["quota_retained"]
            assert not result["remote_stop_confirmed"]
            assert future.result(timeout=5)[0]["terminal"] is False
        finally:
            adapter.release_start.set()
    assert adapter.stopped_ids == [inv.id]
    with h.open_session() as s:
        assert s.get(InvocationRow, inv.id).status == "unknown"
        assert s.scalar(select(QuotaReservationRow).where(QuotaReservationRow.invocation_id == inv.id)).released_at is None
    assert h.worker.recover()["settled"] == []
    with pytest.raises(QuotaError, match="cannot retry"):
        h.worker.reconcile(inv.id, action="retry")


def test_stop_api_authorization_and_inactive_calls(client):
    call = create(client)
    path = f"/api/invocations/{call['invocation_id']}/stop-local"
    assert client.post(path).status_code == 401
    assert client.post(path, headers=AGENT).status_code == 403
    assert client.post(path, headers=USER).status_code == 409
    assert client.post("/api/invocations/missing/stop-local", headers=USER).status_code == 404
    assert client.app.state.adapter.start_count == 0


def test_ui_exposes_boundaries_and_manual_stop(client):
    html = client.get("/ui").text
    for text in ['id="execution-mode"', 'id="collaboration-enabled"', "accept_unmanaged_native_agents",
                 "结束本次本地进程", "远端停止未确认", "setInterval", "stop-local"]:
        assert text in html
    assert 'option value="strict"' in html
    assert '运行中的调用会被取消' not in html
