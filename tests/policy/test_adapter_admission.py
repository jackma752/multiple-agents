"""Plan §4.3: approval cannot admit an uncontrolled native-agent runtime.

All databases are disposable. Popen is a tripwire: no real Codex is run.
"""
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from agenthub.contracts import AdapterContract, ExecutionAdmission, InvocationSnapshot, Policy, execution_admission
from agenthub_adapters.codex import CodexAdapter, FakeCodexRunner, StubCodexRunner, success_script
from agenthub_adapters.mock import MockAdapter
from agenthub_kernel.adapters import describe
from agenthub_kernel.db import ApprovalRow, InvocationRow, QuotaReservationRow, TreeQuotaRow
from agenthub_kernel.worker import Worker
from agenthub_rt.api import create_app
from tests.helpers import KernelHarness

USER = {"Authorization": "Bearer dev-user-token"}


@pytest.fixture
def no_process(monkeypatch):
    launch = Mock(side_effect=AssertionError("a blocked adapter reached Popen"))
    monkeypatch.setattr("agenthub_adapters.codex.subprocess.Popen", launch)
    return launch


def real_adapter(tmp_path, **kwargs):
    return CodexAdapter(
        codex_path=__file__, codex_home=str(tmp_path / "never-create-home"),
        workdir_root=str(tmp_path / "runs"), **kwargs,
    )


def approved(h, depth):
    root = h.create_root(agent_id="codex")
    task = root if depth == 0 else h.create_subtask(root.id, "child", agent_id="codex")
    inv = h.create_invocation(task.id)
    approval = h.create_approval(inv.id)
    h.approve(approval.id)
    return root.id, inv.id, approval.id


def assert_unconsumed(h, root_id, inv_id, approval_id):
    with h.open_session() as s:
        a = s.get(ApprovalRow, approval_id)
        assert a.status == "approved" and a.consumed_at is None
        inv = s.get(InvocationRow, inv_id)
        assert inv.status == "queued" and inv.started_at is None
        assert not inv.adapter_invocation_id
        quota = s.get(TreeQuotaRow, root_id)
        assert (quota.launched_total, quota.concurrent, quota.total_invocations) == (0, 0, 0)
        assert s.scalar(select(func.count()).select_from(QuotaReservationRow)) == 0


@pytest.mark.parametrize("depth", [0, 1])
def test_approved_real_call_blocked_before_consumption(tmp_path, no_process, depth):
    adapter = real_adapter(tmp_path)
    h = KernelHarness(url=f"sqlite:///{tmp_path / 'gate.db'}", adapter=adapter)
    ids = approved(h, depth)
    for _ in range(2):
        result = h.drain()
        assert len(result) == 1
        assert result[0]["code"] == "native_subagents_uncontrolled"
        assert result[0]["launch_attempted"] is False
        assert_unconsumed(h, *ids)
    assert adapter.start_count == 0
    assert adapter.store.get(ids[1]) is None
    assert not (tmp_path / "never-create-home").exists()
    no_process.assert_not_called()


def test_restart_and_concurrent_workers_cannot_consume_old_approvals(tmp_path, no_process):
    adapter = real_adapter(tmp_path)
    h = KernelHarness(url=f"sqlite:///{tmp_path / 'gate.db'}", adapter=adapter)
    ids = approved(h, 0)
    workers = [Worker(h.db, real_adapter(tmp_path)) for _ in range(2)]
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(lambda worker: worker.drain_once(), workers))
    assert all(r[0]["code"] == "native_subagents_uncontrolled" for r in results)
    assert_unconsumed(h, *ids)
    no_process.assert_not_called()


def test_direct_start_cannot_bypass_with_a_gate_token(tmp_path, no_process):
    adapter = real_adapter(tmp_path)
    result = adapter.start("inv-test", InvocationSnapshot("codex", "test", None, 0), gate_token="some-token")
    assert not result.ok and result.terminal
    assert adapter.start_count == 0 and adapter.store.get("inv-test") is None
    assert not (tmp_path / "never-create-home").exists()
    no_process.assert_not_called()


@pytest.mark.parametrize("fake_admission", [None, {"allowed": True}, ExecutionAdmission("yes", "strict"), ExecutionAdmission(True, "blocked"), ExecutionAdmission(True, [])])
def test_unknown_or_malformed_admission_fails_closed(fake_admission):
    adapter = Mock()
    adapter.execution_admission.return_value = fake_admission
    assert execution_admission(adapter).allowed is False
    assert execution_admission(AdapterContract()).allowed is False


def test_broken_capability_check_is_a_safe_denial():
    adapter = Mock()
    adapter.execution_admission.side_effect = RuntimeError("credential must not be reflected")
    admission = execution_admission(adapter)
    assert admission.allowed is False and "credential" not in admission.reason


