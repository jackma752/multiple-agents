"""The Claude Code adapter behind the unchanged kernel gates (offline).

API -> approval -> Worker consume + quota reservation -> adapter -> settlement,
with the in-process FakeClaudeRunner (simulation; hand-written documented shape)
or a fake pinned file whose launch is blocked by a sentinel. No process of
Claude Code, model call or credential is involved.
"""

import datetime as dt
import hashlib
import json
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from agenthub_adapters import claude_code as cc
from agenthub_adapters.claude_code import (
    PINNED_CLAUDE_CODE, ClaudeCodeAdapter, ClaudeCodeLauncher, FakeClaudeRunner, StubClaudeLauncher,
    result_object, scripted,
)
from agenthub_adapters.store import ExecutionStore
from agenthub_kernel.db import ApprovalRow, Database, InvocationRow, QuotaReservationRow
from agenthub_rt.api import create_app

USER = {"Authorization": "Bearer dev-user-token"}
PROMPT = "PROMPT-91c2 please answer"
REPLY = "REPLY-7f3a"


def fake(tmp_path, script=None):
    return ClaudeCodeAdapter(
        launcher=StubClaudeLauncher("success-usage"), workdir_root=str(tmp_path / "runs"),
        store=ExecutionStore(str(tmp_path / "cc.db"), table="claude_code_executions"),
        runner=FakeClaudeRunner(script or scripted(result_object(reply=REPLY))))


@pytest.fixture
def make(tmp_path):
    clients = []

    def _make(adapter):
        app = create_app(db=Database(f"sqlite:///{(tmp_path / 'hub.db').as_posix()}"), adapter=adapter)
        client = TestClient(app)
        client.__enter__()
        clients.append(client)
        return client

    yield _make
    for client in clients:
        client.__exit__(None, None, None)
        client.app.state.db.engine.dispose()


def submit(client, mode="strict", key="one"):
    cid = client.post("/api/conversations", json={"title": "cc"}, headers=USER).json()["conversation_id"]
    r = client.post(f"/api/conversations/{cid}/invocations", headers=USER, json={
        "text": PROMPT, "agent_id": "claude-code", "client_request_id": key, "execution_mode": mode})
    assert r.status_code == 200, r.text
    return cid, r.json()


def approve(client, turn, consent=None):
    body = {"accept_unmanaged_native_agents": consent} if consent is not None else None
    return client.post(f"/api/approvals/{turn['approval_id']}/approve", headers=USER, json=body)


def run(client, turn):
    return client.post(f"/api/invocations/{turn['invocation_id']}/run", params={"wait": "true"}, headers=USER)


def held(client):
    with client.app.state.db.session() as s:
        return s.scalar(select(func.count()).select_from(QuotaReservationRow)
                        .where(QuotaReservationRow.released_at.is_(None)))


def status(client, turn):
    with client.app.state.db.session() as s:
        return s.get(InvocationRow, turn["invocation_id"]).status


def messages(client, cid):
    return client.get(f"/api/conversations/{cid}/messages", headers=USER).json()["messages"]


# --------------------------------------------------------------------------- #
# no approval -> zero launches
# --------------------------------------------------------------------------- #


def test_unapproved_call_launches_nothing(make, tmp_path):
    adapter = fake(tmp_path)
    client = make(adapter)
    _cid, turn = submit(client)
    assert run(client, turn).status_code == 409
    assert adapter.start_count == 0 and adapter.runner.calls == [] and held(client) == 0


@pytest.mark.parametrize("action", ["reject", "revoke", "expire"])
def test_rejected_revoked_or_expired_launches_nothing(make, tmp_path, action):
    adapter = fake(tmp_path)
    client = make(adapter)
    _cid, turn = submit(client)
    if action == "expire":
        assert approve(client, turn).status_code == 200
        with client.app.state.db.session() as s:
            s.get(ApprovalRow, turn["approval_id"]).expires_at = (
                dt.datetime.now(dt.timezone.utc).replace(tzinfo=None) - dt.timedelta(seconds=1))
            s.commit()
    else:
        if action == "revoke":
            assert approve(client, turn).status_code == 200
        r = client.post(f"/api/approvals/{turn['approval_id']}/{action}", headers=USER)
        assert r.status_code == 200, r.text
    assert run(client, turn).status_code == 409
    assert adapter.start_count == 0 and adapter.runner.calls == [] and held(client) == 0


