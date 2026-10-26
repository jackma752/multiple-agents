"""P4.6 retries and durable quota waits. All adapters are offline; time is injected."""
import datetime as dt

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from agenthub_adapters.claude_code import result_object, scripted
from agenthub_adapters.quota import quota_error
from agenthub_kernel.autopilot import Autopilot
from agenthub_kernel.db import ApprovalRow, InvocationRow, ProviderQuotaRow, StageRunRow
from tests.http.test_autopilot import (
    USER, build, make_hub, conv, prove, start, stage_of, run_stage, save_plan, launched,
)
from tests.http.test_async_and_tool_approval import eventually


def script(client, outcomes):
    """Replace only the in-process fixture script after manual proof, recording test inputs."""
    prompts = []
    results = iter(outcomes)

    def run(argv, env, prompt):
        prompts.append(prompt)
        obj = next(results)
        return scripted(obj, exit_code=1 if obj.get("is_error") else 0)(argv, env, prompt)

    client.app.state.worker.adapter.for_agent("claude-code").runner.script = run
    return prompts


def fail(text="temporary upstream failure", **fields):
    return {**result_object(reply=text, is_error=True, subtype="error_during_execution"), **fields}


def pulse(client):
    client.app.state.autopilot.tick()
    assert client.app.state.worker.wait_idle(10)


def fake_clock(client):
    clock = [dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)]
    client.app.state.autopilot.clock = lambda: clock[0]
    return clock


def cc_stage(client, cid, **kw):
    response = start(client, cid, main_agent="claude-code", agents=["claude-code"], **kw)
    assert response.status_code == 200, response.text
    return response.json()["stage_id"]


def test_server_background_loop_finishes_without_manual_tick(make_hub):
    client, _ = make_hub(autopilot_interval=0.02)
    cid = conv(client)
    prove(client, cid, agents=("claude-code",))
    cc_stage(client, cid)
    eventually(lambda: stage_of(client, cid)["status"] == "completed")
    assert stage_of(client, cid)["calls_used"] == 1


def test_latest_stage_uses_creation_order_when_timestamps_tie(make_hub, monkeypatch):
    from agenthub_kernel import autopilot

    client, _ = make_hub()
    cid = conv(client)
    prove(client, cid, agents=("claude-code",))
    ids = iter(("stage_z_first", "stage_a_second"))
    monkeypatch.setattr(autopilot, "new_id", lambda prefix: next(ids))
    first = cc_stage(client, cid)
    assert run_stage(client, cid) == "completed"
    second = cc_stage(client, cid)
    with client.app.state.db.session() as session:
        for sid in (first, second):
            session.get(StageRunRow, sid).created_at = dt.datetime(2026, 10, 26)
    assert stage_of(client, cid)["stage_id"] == second
    assert run_stage(client, cid) == "completed"


@pytest.mark.parametrize("failures", [2, 4])
def test_retries_have_failure_context_and_keep_count_after_scheduler_restart(make_hub, failures):
    client, _ = make_hub()
    cid = conv(client)
    prove(client, cid, agents=("claude-code",))
    prompts = script(client, [fail() for _ in range(failures)] + [result_object(reply="fixed")])
    cc_stage(client, cid)
    pulse(client)
    pulse(client)  # records first repair
    client.app.state.autopilot = Autopilot(client.app.state.db, client.app.state.worker)
    assert run_stage(client, cid) == ("completed" if failures == 2 else "blocked")
    stage = stage_of(client, cid)
    assert stage["calls_used"] == (3 if failures == 2 else 4)
    assert stage["checkpoint"]["retry_counts"]["main"] == min(failures, 3)
    assert len(set(prompts)) == len(prompts)
    assert "Previous failure" in prompts[1] and "different approach" in prompts[1]
    if failures == 4:
        assert stage["summary"]["reason"] == "step_failed"
        assert stage["summary"]["failures"]["main"][0]["invocation_id"]


def test_review_failure_runs_a_repair_then_reviews_the_revised_result(make_hub):
    client, _ = make_hub()
    cid = conv(client)
    prove(client, cid, agents=("claude-code",))
    prompts = script(client, [result_object(reply="initial"), result_object(reply="VERDICT: FAIL\nmissing validation"),
                              result_object(reply="revised with validation"), result_object(reply="VERDICT: PASS")])
    cc_stage(client, cid, review_agent="claude-code")
    assert run_stage(client, cid) == "completed"
    stage = stage_of(client, cid)
    assert [s["step"] for s in stage["steps"]] == ["main", "review", "repair:1", "review:1"]
    assert "missing validation" in prompts[2]
    assert "revised with validation" in prompts[3]
    assert stage["summary"]["retry_counts"] == {"review_cycle": 1}


