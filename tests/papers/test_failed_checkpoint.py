"""Real API/consume path, offline models: completed failures keep paid checkpoints."""
import copy

import pytest
from sqlalchemy import func, select

from agenthub_kernel.db import InvocationRow, StageRunRow, TaskRow, TreeQuotaRow, UsageLedgerRow
from agenthub_kernel.papers.db import PaperStepRow, ResearchTaskRow
from tests.papers.test_guidance import body


def failed(hub, monkeypatch, *, kind="study", target=2, core=1, citation=False, mode="strict", fail_one=False):
    from tools.demo import guidance_stub

    original = guidance_stub.respond
    active, seen, first = [True], [], []

    def response(template, data):
        seen.append((template, copy.deepcopy(data)))
        out = original(template, data)
        if active[0] and template == f"research.{kind}.{'v2' if kind == 'study' else 'v1'}":
            if fail_one:
                pid = data["paper"]["paper_id"]
                if not first:
                    first.append(pid)
                if pid != first[0]:
                    return out
            if citation:
                out["units"][0]["sources"][0]["quote"] = "Authored absent quotation for a bounded repair."
            else:
                return {"units": []} if kind == "study" else {"comparison": "failed", "candidates": []}
        if active[0] and citation and template == "research.source-repair.v1":
            return {"replacements": []}
        return out

    monkeypatch.setattr(guidance_stub, "respond", response)
    hub.prove()
    if mode == "supervised":
        cid = hub.api("POST", "/api/conversations", json={}).json()["conversation_id"]
        for agent in ("claude-code", "deepseek"):
            turn = hub.api("POST", f"/api/conversations/{cid}/invocations", json={
                "text": "hello", "agent_id": agent, "client_request_id": f"prove-supervised-{agent}",
                "execution_mode": "supervised"}).json()
            assert hub.api("POST", f"/api/approvals/{turn['approval_id']}/approve",
                           json={"accept_unmanaged_native_agents": True}).status_code == 200
            r = hub.api("POST", f"/api/invocations/{turn['invocation_id']}/run", params={"wait": "true"})
            assert r.json()["status"] == "succeeded", r.text
    data = body(hub, "proposal")
    data["request"].update(target_papers=target, core_papers=core)
    data.update(execution_mode=mode, supervised_consent=mode == "supervised")
    r = hub.api("POST", "/api/papers", json=data)
    assert r.status_code == 200, r.text
    tid = r.json()["task_id"]
    v = hub.run(tid)
    if v["status"] == "waiting_input":
        assert hub.choose(tid, "continue").status_code == 200
        v = hub.run(tid)
    assert v["phase"] == "done" and v["retry_checkpoint"]
    active[0] = False
    return tid, v, seen


def test_done_failed_study_continues_without_repeating_finished_work(hub, monkeypatch):
    old, before, seen = failed(hub, monkeypatch)
    calls, downloads = hub.model_calls(), list(hub.fetcher.calls)
    count = len(seen)
    artifacts = {a["name"]: a["sha256"] for a in before["artifacts"]}
    checkpoint = before["retry_checkpoint"]
    assert checkpoint["phase"] == "read" and len(checkpoint["paper_ids"]) == 1
    assert checkpoint["remaining_calls"] == 40 - before["stage"]["calls_used"]
    r = hub.api("POST", f"/api/papers/{old}/continue", json={})
    assert r.status_code == 200, r.text
    new = r.json()["task_id"]
    assert r.json()["phase"] == "read" and r.json()["usage"] == before["usage"]
    again = hub.api("POST", f"/api/papers/{old}/continue", json={})
    assert again.json()["task_id"] == new
    with hub.client.app.state.db.session() as s:
        row = s.get(ResearchTaskRow, new)
        stage = s.get(StageRunRow, row.stage_id)
        assert stage.id != before["stage"]["stage_id"] and stage.checkpoint["bindings"]
        assert s.get(TaskRow, row.root_task_id).policy["max_agent_invocations"] == 40 - before["usage"]["calls"]
    v = hub.run(new)
    assert v["status"] == "completed" and v["quality_status"] == "passed"
    assert v["usage"]["calls"] == before["usage"]["calls"] + hub.model_calls() - calls
    assert hub.fetcher.calls == downloads
    assert not any(t in ("research.plan.v1", "research.screen.v1", "research.extract.v1") for t, _ in seen[count:])
    assert sum(t == "research.study.v2" for t, _ in seen[count:]) == 1
    original = hub.view(old)
    assert original["quality_status"] == before["quality_status"]
    assert original["steps"] == before["steps"] and original["outcome"] == before["outcome"]
    assert {a["name"]: a["sha256"] for a in original["artifacts"]} == artifacts


def test_saved_citation_seed_only_repairs_failed_reference(hub, monkeypatch):
    old, before, seen = failed(hub, monkeypatch, citation=True)
    count = len(seen)
    seed = next(d for t, d in seen if t == "research.study.v2")
    r = hub.api("POST", f"/api/papers/{old}/continue", json={})
    assert r.status_code == 200, r.text
    v = hub.run(r.json()["task_id"])
    assert v["quality_status"] == "passed"
    templates = [t for t, _ in seen[count:]]
    assert "research.study.v2" not in templates
    repair = next(d for t, d in seen[count:] if t == "research.source-repair.v1")
    assert [u["id"] for u in repair["units"]] == ["problem1"]
    assert {i["unit_id"] for i in repair["issues"]} == {"problem1"}
    assert all(c["text"] in [c["text"] for c in seed["chunks"]] for c in repair["chunks"])
    study = next(s for s in v["outcome"]["studies"].values() if s["units"])
    assert study["units"][0]["text"] == next(d for t, d in seen if t == "research.source-repair.v1")["units"][0]["text"]
    assert hub.view(old)["steps"] == before["steps"]