def test_concurrent_runs_launch_at_most_once(make, tmp_path):
    adapter = fake(tmp_path)
    client = make(adapter)
    _cid, turn = submit(client)
    assert approve(client, turn).status_code == 200
    barrier = threading.Barrier(4)

    def go(_):
        barrier.wait(timeout=10)
        return run(client, turn).status_code

    with ThreadPoolExecutor(max_workers=4) as pool:
        codes = sorted(pool.map(go, range(4)))
    assert codes == [200, 409, 409, 409]
    assert adapter.start_count == 1 and len(adapter.runner.calls) == 1


# --------------------------------------------------------------------------- #
# admission and consent
# --------------------------------------------------------------------------- #


@pytest.fixture
def real(tmp_path, monkeypatch):
    exe = tmp_path / "bin" / "claude.exe"
    exe.parent.mkdir()
    exe.write_bytes(b"not a real claude binary")
    monkeypatch.setitem(PINNED_CLAUDE_CODE, "path", str(exe))
    monkeypatch.setitem(PINNED_CLAUDE_CODE, "sha256", hashlib.sha256(exe.read_bytes()).hexdigest())
    monkeypatch.setitem(PINNED_CLAUDE_CODE, "size", exe.stat().st_size)
    ca = tmp_path / "ca.pem"
    ca.write_text("-----BEGIN CERTIFICATE-----\n")
    launches = []

    def sentinel(argv, **kwargs):
        launches.append(list(argv))
        raise OSError("sentinel: launch blocked in tests")

    monkeypatch.setattr(subprocess, "Popen", sentinel)
    adapter = ClaudeCodeAdapter(
        launcher=ClaudeCodeLauncher.pinned(config_dir=str(tmp_path / "cfg"), proxy="http://127.0.0.1:8090",
                                           ca_bundle=str(ca)),
        workdir_root=str(tmp_path / "runs"),
        store=ExecutionStore(str(tmp_path / "cc.db"), table="claude_code_executions"))
    return adapter, launches


def test_strict_refuses_the_real_launcher_before_consuming(make, real, monkeypatch):
    monkeypatch.setattr(cc, "ZERO_MODEL_IDENTITY_REPORT", "0" * 64)
    adapter, launches = real
    client = make(adapter)
    _cid, turn = submit(client, "strict")
    assert approve(client, turn).status_code == 200
    r = run(client, turn)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "claude_code_controls_unverified"
    with client.app.state.db.session() as s:
        assert s.get(ApprovalRow, turn["approval_id"]).status == "approved"  # not consumed
    assert launches == [] and adapter.start_count == 0 and held(client) == 0


def test_supervised_real_is_refused_while_identity_is_unconfirmed(make, real):
    adapter, launches = real
    client = make(adapter)
    _cid, turn = submit(client, "supervised")
    assert approve(client, turn, consent=True).status_code == 200
    r = run(client, turn)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "claude_code_supervised_conditions"
    assert "identity_unconfirmed" in r.json()["detail"]["error"]
    assert launches == [] and adapter.start_count == 0