def test_failure_after_quota_recovery_switches_back_to_repair(make_hub):
    client, _ = make_hub()
    cid = conv(client)
    prove(client, cid, agents=("claude-code",))
    clock = fake_clock(client)
    prompts = script(client, [fail("quota exceeded"), fail("temporary upstream failure"), result_object()])
    cc_stage(client, cid)
    assert run_stage(client, cid) == "waiting_quota"
    clock[0] += dt.timedelta(minutes=6)
    assert run_stage(client, cid) == "completed"
    assert "Continue the interrupted stage step" in prompts[1]
    assert "Repair attempt 1 of 3" in prompts[2]
    assert "Continue the interrupted stage step" not in prompts[2]
    assert stage_of(client, cid)["summary"]["retry_counts"] == {"main": 1}


def test_scheduler_error_rolls_back_only_its_stage_and_records_a_blocker(make_hub, monkeypatch):
    client, _ = make_hub()
    cid = conv(client)
    prove(client, cid, agents=("claude-code",))
    sid = cc_stage(client, cid)
    cid2 = conv(client)
    cc_stage(client, cid2)
    scheduler = client.app.state.autopilot
    advance = scheduler._advance

    def broken(session, stage):
        if stage.id == sid:
            advance(session, stage)  # creates an approval inside the transaction
            raise RuntimeError("private diagnostic must not enter summary")
        return advance(session, stage)

    monkeypatch.setattr(scheduler, "_advance", broken)
    assert run_stage(client, cid2) == "completed"
    stage = stage_of(client, cid)
    assert stage["status"] == "blocked" and stage["calls_used"] == 0
    assert stage["summary"]["reason"] == "scheduler_error"
    assert stage["summary"]["message"] == "RuntimeError"


def test_quota_wait_uses_reset_time_does_not_use_repair_budget_and_survives_restart(tmp_path):
    app, _ = build(tmp_path)
    with TestClient(app) as client:
        cid = conv(client)
        prove(client, cid, agents=("claude-code",))
        clock = fake_clock(client)
        reset = clock[0] + dt.timedelta(hours=30)  # exceeds both old manual and stage TTLs
        script(client, [fail("usage limit reached", reset_at=reset.isoformat() + "Z")])
        cc_stage(client, cid)
        assert run_stage(client, cid) == "waiting_quota"
        assert stage_of(client, cid)["checkpoint"].get("retry_counts", {}) == {}
        before = launched(client)
        pulse(client)
        assert launched(client) == before
    app.state.db.engine.dispose()

    app2, _ = build(tmp_path)
    with TestClient(app2) as client:
        client.app.state.worker.recover()
        client.app.state.autopilot.clock = lambda: reset + dt.timedelta(seconds=1)
        prompts = script(client, [result_object(reply="continued")])
        assert run_stage(client, cid) == "completed"
        assert len(prompts) == 1 and "Continue the interrupted stage step" in prompts[0]
        assert stage_of(client, cid)["calls_used"] == 2
        with app2.state.db.session() as session:
            assert session.get(ProviderQuotaRow, "claude-code") is None


def test_unknown_reset_backs_off_and_one_probe_is_shared_by_stages(make_hub):
    client, _ = make_hub()
    cid = conv(client)
    prove(client, cid, agents=("claude-code",))
    prompts = script(client, [fail("rate_limit_error"), fail("rate_limit_error"), result_object(), result_object()])
    cc_stage(client, cid)
    clock = fake_clock(client)
    assert run_stage(client, cid) == "waiting_quota"
    cid2 = conv(client)
    cc_stage(client, cid2)
    pulse(client)
    assert stage_of(client, cid2)["status"] == "waiting_quota"
    assert len(prompts) == 1
    clock[0] += dt.timedelta(seconds=301)
    pulse(client)  # first pass prepares the original retry; second stage gets the probe lease
    pulse(client)
    assert len(prompts) == 2
    with client.app.state.db.session() as s:
        row = s.get(ProviderQuotaRow, "claude-code")
        assert row.backoff_seconds == 600 and row.reset_at is None
    clock[0] += dt.timedelta(seconds=601)
    for _ in range(8):
        pulse(client)
    assert stage_of(client, cid)["status"] == stage_of(client, cid2)["status"] == "completed"
    assert len(prompts) == 4