def test_failed_directions_reuses_all_successful_teaching(hub, monkeypatch):
    old, before, seen = failed(hub, monkeypatch, kind="directions")
    assert before["retry_checkpoint"]["phase"] == "directions"
    count = len(seen)
    r = hub.api("POST", f"/api/papers/{old}/continue", json={})
    assert r.status_code == 200, r.text
    v = hub.run(r.json()["task_id"])
    assert v["quality_status"] == "passed"
    assert [t for t, _ in seen[count:]] == ["research.directions.v1"]


def test_successful_core_is_preserved_when_another_core_failed(hub, monkeypatch):
    old, before, seen = failed(hub, monkeypatch, target=3, core=2, fail_one=True)
    success = next(s for s in before["outcome"]["studies"].values() if s["units"])
    count = len(seen)
    r = hub.api("POST", f"/api/papers/{old}/continue", json={})
    assert r.status_code == 200, r.text
    v = hub.run(r.json()["task_id"])
    assert v["status"] == "completed" and v["quality_status"] == "passed"
    preserved = next(s for s in v["outcome"]["studies"].values() if s["title"] == success["title"])
    assert [u["text"] for u in preserved["units"]] == [u["text"] for u in success["units"]]
    retried = [d for t, d in seen[count:] if t == "research.study.v2"]
    assert len(retried) == 1 and retried[0]["paper"]["title"] != success["title"]


@pytest.mark.parametrize("condition,code", [
    ("active", "stage_in_progress"), ("running", "call_in_flight"), ("unknown", "status_unknown"),
    ("calls", "allowance_used_up"), ("tokens", "allowance_used_up"),
])
def test_failed_checkpoint_refusals_start_nothing(hub, monkeypatch, condition, code):
    old, before, _ = failed(hub, monkeypatch)
    with hub.client.app.state.db.session() as s:
        row = s.get(ResearchTaskRow, old)
        root = s.get(TaskRow, row.root_task_id)
        if condition == "active":
            s.get(StageRunRow, row.stage_id).status = "active"
        elif condition in ("unknown", "running"):
            inv_id = next(i for st in before["steps"] if st["name"] == "study" for i in st["invocation_ids"])
            s.get(InvocationRow, inv_id).status = condition
        elif condition == "calls":
            s.get(TreeQuotaRow, root.id).total_invocations = root.policy["max_agent_invocations"]
        else:
            used = s.scalar(select(func.coalesce(func.sum(UsageLedgerRow.input_total), 0) +
                                   func.coalesce(func.sum(UsageLedgerRow.output_total), 0))
                            .where(UsageLedgerRow.root_task_id == root.id))
            root.policy = {**root.policy, "token_budget_total": int(used) + 1, "token_budget_per_call": 2}
    calls = hub.model_calls()
    r = hub.api("POST", f"/api/papers/{old}/continue", json={})
    assert r.status_code == 409 and r.json()["detail"]["code"] == code, r.text
    assert hub.model_calls() == calls and hub.view(old)["continued_to"] is None


def test_continue_requires_login_approver_and_supervised_boundary(hub, monkeypatch):
    old, _, _ = failed(hub, monkeypatch, mode="supervised")
    calls = hub.model_calls()
    path = f"/api/papers/{old}/continue"
    assert hub.client.post(path, json={}).status_code == 401
    assert hub.client.post(path, json={}, headers={"Authorization": "Bearer dev-agent-token"}).status_code == 403
    r = hub.api("POST", path, json={})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "boundary_consent_required"
    assert hub.model_calls() == calls and hub.view(old)["continued_to"] is None
    assert hub.api("POST", path, json={"supervised_consent": True}).status_code == 200


def test_successful_generation_does_not_offer_rerun(hub):
    from tests.papers.test_guidance import complete

    tid, v = complete(hub, "proposal")
    assert v["retry_checkpoint"] is None
    calls = hub.model_calls()
    r = hub.api("POST", f"/api/papers/{tid}/continue", json={})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "task_completed"
    assert hub.model_calls() == calls


def test_missing_material_without_failed_generation_does_not_offer_retry(hub):
    from agenthub_kernel.papers.db import PaperDocumentRow, PaperRow
    from tests.papers.test_guidance import complete

    tid, _ = complete(hub, "proposal")
    with hub.client.app.state.db.session() as s:
        task = s.get(ResearchTaskRow, tid)
        paper = s.scalar(select(PaperRow).where(PaperRow.task_id == tid, PaperRow.included.is_(True)))
        s.get(PaperDocumentRow, paper.document_id).parse_status = "failed"
        task.outcome = {**task.outcome, "studies": {paper.id: {"finished": True, "units": [], "depth": "material_missing"}}}
    assert hub.view(tid)["retry_checkpoint"] is None
    before = hub.model_calls()
    assert hub.api("POST", f"/api/papers/{tid}/continue", json={}).status_code == 409
    assert hub.model_calls() == before
