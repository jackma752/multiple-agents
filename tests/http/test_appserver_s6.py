"""S6-1: app-server hardening (docs/appserver-s6-plan.md §3-§6). Offline only.

In-memory scripted server or the bundled stdio stub; a fake file stands in for
the pinned executable and every launch path is replaced by a sentinel, so no
Codex process, model call or credential is involved.

Decline-only behaviour is exercised on the in-memory stub by switching the
adapter's ``accept_enabled`` off, which is what a real factory gets.
"""

import hashlib
import json
import math
import os
import threading
import time
from pathlib import Path

import pytest
from sqlalchemy import select

from agenthub.contracts import InvocationSnapshot, execution_admission
from agenthub_adapters import appserver as appserver_module
from agenthub_adapters import appserver_adapter as aa
from agenthub_adapters import proc_tree
from agenthub_adapters.appserver_adapter import (
    APPSERVER_ARGV_TAIL, PINNED_APPSERVER, REAL_APPSERVER_BLOCK_REASON, SUPERVISED_REASON,
    THREAD_SETTINGS, TURN_POLICY, AppServerCodexAdapter, CodexAppServerFactory, LaunchRefused,
    _fit_decision, command_view, settings_problems,
)
from agenthub_adapters.appserver_stub import (
    DEFAULT_DECISIONS, REPLAY_USAGE, InMemoryStubFactory, StdioStubFactory,
)
from agenthub_adapters.mock import MockAdapter
from agenthub_adapters.store import ExecutionStore
from agenthub_kernel import config
from agenthub_kernel.adapters import adapter_binding, describe
from agenthub_kernel.db import ApprovalRow, InvocationRow, ToolApprovalRow
from tests.http.test_async_and_tool_approval import cards, eventually, pending_card, settle, start, status_of
from tests.http.test_conversation_events import events, types
from tests.http.test_conversation_stream import (  # noqa: F401  (make_client is a fixture)
    held_slots, make_adapter, make_client, submit, turn_and_run,
)
from tests.http.test_conversations import USER, approve, conversation, run

STUB_BINDING = "appserver-stub:in-memory"
REPO = Path(__file__).resolve().parents[2]


def snap(adapter, text="x", **extra):
    return InvocationSnapshot(agent_id="codex", task_input=text, parent_task_id=None, depth=0,
                              adapter_version=adapter.binding_identity(), **extra)


def payload_of(adapter, invocation_id):
    return adapter.store.get(invocation_id)["payload"]


def snapshot_of(client, invocation_id):
    with client.app.state.db.session() as s:
        return dict(s.get(InvocationRow, invocation_id).snapshot)


# --------------------------------------------------------------------------- #
# admission matrix (§4.1, §6)
# --------------------------------------------------------------------------- #


@pytest.fixture
def pinned_file(tmp_path, monkeypatch):
    """A fake file pinned in place of the real executable (constants patched for the test)."""
    exe = tmp_path / "bin" / "codex.exe"
    exe.parent.mkdir()
    exe.write_bytes(b"not a real codex binary")
    monkeypatch.setitem(PINNED_APPSERVER, "path", str(exe))
    monkeypatch.setitem(PINNED_APPSERVER, "sha256", hashlib.sha256(exe.read_bytes()).hexdigest())
    monkeypatch.setitem(PINNED_APPSERVER, "size", exe.stat().st_size)
    ca = tmp_path / "ca.pem"
    ca.write_text("-----BEGIN CERTIFICATE-----\n")
    return exe, ca


@pytest.fixture
def no_launch(monkeypatch):
    """Any attempt to spawn the (fake) app-server is recorded and refused."""
    calls = []

    def sentinel(argv, **kwargs):
        calls.append(list(argv))
        raise OSError("sentinel: launch blocked in tests")

    monkeypatch.setattr(appserver_module, "StdioTransport", sentinel)
    return calls


def real_factory(tmp_path, ca, **overrides):
    kwargs = {"codex_home": str(tmp_path / "home"), "proxy": "http://127.0.0.1:8090",
              "ca_bundle": str(ca)}
    kwargs.update(overrides)
    return CodexAppServerFactory.pinned(**kwargs)


def real_adapter(tmp_path, factory, **kwargs):
    return AppServerCodexAdapter(transport_factory=factory, workdir_root=str(tmp_path / "runs"),
                                 store=ExecutionStore(str(tmp_path / "x.db"), table="appserver_executions"),
                                 **kwargs)


def test_supervised_admits_only_the_pinned_factory_and_strict_never(tmp_path, pinned_file):
    _exe, ca = pinned_file
    adapter = real_adapter(tmp_path, real_factory(tmp_path, ca))
    strict = execution_admission(adapter, "strict")
    assert not strict.allowed and strict.code == "appserver_real_unverified"
    supervised = execution_admission(adapter, "supervised")
    assert supervised.allowed and supervised.mode == "supervised"
    assert supervised.code == "user_supervised_boundary"
    assert adapter.accept_enabled is False  # decline-only for a real factory
    assert adapter.capabilities()["tool_accept"] is False


