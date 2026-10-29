"""A call that died with the previous Hub process, an accidental tree stop, and continuing the research."""

from __future__ import annotations

from sqlalchemy import func, select

from agenthub_kernel.db import InvocationRow, TaskRow
from agenthub_kernel.service import PolicyService

from tests.papers.conftest import Hub


def _strand_synthesis(hub, task):
    """After reading, create the next model call and leave it as a crash would: running, adapter record running."""
    view = hub.run(task, until=lambda v: any(s["name"] == "extract" for s in v["steps"])
                   and all(s["status"] == "succeeded" for s in v["steps"] if s["name"] == "extract")
                   and v["phase"] == "synthesize")
    ap = hub.client.app.state.autopilot
    created = ap._advance_one(view["stage"]["stage_id"])  # the synthesis call, approved, not started
    assert len(created) == 1
    inv_id = created[0]
    with hub.client.app.state.db.session() as s:
        PolicyService(s).consume(inv_id)
        s.get(InvocationRow, inv_id).status = "running"
    store = hub.client.app.state.adapter.for_agent("claude-code").store
    store.record_start(inv_id, "")  # the child process was started, then the Hub went away
    return inv_id


def test_a_call_that_died_with_the_previous_hub_becomes_unknown_not_running(tmp_path):
    hub = Hub(tmp_path).open()
    try:
        hub.prove()
        task = hub.create(target=2)
        inv_id = _strand_synthesis(hub, task)
        calls = hub.model_calls()
        hub.restart()  # the lifespan is not run by TestClient here; recover() is what startup runs
        summary = hub.client.app.state.worker.recover()
        assert inv_id in summary["unknown"] and inv_id not in summary["confirmed_running"]
        with hub.client.app.state.db.session() as s:
            row = s.get(InvocationRow, inv_id)
            assert row.status == "unknown" and "restart" in (row.error or "")
        view = hub.run(task, passes=3)
        assert view["status"] == "blocked" and view["stage"]["summary"]["reason"] == "status_unknown"
        assert view["unknown_calls"] == [inv_id]
        assert hub.model_calls() == calls  # never relaunched
        # the user registers it as ended, then re-authorizes: finished steps are reused
        r = hub.api("POST", f"/api/invocations/{inv_id}/reconcile", json={"action": "settle", "outcome": "failed"})
        assert r.status_code == 200, r.text
        assert hub.api("POST", f"/api/papers/{task}/reauthorize", json={}).status_code == 200
        view = hub.run(task)
        assert view["status"] == "completed"
        assert all(len(s["invocation_ids"]) == 1 for s in view["steps"] if s["name"] == "extract")
    finally:
        hub.close()


def test_continue_a_research_whose_tree_was_stopped(tmp_path):
    hub = Hub(tmp_path).open()
    try:
        hub.prove()
        old = hub.create(target=2, max_calls=20)
        inv_id = _strand_synthesis(hub, old)
        # what the user did: stop the stage, then "停止本地进程" (stops the whole tree)
        hub.api("POST", f"/api/papers/{old}/stage/stop")
        assert hub.api("POST", f"/api/invocations/{inv_id}/stop-local").status_code == 200
        r = hub.api("POST", f"/api/papers/{old}/reauthorize", json={})
        assert r.status_code == 409 and r.json()["detail"]["code"] == "tree_stopped"
        before = hub.view(old)
        used = before["usage"]["calls"]
        evidence = len(hub.api("GET", f"/api/papers/{old}/evidence").json()["evidence"])
        calls = hub.model_calls()

        r = hub.api("POST", f"/api/papers/{old}/continue", json={"client_request_id": "cont-1"})
        assert r.status_code == 200, r.text
        new = r.json()["task_id"]
        again = hub.api("POST", f"/api/papers/{old}/continue", json={"client_request_id": "cont-1"})
        assert again.json()["task_id"] == new  # a repeated click creates nothing new
        view = hub.view(new)
        assert view["continued_from"] == old and hub.view(old)["continued_to"] == new
        assert view["phase"] == before["phase"] != "plan"  # resumes where the stopped task was
        # the remaining allowance only: the new tree cannot spend more than the old one had left
        assert view["stage"]["max_calls"] == 20 - before["stage"]["calls_used"]
        with hub.client.app.state.db.session() as s:
            root = s.get(TaskRow, view["root_task_id"])
            assert root.policy["max_agent_invocations"] == 20 - used
        assert len(hub.api("GET", f"/api/papers/{new}/evidence").json()["evidence"]) == evidence
        view = hub.run(new)
        assert view["status"] == "completed"
        # nothing before synthesis ran again
        assert [s for s in view["steps"] if s["name"] in ("plan", "screen", "extract")] and all(
            len(s["invocation_ids"]) == 1 for s in view["steps"] if s["name"] in ("plan", "screen", "extract"))
        new_calls = hub.model_calls() - calls
        assert 0 < new_calls <= view["stage"]["max_calls"]
        with hub.client.app.state.db.session() as s:
            assert s.scalar(select(func.count()).select_from(InvocationRow).where(
                InvocationRow.status == "unknown")) == 1  # the old stranded call stays recorded as it was
    finally:
        hub.close()


def test_continue_is_refused_while_the_old_research_can_still_resume(hub):
    hub.prove()
    task = hub.create(target=2)
    hub.run(task, passes=2, until=lambda v: False)
    r = hub.api("POST", f"/api/papers/{task}/continue", json={"client_request_id": "c"})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "not_stopped"
