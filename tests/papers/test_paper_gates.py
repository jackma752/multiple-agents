"""P6 authorization, budget and recovery gates: zero new executions when not allowed."""

from __future__ import annotations

from sqlalchemy import select

from agenthub_adapters.claude_code import result_object, scripted
from agenthub_kernel.db import InvocationRow
from agenthub_kernel.papers.db import PaperStepRow
from agenthub_kernel.papers.pipeline import run_tools
from tools.demo.paper_stub_model import respond

from tests.papers.conftest import Hub, waiting


def _stage_id(hub, task):
    return hub.view(task)["stage"]["stage_id"]


def test_research_needs_a_proven_agent_and_launches_nothing_otherwise(hub):
    r = hub.api("POST", "/api/papers", json=hub.body())
    assert r.status_code == 409 and r.json()["detail"]["code"] == "stage_not_proven"
    assert hub.launched() == 0 and hub.model_calls() == 0 and hub.fetcher.calls == []
    pre = hub.api("POST", "/api/papers/preflight", json=hub.body()).json()
    assert not pre["ok"] and {b["code"] for b in pre["blockers"]} == {"stage_not_proven"}


def test_agent_tokens_cannot_authorize_research(hub):
    for token in ("dev-agent-token", "dev-worker-token"):
        r = hub.client.post("/api/papers", headers={"Authorization": f"Bearer {token}"}, json=hub.body())
        assert r.status_code == 403
    assert hub.client.post("/api/papers", json=hub.body()).status_code == 401


def test_repeated_create_returns_the_same_task(hub):
    hub.prove()
    first, second = hub.create(), hub.create()
    assert first == second
    assert len(hub.api("GET", "/api/papers").json()["tasks"]) == 1


def test_stop_mid_pipeline_starts_no_new_call_or_tool(hub):
    hub.prove()
    task = hub.create()
    hub.client.app.state.autopilot.tick()
    hub.client.app.state.worker.wait_idle(10)
    assert hub.api("POST", f"/api/papers/{task}/stage/stop").status_code == 200
    calls, fetches, launched = hub.model_calls(), len(hub.fetcher.calls), hub.launched()
    for _ in range(5):
        hub.client.app.state.autopilot.tick()
        hub.client.app.state.worker.wait_idle(10)
    assert (hub.model_calls(), len(hub.fetcher.calls), hub.launched()) == (calls, fetches, launched)
    assert hub.view(task)["status"] == "stopped"


def test_budget_refusal_blocks_with_zero_launches(hub):
    hub.prove()
    launched, calls = hub.launched(), hub.model_calls()
    task = hub.create(token_budget_total=5)
    view = hub.run(task)
    assert view["status"] == "blocked"
    assert view["stage"]["summary"]["reason"] == "start_refused"
    assert hub.launched() == launched and hub.model_calls() == calls


def test_paused_stage_approval_is_refused_at_the_gate(hub):
    hub.prove()
    task = hub.create()
    ap, worker = hub.client.app.state.autopilot, hub.client.app.state.worker
    created = ap._advance_one(_stage_id(hub, task))  # the plan call, approved under the stage, not started
    assert len(created) == 1
    hub.api("POST", f"/api/papers/{task}/stage/pause")
    calls = hub.model_calls()
    result = worker.submit(created[0])
    assert not result["accepted"] and result["code"] == "stage_not_active"
    assert hub.model_calls() == calls


def test_tool_steps_do_not_run_while_paused(hub):
    hub.prove()
    task = hub.create()
    ap, worker = hub.client.app.state.autopilot, hub.client.app.state.worker
    stage_id = _stage_id(hub, task)
    for inv in ap._advance_one(stage_id):
        ap._start(stage_id, inv)
    worker.wait_idle(10)
    ap._advance_one(stage_id)  # plan done -> search steps written, not run
    with hub.client.app.state.db.session() as s:
        pending = s.scalars(select(PaperStepRow).where(PaperStepRow.kind == "tool")).all()
        assert pending and all(p.status == "pending" for p in pending)
    hub.api("POST", f"/api/papers/{task}/stage/pause")
    assert run_tools(ap, stage_id) == 0 and hub.fetcher.calls == []
    hub.api("POST", f"/api/papers/{task}/stage/resume")
    assert run_tools(ap, stage_id) >= 1 and hub.fetcher.calls