@pytest.mark.parametrize("change, problem", [
    (lambda f, t, e, c: setattr(f, "env_overrides", ("AGENT_HUB_CODEX_PATH",)), "env_override"),
    (lambda f, t, e, c: f.argv.append("--extra"), "argv_changed"),
    (lambda f, t, e, c: setattr(f, "proxy", None), "proxy_missing"),
    (lambda f, t, e, c: setattr(f, "ca_bundle", str(t / "missing.pem")), "ca_unreadable"),
    (lambda f, t, e, c: e.write_bytes(b"different length now"), "size_mismatch"),
    (lambda f, t, e, c: e.unlink(), "executable_missing"),
    (lambda f, t, e, c: setattr(f, "version", "codex-cli 0.0.1"), "identity_not_pinned_constant"),
])
def test_each_supervised_condition_blocks(tmp_path, pinned_file, change, problem):
    exe, ca = pinned_file
    factory = real_factory(tmp_path, ca)
    change(factory, tmp_path, exe, ca)
    admission = execution_admission(real_adapter(tmp_path, factory), "supervised")
    assert not admission.allowed and admission.code == "appserver_supervised_conditions"
    assert problem in admission.reason


def test_a_factory_not_built_from_the_constant_is_refused(tmp_path, pinned_file):
    exe, ca = pinned_file
    copy = tmp_path / "copy.exe"
    copy.write_bytes(exe.read_bytes())
    factory = CodexAppServerFactory(codex_path=str(copy), codex_home=str(tmp_path / "home"),
                                    expected_identity={"sha256": PINNED_APPSERVER["sha256"],
                                                       "size": PINNED_APPSERVER["size"]},
                                    version=PINNED_APPSERVER["version"], proxy="http://p:1",
                                    ca_bundle=str(ca))
    admission = execution_admission(real_adapter(tmp_path, factory), "supervised")
    assert not admission.allowed and "identity_not_pinned_constant" in admission.reason


@pytest.mark.parametrize("timeout", [0, 601, math.inf, math.nan, -1])
def test_supervised_needs_a_bounded_local_deadline(tmp_path, pinned_file, timeout):
    _exe, ca = pinned_file
    adapter = real_adapter(tmp_path, real_factory(tmp_path, ca), timeout_seconds=timeout)
    admission = execution_admission(adapter, "supervised")
    assert not admission.allowed and "timeout" in admission.reason


def test_supervised_refuses_an_adapter_that_could_answer_accept(tmp_path, pinned_file):
    _exe, ca = pinned_file
    adapter = real_adapter(tmp_path, real_factory(tmp_path, ca))
    adapter.accept_enabled = True
    admission = execution_admission(adapter, "supervised")
    assert not admission.allowed and "tool_accept_enabled" in admission.reason


def test_lookalike_factory_type_is_refused(tmp_path, pinned_file):
    _exe, ca = pinned_file

    class Lookalike(CodexAppServerFactory):
        pass

    factory = Lookalike.pinned(codex_home=str(tmp_path / "h"), proxy="http://p:1", ca_bundle=str(ca))
    admission = execution_admission(real_adapter(tmp_path, factory), "supervised")
    assert not admission.allowed and admission.code == "appserver_real_unverified"


def test_offline_stubs_stay_simulation(tmp_path):
    for factory in (InMemoryStubFactory("stream"), StdioStubFactory("stream")):
        admission = execution_admission(make_adapter(tmp_path, factory), "supervised")
        assert admission.allowed and admission.mode == "simulation"


def test_file_replaced_after_admission_is_never_launched(tmp_path, pinned_file, no_launch):
    exe, ca = pinned_file
    adapter = real_adapter(tmp_path, real_factory(tmp_path, ca))
    assert execution_admission(adapter, "supervised").allowed
    exe.write_bytes(b"NOT a real codex binary")  # same size, other content
    assert exe.stat().st_size == PINNED_APPSERVER["size"]
    with pytest.raises(LaunchRefused, match="sha256 mismatch"):
        adapter.transport_factory("inv-direct", str(tmp_path))
    result = adapter.start_streaming("inv-1", snap(adapter, execution_mode="supervised"), gate_token="t")
    assert result.ok is False and result.terminal is True and "sha256 mismatch" in result.detail
    assert no_launch == []  # nothing was spawned


def test_matching_file_reaches_the_launch_with_the_verified_argv(tmp_path, pinned_file, no_launch):
    exe, ca = pinned_file
    adapter = real_adapter(tmp_path, real_factory(tmp_path, ca))
    result = adapter.start_streaming("inv-1", snap(adapter, execution_mode="supervised"), gate_token="t")
    assert result.ok is False and "sentinel" in result.detail
    assert no_launch == [[str(exe), *APPSERVER_ARGV_TAIL]]
    assert APPSERVER_ARGV_TAIL == ("app-server", "--stdio", "--disable", "multi_agent")