def test_ui_and_http_cannot_override_admission(tmp_path, no_process):
    adapter = real_adapter(tmp_path)
    h = KernelHarness(adapter=adapter)
    app = create_app(db=h.db, adapter=adapter)
    with TestClient(app) as client:
        info = client.get("/api/adapter", headers=USER).json()
        assert info["execution_admission"]["allowed"] is False
        assert info["subagent_disable_requested"] is True
        assert info["subagents_disabled"] is False
        assert info["capabilities"]["control_subagent"] is False
        # A registry entry and a client's snapshot cannot attest host controls.
        assert client.post("/api/agents", json={"id": "codex", "name": "codex", "capabilities": {"control_subagent": True}}, headers=USER).status_code == 200
        created = client.post("/api/invocations", json={
            "task_id": "-", "agent_id": "codex", "task_input": "test",
            "permissions": {"strict": False, "execution_admission": {"allowed": True}},
        }, headers=USER).json()
        client.post(f"/api/approvals/{created['approval_id']}/approve", headers=USER)
        result = client.post("/api/worker/drain", headers=USER).json()
        assert result[0]["code"] == "native_subagents_uncontrolled"
        assert_unconsumed(h, created["task_id"], created["invocation_id"], created["approval_id"])
        html = client.get("/ui").text
        assert "子 Agent 已禁用" not in html
        assert "原生子 Agent 阻断未通过" in html
        assert "名额为 0 不证明历史原生子 Agent 已停止" in html
    no_process.assert_not_called()


def test_zero_child_limit_does_not_make_uncontrolled_root_safe(tmp_path, no_process):
    h = KernelHarness(adapter=real_adapter(tmp_path), policy=Policy(max_subagents_total=0, max_subagents_concurrent=0))
    ids = approved(h, 0)
    assert h.drain()[0]["code"] == "native_subagents_uncontrolled"
    assert_unconsumed(h, *ids)
    no_process.assert_not_called()


def test_offline_fixtures_still_require_approval(tmp_path, no_process):
    adapter = real_adapter(tmp_path, runner=FakeCodexRunner(success_script()))
    assert execution_admission(adapter).mode == "simulation"
    assert describe(adapter)["real_process"] is False
    h = KernelHarness(adapter=adapter)
    root = h.create_root(agent_id="codex")
    inv = h.create_invocation(root.id)
    a = h.create_approval(inv.id)
    assert h.drain() == [] and adapter.start_count == 0
    h.approve(a.id)
    assert h.drain()[0]["ok"] is True and adapter.start_count == 1
    assert execution_admission(MockAdapter()).allowed is True
    no_process.assert_not_called()


def test_stub_cannot_be_pointed_at_an_arbitrary_command(tmp_path, no_process):
    import sys
    adapter = real_adapter(tmp_path, command_prefix=[sys.executable], runner=StubCodexRunner(str(tmp_path / "scratch")))
    assert execution_admission(adapter).allowed is False
    from agenthub_adapters.codex import CodexLaunchFailed
    with pytest.raises(CodexLaunchFailed):
        adapter.runner.run([sys.executable, __file__, "exec"], {}, 1)
    no_process.assert_not_called()


def test_no_environment_opt_out(tmp_path, monkeypatch, no_process):
    monkeypatch.setenv("AGENT_HUB_STRICT", "false")
    monkeypatch.setenv("AGENT_HUB_ALLOW_UNCONTROLLED", "true")
    adapter = real_adapter(tmp_path, disable_subagents=False)
    assert execution_admission(adapter).allowed is False
    no_process.assert_not_called()


def test_real_runner_cannot_inherit_simulation_admission(tmp_path, no_process):
    from agenthub_adapters.codex import SubprocessCodexRunner
    adapter = real_adapter(tmp_path, runner=FakeCodexRunner(success_script()))
    assert execution_admission(adapter).allowed is True
    adapter.runner = SubprocessCodexRunner(str(tmp_path / "scratch"))
    assert execution_admission(adapter).allowed is False
    result = adapter.start("inv-test", InvocationSnapshot("codex", "test", None, 0), gate_token="approved")
    assert result.ok is False and adapter.start_count == 0
    no_process.assert_not_called()


def test_build_adapter_only_admits_the_bundled_stub(tmp_path, monkeypatch, no_process):
    import sys
    from agenthub_adapters.codex import bundled_stub_path
    from agenthub_kernel import config
    from agenthub_kernel.adapters import build_adapter

    settings = {
        "store_path": str(tmp_path / "exec.db"), "workdir_root": str(tmp_path / "runs"),
        "codex_path": bundled_stub_path(), "codex_home": str(tmp_path / "home"),
        "command_prefix": [sys.executable], "proxy": "", "ca_bundle": "",
        "timeout_seconds": 1, "sandbox": "read-only", "expected_identity": None,
        "version": "test",
    }
    monkeypatch.setattr(config, "codex_settings", lambda: settings)
    assert execution_admission(build_adapter("codex")).mode == "simulation"
    settings["codex_path"] = __file__
    assert execution_admission(build_adapter("codex")).allowed is False
    settings["codex_path"] = bundled_stub_path()
    settings["command_prefix"] = [sys.executable, "-c", "arbitrary command"]
    assert execution_admission(build_adapter("codex")).allowed is False
    no_process.assert_not_called()