def test_supervised_needs_this_calls_explicit_consent(make, real, monkeypatch):
    monkeypatch.setattr(cc, "ZERO_MODEL_IDENTITY_REPORT", "0" * 64)
    adapter, launches = real
    client = make(adapter)
    _cid, turn = submit(client, "supervised")
    assert approve(client, turn).status_code == 409  # boundary_consent_required
    assert approve(client, turn, consent="true").status_code in (409, 422)  # a string is not consent
    assert run(client, turn).status_code == 409 and launches == []
    assert approve(client, turn, consent=True).status_code == 200
    r = run(client, turn)
    assert r.status_code == 200
    # The sentinel refused the launch: provably nothing ran, so the call is failed and freed.
    assert status(client, turn) == "failed" and held(client) == 0
    assert launches == [[PINNED_CLAUDE_CODE["path"], *cc.CLAUDE_ARGV_TAIL]]


# --------------------------------------------------------------------------- #
# settlement
# --------------------------------------------------------------------------- #


def test_success_settles_releases_and_saves_the_reply(make, tmp_path):
    adapter = fake(tmp_path)
    client = make(adapter)
    cid, turn = submit(client)
    approve(client, turn)
    assert run(client, turn).json()["status"] == "succeeded"
    assert held(client) == 0
    reply = messages(client, cid)[-1]
    assert reply["role"] == "assistant" and reply["text"] == REPLY
    assert reply["result"]["usage"]["output_tokens"] == 3


@pytest.mark.parametrize("script, expected, slot", [
    (scripted(result_object(is_error=True, reply="Not logged in"), exit_code=1), "failed", 0),
    (scripted(result_object(subtype="error_max_turns", is_error=True, reply=None), exit_code=1), "failed", 0),
    (scripted(None, exit_code=1), "failed", 0),
    (scripted(text="garbage", exit_code=0), "unknown", 1),
    (scripted(result_object(reply=REPLY), exit_code=2), "unknown", 1),
    (scripted(result_object(reply=REPLY), exit_code=None, timed_out=True), "unknown", 1),
])
def test_failure_paths(make, tmp_path, script, expected, slot):
    adapter = fake(tmp_path, script)
    client = make(adapter)
    cid, turn = submit(client)
    approve(client, turn)
    run(client, turn)
    assert status(client, turn) == expected and held(client) == slot
    assert [m["role"] for m in messages(client, cid)] == ["user"]  # no invented reply


def test_no_prompt_or_reply_text_in_events_or_kernel_rows(make, tmp_path):
    adapter = fake(tmp_path)
    client = make(adapter)
    cid, turn = submit(client)
    approve(client, turn)
    run(client, turn)
    events = client.get(f"/api/conversations/{cid}/events", headers=USER).json()["events"]
    blob = json.dumps(events, ensure_ascii=False)
    assert PROMPT not in blob and REPLY not in blob
    with client.app.state.db.session() as s:
        row = s.get(InvocationRow, turn["invocation_id"])
        assert REPLY not in (row.error or "")
    stored = json.dumps(adapter.store.get(turn["invocation_id"]), ensure_ascii=False)
    assert PROMPT not in stored  # the reply lives only in final_message, like the other adapters


def test_build_adapter_selects_stub_or_pinned_constant(monkeypatch, tmp_path):
    from agenthub_kernel import config
    from agenthub_kernel.adapters import build_adapter, describe

    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AGENT_HUB_CLAUDE_CODE_WORKDIR", str(tmp_path / "runs"))
    monkeypatch.setenv("AGENT_HUB_CLAUDE_CODE_STUB", "success-usage")
    stub = build_adapter("claude-code")
    assert type(stub.launcher) is StubClaudeLauncher and stub.binding_identity() == "claude-code-stub:stdio"
    monkeypatch.delenv("AGENT_HUB_CLAUDE_CODE_STUB")
    real_one = build_adapter("claude-code")
    assert type(real_one.launcher) is ClaudeCodeLauncher
    assert real_one.launcher.path == PINNED_CLAUDE_CODE["path"]
    info = describe(real_one)
    assert info["kind"] == "claude-code" and info["agent_ids"] == ["claude-code"]
    assert info["binding_identity"].startswith("claude-code@2.1.308;sha256=c9796ba2")
    assert info["execution_admission"]["allowed"] is False
    assert info["supervised_admission"]["allowed"] is False