def test_build_adapter_uses_the_constant_and_records_env_overrides(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_HUB_ADAPTER", "codex")
    monkeypatch.setenv("AGENT_HUB_CODEX_TRANSPORT", "app-server")
    monkeypatch.delenv("AGENT_HUB_APPSERVER_STUB", raising=False)
    monkeypatch.setenv("AGENT_HUB_CODEX_PATH", str(tmp_path / "elsewhere.exe"))
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path))
    from agenthub_kernel.adapters import build_adapter

    adapter = build_adapter()
    factory = adapter.transport_factory
    assert type(factory) is CodexAppServerFactory
    assert factory.codex_path == PINNED_APPSERVER["path"]  # the override is not used...
    assert factory.env_overrides == ("AGENT_HUB_CODEX_PATH",)  # ...and it blocks supervised
    assert "env_override" in execution_admission(adapter, "supervised").reason
    assert adapter.version == PINNED_APPSERVER["version"]


def test_pinned_identity_is_the_staged_alpha11_2_and_separate_from_exec():
    assert PINNED_APPSERVER["version"] == "codex-cli 0.163.0-alpha.11.2"
    assert PINNED_APPSERVER["path"].endswith(
        os.path.join("AgentHub", "runtimes", "codex", "sha256-c81ed464e5e7cafd", "codex.exe"))
    assert PINNED_APPSERVER["sha256"] == "c81ed464e5e7cafd09a5fa26af390c480bed8e9fab2a87a54b285fbd56e7122a"
    assert PINNED_APPSERVER["size"] == 336134960
    assert config.PINNED_CODEX_VERSION != PINNED_APPSERVER["version"]  # exec is unchanged


# --------------------------------------------------------------------------- #
# D11: identity binding
# --------------------------------------------------------------------------- #


def test_every_snapshot_path_binds_the_serving_adapter(make_client):
    factory = InMemoryStubFactory("stream")
    client = make_client(factory)
    cid = conversation(client)
    turn = submit(client, cid)
    assert snapshot_of(client, turn["invocation_id"])["adapter_version"] == STUB_BINDING
    r = client.post("/api/invocations", json={"task_id": "t", "agent_id": "codex", "task_input": "x"})
    assert r.status_code == 200, r.text
    assert snapshot_of(client, r.json()["invocation_id"])["adapter_version"] == STUB_BINDING
    assert describe(client.app.state.adapter)["binding_identity"] == STUB_BINDING


def test_identity_change_refuses_before_consuming_the_approval(make_client):
    factory = InMemoryStubFactory("stream")
    client = make_client(factory)
    cid = conversation(client)
    turn = submit(client, cid)
    approve(client, turn)
    factory.binding_identity = lambda: "appserver-stub:re-pinned"  # e.g. a new pinned build
    r = run(client, turn)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "adapter_identity_changed"
    assert client.app.state.adapter.start_count == 0 and factory.servers == []
    with client.app.state.db.session() as s:
        assert s.get(ApprovalRow, turn["approval_id"]).status == "approved"  # not consumed
    assert held_slots(client) == 0


def test_direct_start_with_another_binding_launches_nothing(tmp_path):
    factory = InMemoryStubFactory("stream")
    adapter = make_adapter(tmp_path, factory)
    stale = InvocationSnapshot(agent_id="codex", task_input="x", parent_task_id=None, depth=0)
    result = adapter.start_streaming("inv-1", stale, gate_token="t")
    assert result.ok is False and result.terminal is True and "another adapter identity" in result.detail
    assert adapter.start_count == 0 and factory.servers == []


def test_switching_to_an_adapter_without_a_binding_cannot_run_the_old_approval(make_client, tmp_path):
    """Review finding: the check compares BOTH sides, even if the new adapter declares none."""
    from tests.adapters.test_codex_adapter import make_adapter as make_exec_adapter

    factory = InMemoryStubFactory("stream")
    client = make_client(factory)
    cid = conversation(client)
    turn = submit(client, cid)
    approve(client, turn)
    exec_adapter = make_exec_adapter(tmp_path)  # FakeCodexRunner: simulation, no process
    assert adapter_binding(exec_adapter) == "unknown"
    client.app.state.worker.adapter = exec_adapter
    client.app.state.adapter = exec_adapter
    r = run(client, turn)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "adapter_identity_changed"
    assert exec_adapter.start_count == 0 and exec_adapter.runner.calls == []
    with client.app.state.db.session() as s:
        assert s.get(ApprovalRow, turn["approval_id"]).status == "approved"  # not consumed
    # The exec adapter refuses such a snapshot by itself as well.
    direct = exec_adapter.start("inv-x", InvocationSnapshot(
        agent_id="codex", task_input="x", parent_task_id=None, depth=0,
        adapter_version=STUB_BINDING), gate_token="t")
    assert direct.ok is False and direct.terminal is True and "another adapter identity" in direct.detail
    assert exec_adapter.start_count == 0 and exec_adapter.runner.calls == []