def test_quota_backoff_uses_the_authorized_interval(make_hub):
    client, _ = make_hub()
    cid = conv(client)
    prove(client, cid, agents=("claude-code",))
    clock = fake_clock(client)
    script(client, [fail("quota exceeded"), result_object()])
    assert start(client, cid, quota_retry_seconds=0).status_code == 422
    cc_stage(client, cid, quota_retry_seconds=60)
    assert run_stage(client, cid) == "waiting_quota"
    with client.app.state.db.session() as s:
        row = s.get(ProviderQuotaRow, "claude-code")
        assert row.next_check_at == clock[0] + dt.timedelta(seconds=60)
    clock[0] += dt.timedelta(seconds=61)
    assert run_stage(client, cid) == "completed"


@pytest.mark.parametrize("action", ["pause", "stop"])
def test_user_action_wins_over_quota_wakeup(make_hub, action):
    client, _ = make_hub()
    cid = conv(client)
    prove(client, cid, agents=("claude-code",))
    clock = fake_clock(client)
    prompts = script(client, [fail("rate limit exceeded"), result_object()])
    sid = cc_stage(client, cid)
    assert run_stage(client, cid) == "waiting_quota"
    assert client.post(f"/api/stages/{sid}/{action}", headers=USER).status_code == 200
    clock[0] += dt.timedelta(hours=2)
    pulse(client)
    assert len(prompts) == 1
    if action == "pause":
        assert client.post(f"/api/stages/{sid}/resume", headers=USER).status_code == 200
        assert run_stage(client, cid) == "completed"
    else:
        assert stage_of(client, cid)["status"] == "stopped"


def test_independent_item_runs_while_provider_waits_and_completed_items_are_not_repeated(make_hub):
    client, factory = make_hub()
    cid = conv(client)
    prove(client, cid)
    clock = fake_clock(client)
    save_plan(client, cid, [{"agent_id": "claude-code", "goal": "work A"},
                           {"agent_id": "codex", "goal": "independent B"}])
    root = client.get(f"/api/conversations/{cid}/messages", headers=USER).json()["root_task_id"]
    assert client.patch(f"/api/tasks/{root}/policy", headers=USER,
                        json={"max_subagents_total": 8}).status_code == 200
    script(client, [fail("quota exceeded"), result_object(reply="A"), result_object(reply="collected")])
    assert start(client, cid, plan="current", collect_agent="claude-code").status_code == 200
    assert run_stage(client, cid) == "waiting_quota"
    plan = client.get(f"/api/conversations/{cid}/plans", headers=USER).json()["plans"][0]
    assert plan["items"][1]["state"] == "done"
    codex_runs = len(factory.servers)
    clock[0] += dt.timedelta(minutes=6)
    assert run_stage(client, cid) == "completed"
    assert len(factory.servers) == codex_runs


def test_unknown_execution_is_never_automatically_retried(make_hub):
    client, _ = make_hub()
    cid = conv(client)
    prove(client, cid, agents=("claude-code",))
    sid = cc_stage(client, cid)
    inv_id = client.app.state.autopilot._advance_one(sid)[0]
    with client.app.state.db.session() as s:
        s.get(InvocationRow, inv_id).status = "unknown"
        s.scalar(select(ApprovalRow).where(ApprovalRow.invocation_id == inv_id)).status = "consumed"
    pulse(client)
    stage = stage_of(client, cid)
    assert stage["status"] == "blocked" and stage["summary"]["reason"] == "status_unknown"
    assert stage["calls_used"] == 1