def test_unknown_call_blocks_and_is_never_replayed(tmp_path):
    def script(argv, env, prompt):
        if "research.extract.v1" in prompt:
            return scripted(None, exit_code=None, timed_out=True)(argv, env, prompt)
        return scripted(result_object(reply=respond(prompt)))(argv, env, prompt)

    hub = Hub(tmp_path, runner_script=script).open()
    try:
        hub.prove()
        task = hub.create(target=2)
        view = hub.run(task)
        assert view["status"] == "blocked" and view["stage"]["summary"]["reason"] == "status_unknown"
        with hub.client.app.state.db.session() as s:
            unknown = s.scalars(select(InvocationRow.id).where(InvocationRow.status == "unknown")).all()
        assert len(unknown) == 1
        calls = hub.model_calls()
        for _ in range(4):
            hub.client.app.state.autopilot.tick()
        assert hub.model_calls() == calls
        r = hub.api("POST", f"/api/papers/{task}/reauthorize", json={})
        assert r.status_code == 409 and r.json()["detail"]["code"] == "status_unknown"
    finally:
        hub.close()


def _extracted(view):
    return [s for s in view["steps"] if s["name"] == "extract" and s["status"] == "succeeded"]


def test_reauthorize_after_stop_reuses_finished_steps(hub):
    hub.prove()
    task = hub.create(target=2)
    hub.run(task, until=lambda v: len(_extracted(v)) == 2)
    assert hub.api("POST", f"/api/papers/{task}/stage/stop").status_code == 200
    assert hub.api("POST", f"/api/papers/{task}/reauthorize", json={"max_calls": 30}).status_code == 200
    view = hub.run(task)
    assert view["status"] == "completed"
    extracts = [s for s in view["steps"] if s["name"] == "extract"]
    assert len(extracts) == 2 and all(len(s["invocation_ids"]) == 1 for s in extracts)
    plan = [s for s in view["steps"] if s["name"] == "plan"]
    assert len(plan) == 1 and len(plan[0]["invocation_ids"]) == 1


def test_reauthorize_cannot_lower_the_tree_limit(hub):
    hub.prove()
    task = hub.create(target=2)
    hub.run(task, until=lambda v: len(_extracted(v)) >= 1)
    hub.api("POST", f"/api/papers/{task}/stage/stop")
    r = hub.api("POST", f"/api/papers/{task}/reauthorize", json={"max_agent_invocations": 2})
    assert r.status_code == 422


def test_workbench_page_is_served_and_linked_from_chat(hub):
    page = hub.client.get("/papers")
    assert page.status_code == 200 and "论文研究" in page.text and "/api/papers/preflight" in page.text
    assert 'href="/papers"' in hub.client.get("/chat").text
    assert hub.client.get("/api/papers").status_code == 401  # the page is static; data needs the user
    # same light/dark/system theme as the other pages, from the shared assets
    assert "/assets/display.css" in page.text and "data-theme-picker" in page.text
    assert hub.client.get("/assets/display.css").status_code == 200


def test_call_cap_defaults_to_the_estimate_and_unlimited_only_when_chosen(hub):
    hub.prove()
    default = hub.api("POST", "/api/papers/preflight", json=hub.body(max_calls=None)).json()
    assert default["ok"] and 0 < default["max_calls"] <= 50
    unlimited = hub.api("POST", "/api/papers/preflight", json=hub.body(max_calls=0)).json()
    assert unlimited["ok"] and unlimited["max_calls"] == 0 and any("不限" in w for w in unlimited["warnings"])
    task = hub.create(max_calls=None)
    assert hub.view(task)["stage"]["max_calls"] == default["max_calls"]  # never silently unlimited


def test_waiting_holds_no_running_slot(hub):
    hub.prove()
    task = hub.create()
    hub.run(task, until=waiting)
    with hub.client.app.state.db.session() as s:
        active = s.scalars(select(InvocationRow.id).where(InvocationRow.status.in_(("starting", "running")))).all()
    assert active == []