def test_an_unavailable_binding_never_matches(make_client):
    factory = InMemoryStubFactory("stream")
    client = make_client(factory)
    factory.binding_identity = lambda: ""  # broken declaration -> "unavailable" on both sides
    cid = conversation(client)
    turn = submit(client, cid)
    assert snapshot_of(client, turn["invocation_id"])["adapter_version"] == "unavailable"
    approve(client, turn)
    r = run(client, turn)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "adapter_identity_changed"
    assert factory.servers == []


def test_adapters_without_a_binding_keep_unknown_and_old_hashes(make_client, tmp_path):
    client = make_client(adapter=MockAdapter())
    cid = conversation(client)
    r = client.post(f"/api/conversations/{cid}/invocations", headers=USER, json={
        "text": "x", "agent_id": "mock", "client_request_id": "m"})
    assert r.status_code == 200, r.text
    snapshot = snapshot_of(client, r.json()["invocation_id"])
    assert snapshot["adapter_version"] == "unknown"
    # Since T-1d a new turn also binds its context preview (stored snapshots keep
    # their hashes); the binding itself adds nothing for an adapter without one.
    preview = snapshot["context_preview"]
    assert preview["adapter_text"] == {"status": "unknown"}  # the Mock declares no profile
    legacy = InvocationSnapshot(agent_id="mock", task_input="x", parent_task_id=None, depth=0,
                                policy_version=snapshot["policy_version"], context_preview=preview)
    assert r.json()["payload_hash"] == legacy.payload_hash()
    assert adapter_binding(MockAdapter()) == "unknown"


# --------------------------------------------------------------------------- #
# D2: fixed settings, read back
# --------------------------------------------------------------------------- #


def test_thread_and_turn_requests_carry_the_fixed_settings(tmp_path):
    factory = InMemoryStubFactory("stream")
    adapter = make_adapter(tmp_path, factory)
    assert adapter.start_streaming("inv-1", snap(adapter), gate_token="t").ok
    received = factory.servers[0].received
    thread = next(m for m in received if m.get("method") == "thread/start")
    assert thread["params"] == {"cwd": str(tmp_path / "runs" / "inv-1"), **THREAD_SETTINGS}
    turn = next(m for m in received if m.get("method") == "turn/start")
    assert {k: turn["params"][k] for k in TURN_POLICY} == TURN_POLICY
    assert turn["params"]["sandboxPolicy"] == {"type": "readOnly", "networkAccess": False}


def test_settings_mismatch_fails_before_any_turn_and_frees_the_slot(make_client):
    factory = InMemoryStubFactory("settings-mismatch")
    client = make_client(factory)
    cid = conversation(client)
    turn, result = turn_and_run(client, cid, "one")
    assert result["status"] == "failed" and held_slots(client) == 0
    assert "turn/start" not in factory.servers[0].received_methods()
    detail = client.app.state.adapter.store.get(turn["invocation_id"])["detail"]
    assert "appserver_settings_mismatch: sandbox" in detail


@pytest.mark.parametrize("result, problem", [
    ({"sandbox": "read-only"}, "sandbox"),  # the legacy string is not the verified object
    ({"sandbox": {"type": "readOnly", "networkAccess": True}}, "sandbox"),
    ({"sandbox": {"type": "readOnly", "writableRoots": []}}, "sandbox"),
    ({"approvalPolicy": "never"}, "approvalPolicy"),
    ({"approvalsReviewer": "auto_review"}, "approvalsReviewer"),
    ({"cwd": "relative\\dir"}, "cwd"),
    ({"cwd": "C:\\elsewhere"}, "cwd"),
])
def test_settings_read_back_rejects_each_difference(result, problem):
    good = {"approvalsReviewer": "user", "approvalPolicy": "on-request",
            "sandbox": {"type": "readOnly", "networkAccess": False}, "cwd": "C:\\work\\a"}
    assert settings_problems(good, "C:\\work\\a") == []
    assert problem in settings_problems({**good, **result}, "C:\\work\\a")


# --------------------------------------------------------------------------- #
# §4.4: tool approvals (decline-only, closure gate, never cancel)
# --------------------------------------------------------------------------- #


def decline_only_call(make_client, tmp_path, scenario="approval-default", *, ttl=10, **kwargs):
    factory = InMemoryStubFactory(scenario)
    adapter = make_adapter(tmp_path, factory, **kwargs)
    adapter.accept_enabled = False  # what a real factory gets
    client = make_client(adapter=adapter)
    client.app.state.worker.tool_approval_ttl = ttl
    cid = conversation(client)
    turn = submit(client, cid)
    approve(client, turn)
    assert start(client, turn).status_code == 202
    return client, factory, cid, turn