def test_manual_quota_resume_and_authorization_tamper_gate(make_hub):
    client, _ = make_hub()
    cid = conv(client)
    prove(client, cid, agents=("claude-code",))
    clock = fake_clock(client)
    prompts = script(client, [fail("quota exceeded"), result_object()])
    sid = cc_stage(client, cid, auto_resume=False)
    assert run_stage(client, cid) == "waiting_quota"
    clock[0] += dt.timedelta(hours=2)
    pulse(client)
    assert len(prompts) == 1
    client.post(f"/api/stages/{sid}/resume", headers=USER)
    assert run_stage(client, cid) == "completed"

    sid = cc_stage(client, cid)
    inv_id = client.app.state.autopilot._advance_one(sid)[0]
    with client.app.state.db.session() as s:
        row = s.get(StageRunRow, sid)
        row.rules = {**row.rules, "max_calls": 50}
    before = launched(client)
    response = client.post(f"/api/invocations/{inv_id}/run", params={"wait": "true"}, headers=USER)
    assert response.status_code == 409 and response.json()["detail"]["code"] == "stage_rule_mismatch"
    assert launched(client) == before


def test_codex_quota_resume_binds_the_failed_calls_own_thread(make_hub):
    client, factory = make_hub(scenario=["happy", "turn-failed", "happy"])
    cid = conv(client)
    prove(client, cid, agents=("codex",))
    clock = fake_clock(client)
    assert start(client, cid, agents=["codex"]).status_code == 200
    pulse(client)
    stage = stage_of(client, cid)
    inv_id = stage["steps"][0]["invocation_id"]
    adapter = client.app.state.worker.adapter.for_agent("codex")
    record = adapter.lookup_by_invocation(inv_id)
    payload = record["payload"]
    payload["provider_quota"] = {"kind": "provider_quota", "reset_at": None}
    adapter.store.set_payload(inv_id, payload)
    assert run_stage(client, cid) == "waiting_quota"
    clock[0] += dt.timedelta(minutes=6)
    assert run_stage(client, cid) == "completed"
    last_id = stage_of(client, cid)["steps"][-1]["invocation_id"]
    with client.app.state.db.session() as s:
        snap = s.get(InvocationRow, last_id).snapshot
        assert snap["context_mode"] == "native_resume"
        assert snap["resume_thread_id"] == payload["native_session"]["thread_id"]
    assert len(factory.servers) == 3


@pytest.mark.parametrize("error, expected", [
    ({"code": 429, "retry_after": 10}, True), ({"message": "rate_limit_error"}, True),
    ({"message": "safeguards flagged this message"}, False),
    ({"message": "maximum context length exceeded"}, False), ({"code": "authentication_error"}, False),
])
def test_only_explicit_quota_evidence_becomes_a_wait(error, expected):
    assert bool(quota_error(error)) is expected


def test_probe_stranded_by_a_revoked_approval_does_not_pin_the_bucket(make_hub):
    """A probe whose approval was revoked never ran, so it must not lease the bucket forever.

    Reproduces the P4.6d blocker: a policy update revoked the probe's approval, the
    invocation stayed ``draft``, ``probe_invocation_id`` was never cleared and every
    later stage waited for a call that could not start.
    """
    client, _ = make_hub()
    cid = conv(client)
    prove(client, cid, agents=("claude-code",))
    clock = fake_clock(client)
    reset = clock[0] + dt.timedelta(seconds=5)
    prompts = script(client, [fail("usage limit reached", reset_at=reset.isoformat() + "Z"),
                              result_object(reply="recovered")])
    cc_stage(client, cid)
    assert run_stage(client, cid) == "waiting_quota"

    with client.app.state.db.session() as s:
        row = s.get(ProviderQuotaRow, "claude-code")
        failed = s.scalar(select(InvocationRow).where(InvocationRow.status == "failed"))
        stranded = InvocationRow(id="inv-stranded-probe", task_id=failed.task_id,
                                 payload_hash=failed.payload_hash, snapshot=dict(failed.snapshot or {}),
                                 status="draft")
        s.add(stranded)
        s.flush()
        s.add(ApprovalRow(id="appr-stranded-probe", invocation_id=stranded.id,
                          payload_hash=failed.payload_hash, user_id="local-user", status="revoked"))
        row.probe_invocation_id = stranded.id
        row.next_check_at = clock[0] - dt.timedelta(seconds=1)

    assert run_stage(client, cid) == "completed"
    assert len(prompts) == 2 and "Continue the interrupted stage step" in prompts[1]
    with client.app.state.db.session() as s:
        assert s.get(ProviderQuotaRow, "claude-code") is None
        # The stranded invocation itself is never executed or rewritten by the scheduler.
        assert s.get(InvocationRow, "inv-stranded-probe").status == "draft"