def test_decline_only_card_offers_decline_and_shows_the_split_command(make_client, tmp_path):
    client, factory, cid, turn = decline_only_call(make_client, tmp_path)
    card = pending_card(client, cid)
    assert card["available_decisions"] == ["decline"]
    details = card["details"]
    assert details["provider_available_decisions"] == DEFAULT_DECISIONS
    view = details["command_view"]
    assert view["shell"].lower().endswith("powershell.exe") and view["inner_command"] == "Get-ChildItem -Name"
    assert details["reason_source"] == "model"
    approve_try = client.post(f"/api/tool-approvals/{card['tool_approval_id']}/approve",
                              headers=USER, json={"card_hash": card["card_hash"]})
    assert approve_try.status_code == 409 and approve_try.json()["detail"]["code"] == "accept_not_offered"
    assert client.post(f"/api/tool-approvals/{card['tool_approval_id']}/decline",
                       headers=USER).status_code == 200
    settle(client)
    # D1: decline is sent as decline although the provider's list has no decline.
    assert factory.servers[0].answered_approvals() == [{"id": "srv-1", "decision": "decline"}]
    assert status_of(client, turn["invocation_id"]) == "succeeded"
    final = client.get(f"/api/tool-approvals/{card['tool_approval_id']}", headers=USER).json()
    assert final["delivery"] == "sent" and final["decision_sent"] == "decline"
    assert payload_of(client.app.state.adapter, turn["invocation_id"])["tool_process_state"] == "items_completed"


def test_forged_approval_is_never_written_as_accept(make_client, tmp_path):
    client, factory, cid, turn = decline_only_call(make_client, tmp_path)
    card = pending_card(client, cid)
    with client.app.state.db.session() as s:  # bypasses the API on purpose
        s.get(ToolApprovalRow, card["tool_approval_id"]).status = "approved"
    settle(client)
    assert factory.servers[0].answered_approvals() == [{"id": "srv-1", "decision": "decline"}]
    payload = payload_of(client.app.state.adapter, turn["invocation_id"])
    assert payload["accept_suppressed"] == [card["tool_approval_id"]]


def test_expired_card_with_an_open_gate_sends_one_decline(make_client, tmp_path):
    client, factory, cid, turn = decline_only_call(make_client, tmp_path, ttl=0.3)
    settle(client)
    assert factory.servers[0].answered_approvals() == [{"id": "srv-1", "decision": "decline"}]
    [card] = cards(client, cid)
    assert card["status"] == "expired" and card["decision_sent"] == "decline"


def test_void_card_after_the_effective_point_sends_nothing(make_client, tmp_path):
    client, factory, cid, turn = decline_only_call(make_client, tmp_path, "approval-hold",
                                                   timeout_seconds=1.5)
    card = pending_card(client, cid)
    r = client.post(f"/api/invocations/{turn['invocation_id']}/interrupt", headers=USER)
    assert r.status_code in (200, 202), r.text
    late = client.post(f"/api/tool-approvals/{card['tool_approval_id']}/decline", headers=USER)
    assert late.status_code == 409
    settle(client)
    assert factory.servers[0].answered_approvals() == []  # not even a decline
    final = client.get(f"/api/tool-approvals/{card['tool_approval_id']}", headers=USER).json()
    assert final["status"] == "void" and final["decision_sent"] is None


def test_never_cancel_even_with_accept_enabled_offline(make_client):
    factory = InMemoryStubFactory("approval-default")
    client = make_client(factory)
    client.app.state.worker.tool_approval_ttl = 10
    cid = conversation(client)
    turn = submit(client, cid)
    approve(client, turn)
    assert start(client, turn).status_code == 202
    card = pending_card(client, cid)
    assert card["available_decisions"] == DEFAULT_DECISIONS  # stub: provider list as is
    client.post(f"/api/tool-approvals/{card['tool_approval_id']}/decline", headers=USER)
    settle(client)
    assert factory.servers[0].answered_approvals() == [{"id": "srv-1", "decision": "decline"}]
    assert status_of(client, turn["invocation_id"]) == "succeeded"  # not interrupted


@pytest.mark.parametrize("decision, offered, sent", [
    ("decline", ["accept", "cancel"], "decline"),
    ("decline", None, "decline"),
    ("accept", ["accept", "cancel"], "accept"),
    ("accept", ["decline"], "decline"),
    ("cancel", ["accept", "cancel"], "decline"),
])
def test_fit_decision_never_produces_cancel(decision, offered, sent):
    assert _fit_decision(decision, offered) == sent


@pytest.mark.parametrize("text, expected", [
    ('"C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe" -Command \'dir\'',
     {"inner_command": "dir", "shell_flags": ["-Command"]}),
    ('"C:\\Program Files\\PowerShell\\7\\pwsh.exe" -NoProfile -Command \'Get-Date\'',
     {"inner_command": "Get-Date", "shell_flags": ["-NoProfile", "-Command"]}),
    ("cmd.exe /c 'echo hi'", {"inner_command": "echo hi", "shell_flags": ["/c"]}),
    ("git status", {"argv": ["git", "status"]}),
    # Windows paths keep every backslash, in either quote style; spaces stay inside one word.
    ("'C:\\Program Files\\PowerShell\\7\\pwsh.exe' -Command 'Get-ChildItem C:\\stub'",
     {"shell": "C:\\Program Files\\PowerShell\\7\\pwsh.exe", "inner_command": "Get-ChildItem C:\\stub"}),
    ('"C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe" -Command \'Get-ChildItem -Name\'',
     {"shell": "C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe",
      "inner_command": "Get-ChildItem -Name"}),
    # Quotes inside quotes: the shlex_join encodings are decoded exactly.
    ("\"C:\\\\tools\\\\run.exe\" 'it'\"'\"'s' \"say \\\"hi\\\"\"",
     {"argv": ["C:\\tools\\run.exe", "it's", 'say "hi"']}),
    ("'C:\\Windows\\System32\\cmd.exe' /c 'type \"C:\\a b\\c.txt\"'",
     {"inner_command": 'type "C:\\a b\\c.txt"'}),
    ("echo '中文 路径'", {"argv": ["echo", "中文 路径"]}),
])
def test_command_view_splits_the_wrapper(text, expected):
    view = command_view(text)
    assert view["parsed"] is True
    assert {k: view[k] for k in expected} == expected


@pytest.mark.parametrize("text", [
    "", None, "'unterminated", "a\0b", '"open',
    "dir C:\\stub",              # unquoted backslash: escape or path separator? not guessed
    "echo hi | more",            # unquoted shell syntax is not shlex_join output
    "echo $HOME", 'echo "$HOME"', "echo 中文",
    "line one\nline two",
])
def test_command_view_marks_unparseable(text):
    """Review finding: an unreliable split is never shown; the original stays the only text."""
    assert command_view(text) == {"parsed": False}


def test_s3_scope_check_is_untouched_by_the_display_split():
    from agenthub_adapters.appserver_s3_evidence import classify_command

    shell = ("powershell", "C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe")
    from agenthub_adapters.appserver_s3_evidence import COMMAND

    wrapped = f'"{shell[1]}" -Command \'{COMMAND}\''
    assert classify_command(wrapped, shell) == ("powershell_command", "match")
    assert classify_command(COMMAND, shell) == (None, "unwrapped_fixed_command")


# --------------------------------------------------------------------------- #
# D4 / D5 / D8 / D10
# --------------------------------------------------------------------------- #


def test_tool_item_without_completion_is_reported_unknown(make_client):
    factory = InMemoryStubFactory("tool-unfinished")
    client = make_client(factory)
    cid = conversation(client)
    turn, result = turn_and_run(client, cid, "one")
    assert result["status"] == "succeeded"  # the main turn did complete
    payload = payload_of(client.app.state.adapter, turn["invocation_id"])
    assert payload["tool_process_state"] == "unknown"
    assert payload["tool_activity"] == [{"item_type": "commandExecution", "item_id": "item-cmd-9",
                                         "process_id_reported": True, "completed": False, "status": "",
                                         "lifecycle_seen": True, "output_events": 0}]
    assert "tool process state unknown" in payload["detail"]
    ended = [e for e in events(client, cid)["events"] if e["type"] == "stream.turn_ended"]
    assert ended[0]["payload"]["tool_process_state"] == "unknown"


def test_tool_output_without_an_item_is_recorded_as_unknown_activity(tmp_path):
    """Review finding: output notifications count as tool activity; no text is kept."""
    factory = InMemoryStubFactory("tool-output-only")
    adapter = make_adapter(tmp_path, factory)
    assert adapter.start_streaming("inv-1", snap(adapter), gate_token="t").ok
    payload = payload_of(adapter, "inv-1")
    assert payload["tool_process_state"] == "unknown"
    assert payload["tool_activity"] == [{
        "item_type": "commandExecution", "item_id": "item-cmd-7", "process_id_reported": False,
        "completed": False, "status": "", "lifecycle_seen": False, "output_events": 2}]
    assert payload["foreign_events"] >= 1  # the other thread's output is not ours
    assert "tool-output-secret" not in json.dumps(payload, ensure_ascii=False)


class _BatchClient:
    """Hands the pump one batch; then nothing (optionally a closed stream)."""

    def __init__(self, batch, *, requests=(), disconnected=False):
        self._batch = list(batch)
        self._requests = list(requests)
        self.disconnected = disconnected

    def notifications(self):
        batch, self._batch = self._batch, []
        return batch

    def server_requests(self):
        return self._requests

    def wait_for(self, _predicate, *, timeout):
        return None


def _post_terminal_batch(turn):
    from agenthub_adapters.appserver import Notification

    bound = {"threadId": turn.thread_id, "turnId": turn.turn_id}
    return [
        Notification("item/agentMessage/delta", {**bound, "itemId": "m", "delta": "OK"}),
        Notification("turn/completed", {"threadId": turn.thread_id,
                                        "turn": {"id": turn.turn_id, "status": "completed"}}),
        Notification("item/started", {**bound, "item": {"id": "c1", "type": "commandExecution"}}),
        Notification("item/agentMessage/delta", {"threadId": "other", "turnId": "x", "delta": "?"}),
    ]


@pytest.mark.parametrize("path", ["unrouted_request", "disconnect_drain"])
def test_events_after_the_terminal_in_one_batch_are_counted_late(tmp_path, path):
    """Review finding: the rest of a drained batch was dropped after the terminal event."""
    from agenthub_adapters.appserver import ServerRequest

    adapter = make_adapter(tmp_path, InMemoryStubFactory("stream"))
    turn = aa._Turn("th-1", "tu-1")
    if path == "unrouted_request":
        request = ServerRequest(id=9, method="item/commandExecution/requestApproval",
                                params={"threadId": "th-1", "turnId": "tu-1", "itemId": "c1"})
        client = _BatchClient(_post_terminal_batch(turn), requests=[request])
        gate = object()  # never reached: the batch ends the turn first
    else:
        client = _BatchClient(_post_terminal_batch(turn), disconnected=True)
        gate = None
    assert adapter._pump(client, turn, aa._ignore, gate) is True
    assert turn.terminal_seen and turn.turn_status == "completed"
    assert turn.late_events == 1  # the bound tool item; the other thread's delta is not ours
    assert turn.tools == {}  # a late event is not attributed as this turn's activity


def test_slow_interrupt_ack_is_not_yet_acknowledged_and_the_terminal_decides(tmp_path, monkeypatch):
    monkeypatch.setattr(aa, "INTERRUPT_REQUEST_TIMEOUT", 0.0)
    factory = InMemoryStubFactory("interrupt-terminal")
    adapter = make_adapter(tmp_path, factory, timeout_seconds=10)
    box = {}
    runner = threading.Thread(target=lambda: box.update(
        r=adapter.start_streaming("inv-1", snap(adapter), gate_token="t")))
    runner.start()
    eventually(lambda: "inv-1" in adapter._live)
    asked = adapter.interrupt_invocation("inv-1")
    assert asked["interrupt_accepted"] is False and asked["reason"] == "not_yet_acknowledged"
    runner.join(timeout=10)
    assert box["r"].terminal is True and box["r"].outcome == "canceled"


def test_events_without_thread_id_are_not_attributed(tmp_path):
    factory = InMemoryStubFactory("unbound-delta")
    adapter = make_adapter(tmp_path, factory)
    seen = []
    result = adapter.start_streaming("inv-1", snap(adapter), gate_token="t",
                                     emit=lambda etype, payload: seen.append((etype, payload)))
    assert result.ok
    deltas = [p["text"] for etype, p in seen if etype == "stream.delta"]
    assert "UNBOUND" not in deltas and deltas == ["OK"]
    assert payload_of(adapter, "inv-1")["foreign_events"] >= 1


def test_stdio_call_records_descendants_and_keeps_the_cwd_empty(tmp_path):
    adapter = make_adapter(tmp_path, StdioStubFactory("stream"))
    assert adapter.start_streaming("inv-1", snap(adapter), gate_token="t").ok
    cwd = tmp_path / "runs" / "inv-1"
    assert list(cwd.iterdir()) == []  # logs live beside the work dirs (D6)
    assert (tmp_path / "runs" / "_logs" / "stub-inv-1.log").exists()
    report = payload_of(adapter, "inv-1")["descendants"]
    if os.name == "nt":
        assert isinstance(report["before_close"], list) and report["after_grace"] == []


def test_survivors_are_only_recorded(monkeypatch):
    monkeypatch.setattr(proc_tree, "process_table", lambda: [(11, 1, "b.exe"), (12, 1, "c.exe")])
    unknown = lambda pid: None  # noqa: E731 - creation times unreadable: links are kept
    assert proc_tree.survivors([(10, "a.exe"), (11, "b.exe")], 0, unknown) == [(11, "b.exe")]
    assert proc_tree.descendants(1, [(11, 1, "b.exe"), (13, 11, "d.exe"), (14, 9, "e.exe")], unknown) == [
        (11, "b.exe"), (13, "d.exe")]


def test_a_reused_parent_id_does_not_make_an_old_process_a_descendant():
    """Seen offline: a tray program whose dead parent's id was reused by a fresh launcher."""
    born = {100: 5000, 101: 5001, 77: 10}  # 77 is far older than its "parent" 100
    table = [(101, 100, "python.exe"), (77, 100, "acrotray.exe")]
    assert proc_tree.descendants(100, table, born.get) == [(101, "python.exe")]


def test_an_id_reused_by_a_new_process_is_not_a_survivor(monkeypatch):
    monkeypatch.setattr(proc_tree, "process_table", lambda: [(11, 1, "b.exe")])
    assert proc_tree.survivors([(11, "b.exe", 500)], 0, lambda pid: 900) == []  # another process now
    assert proc_tree.survivors([(11, "b.exe", 500)], 0, lambda pid: 500) == [(11, "b.exe", 500)]


# --------------------------------------------------------------------------- #
# D6 / D9: native resume
# --------------------------------------------------------------------------- #


def resume_pair(tmp_path, second):
    factory = InMemoryStubFactory(["stream", second], thread_id="thread-r")
    adapter = make_adapter(tmp_path, factory)
    assert adapter.start_streaming("inv-1", snap(adapter), gate_token="t").ok
    result = adapter.start_streaming("inv-2", snap(adapter, "follow up", context_mode="native_resume",
                                                   resume_thread_id="thread-r"), gate_token="t")
    return adapter, factory, result


def test_resume_reuses_the_cwd_and_records_the_one_replay(tmp_path):
    adapter, factory, result = resume_pair(tmp_path, "stream")
    assert result.ok
    resume = next(m for m in factory.servers[1].received if m.get("method") == "thread/resume")
    assert resume["params"]["cwd"] == str(tmp_path / "runs" / "inv-1")
    payload = payload_of(adapter, "inv-2")
    assert payload["workdir"] == str(tmp_path / "runs" / "inv-1")
    assert payload["previous_turn_id"] == "stub-turn-0001"
    assert payload["replayed_usage"] == REPLAY_USAGE and payload["usage_anomalies"] == 0
    assert payload["usage"] != REPLAY_USAGE  # the replay never counts as this turn's usage
    assert not (tmp_path / "runs" / "inv-2").exists()


@pytest.mark.parametrize("scenario, replayed", [
    ("replay-twice", REPLAY_USAGE),
    ("replay-other-turn", None),
    ("replay-after-start", None),
])
def test_unexpected_replays_are_anomalies_not_usage(tmp_path, scenario, replayed):
    adapter, _factory, result = resume_pair(tmp_path, scenario)
    assert result.ok
    payload = payload_of(adapter, "inv-2")
    assert payload["replayed_usage"] == replayed
    assert payload["usage_anomalies"] == 1 and payload["usage"] != REPLAY_USAGE


def test_resume_without_the_previous_turn_is_refused_before_any_turn(tmp_path):
    adapter, factory, result = resume_pair(tmp_path, "resume-no-history")
    assert result.ok is False and result.terminal is True
    assert "history not confirmed" in result.detail
    assert "turn/start" not in factory.servers[1].received_methods()


def test_resume_without_a_recorded_work_directory_launches_nothing(tmp_path):
    factory = InMemoryStubFactory("stream", thread_id="thread-r")
    adapter = make_adapter(tmp_path, factory)
    unknown = snap(adapter, context_mode="native_resume", resume_thread_id="thread-unseen")
    result = adapter.start_streaming("inv-1", unknown, gate_token="t")
    assert result.ok is False and "no recorded work directory" in result.detail
    assert adapter.start_count == 0 and factory.servers == []

    assert adapter.start_streaming("inv-2", snap(adapter), gate_token="t").ok
    os.rmdir(tmp_path / "runs" / "inv-2")  # the thread's directory is gone
    gone = adapter.start_streaming("inv-3", snap(adapter, context_mode="native_resume",
                                                 resume_thread_id="thread-r"), gate_token="t")
    assert gone.ok is False and "no recorded work directory" in gone.detail
    assert len(factory.servers) == 1


# --------------------------------------------------------------------------- #
# wording (§4.10, §4.11)
# --------------------------------------------------------------------------- #


def test_wording_claims_nothing_unverified(tmp_path, pinned_file):
    _exe, ca = pinned_file
    adapter = real_adapter(tmp_path, real_factory(tmp_path, ca))
    caps = adapter.capabilities()
    assert not any(caps["verified"].values())
    for text in (SUPERVISED_REASON, REAL_APPSERVER_BLOCK_REASON):
        assert "支持" not in text and "已禁用" not in text
    assert "只能拒绝" in SUPERVISED_REASON and "CLI" in SUPERVISED_REASON
    info = describe(adapter)
    assert info["subagents_disabled"] is False and info["subagent_disable_requested"] is True
    assert info["binding_identity"].startswith("codex-app-server@codex-cli 0.163.0-alpha.11.2;sha256=")


def test_chat_page_hides_approve_unless_accept_is_offered():
    page = (REPO / "apps" / "api" / "agenthub_rt" / "static" / "chat.html").read_text(encoding="utf-8")
    assert "pending && canApprove ?" in page and "function acceptOffered(card)" in page
    assert "这一阶段只能拒绝" in page and "not_yet_acknowledged" in page
