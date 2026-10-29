"""Full-text supplementation: both policies, the 30-minute rule, batches, races, restart, pause/stop.

The clock is injected (``hub.clock``); nothing waits for real time.
"""

from __future__ import annotations

from sqlalchemy import func, select

from agenthub_kernel.papers.db import SupplementRequestRow
from tools.demo.paper_fixtures import supplement_pdf

from tests.papers.conftest import waiting


def _extract_steps(view):
    return [s for s in view["steps"] if s["name"] in ("extract", "synthesize", "outline", "section")]


def _requests(hub):
    with hub.client.app.state.db.session() as s:
        return s.scalar(select(func.count()).select_from(SupplementRequestRow))


def test_policy_one_times_out_once_after_thirty_minutes(hub):
    hub.prove()
    task = hub.create()
    view = hub.run(task, until=waiting)
    deadline = view["supplement"]["deadline_at"]
    hub.clock.advance(minutes=29, seconds=59)
    view = hub.run(task, passes=3, until=lambda v: False)
    assert view["supplement"]["status"] == "awaiting_choice" and view["supplement"]["deadline_at"] == deadline
    assert not _extract_steps(view)
    hub.clock.advance(seconds=1)
    view = hub.run(task)
    assert view["status"] == "completed"
    sup = view["supplement"]
    assert sup["decision"] == "timeout_continue" and sup["decided_by"] == "timeout"
    assert [h["event"] for h in sup["history"]].count("resolved") == 1
    assert _requests(hub) == 1
    assert all(i["state"] == "not_supplied" for i in sup["items"])
    depth = {p["source_id"]: p["read_depth"] for p in view["papers"] if p["included"]}
    assert depth["2601.00103"] == "abstract" and depth["2601.00104"] == "abstract"


def test_policy_one_choice_to_supplement_cancels_the_deadline(hub):
    hub.prove()
    task = hub.create()
    hub.run(task, until=waiting)
    assert hub.choose(task, "supplement").json() == {"changed": True, "status": "awaiting_files"}
    assert hub.choose(task, "supplement").json()["changed"] is False  # repeated click
    hub.clock.advance(hours=3)
    view = hub.run(task, passes=4, until=lambda v: False)
    assert view["status"] == "waiting_input"
    assert view["supplement"]["status"] == "awaiting_files" and view["supplement"]["deadline_at"] is None
    assert not _extract_steps(view)


def test_policy_one_explicit_no_releases_the_gate_immediately(hub):
    hub.prove()
    task = hub.create()
    hub.run(task, until=waiting)
    assert hub.choose(task, "continue").json()["status"] == "resolved"
    assert hub.choose(task, "continue").json()["changed"] is False
    view = hub.run(task)
    assert view["status"] == "completed" and view["supplement"]["decision"] == "user_continue"


def test_policy_two_waits_without_deadline_and_downstream_does_not_start(hub):
    hub.prove()
    task = hub.create(policy="wait_for_fulltext")
    view = hub.run(task, until=waiting)
    sup = view["supplement"]
    assert sup["status"] == "awaiting_files" and sup["deadline_at"] is None and sup["open_batch_id"]
    assert len(sup["items"]) == 3
    calls = hub.model_calls()
    hub.clock.advance(hours=5)
    view = hub.run(task, passes=5, until=lambda v: False)
    assert view["status"] == "waiting_input" and not _extract_steps(view)
    assert hub.model_calls() == calls  # waiting asks no model anything


def test_policy_two_partial_batches_ask_once_each_until_complete(hub):
    hub.prove()
    task = hub.create(policy="wait_for_fulltext", target=4)
    view = hub.run(task, until=waiting)
    missing = {i["paper_id"] for i in view["supplement"]["items"]}
    c, d = hub.paper_id(task, "2601.00103"), hub.paper_id(task, "2601.00104")
    assert missing == {c, d}
    batch = hub.upload(task, [("x.pdf", supplement_pdf("2601.00103"))], targets=[c]).json()["batch_id"]
    assert hub.submit(task, batch).json()["changed"] is True
    again = hub.submit(task, batch)  # a repeated submit (double click, retry) changes nothing
    assert again.status_code == 200 and again.json()["changed"] is False
    view = hub.run(task, until=lambda v: v["supplement"]["status"] == "awaiting_more_choice")
    sup = view["supplement"]
    assert sup["remaining"] == 1 and [h["event"] for h in sup["history"]].count("batch_incomplete") == 1
    hub.clock.advance(hours=1)
    view = hub.run(task, passes=3, until=lambda v: False)
    assert view["supplement"]["status"] == "awaiting_more_choice"  # silence is not "no"
    hub.choose(task, "supplement")
    hub.upload(task, [("y.pdf", supplement_pdf("2601.00104"))], targets=[d])
    hub.submit(task)
    view = hub.run(task)
    assert view["status"] == "completed"
    assert view["supplement"]["decision"] == "all_supplied" and view["supplement"]["round"] == 2
    assert all(p["read_depth"] == "fulltext" for p in view["papers"] if p["included"])


def test_upload_while_asked_records_the_choice_first(hub):
    hub.prove()
    task = hub.create()
    hub.run(task, until=waiting)
    req = hub.view(task)["supplement"]["request_id"]
    r = hub.api("POST", f"/api/papers/{task}/supplement/begin", json={"request_id": req})
    assert r.status_code == 200
    hub.clock.advance(minutes=45)  # the transfer takes long; the deadline is already gone
    view = hub.run(task, passes=3, until=lambda v: False)
    assert view["supplement"]["status"] == "awaiting_files" and view["supplement"]["supplement_chosen_at"]


def test_choice_wins_when_it_commits_before_the_timeout(hub):
    hub.prove()
    task = hub.create()
    hub.run(task, until=waiting)
    hub.clock.advance(minutes=31)  # past the deadline, but no scheduler pass has run yet
    assert hub.choose(task, "supplement").json()["status"] == "awaiting_files"
    view = hub.run(task, passes=3, until=lambda v: False)
    assert view["supplement"]["status"] == "awaiting_files" and view["supplement"]["decision"] is None


def test_late_upload_after_timeout_is_refused_not_merged(hub):
    hub.prove()
    task = hub.create()
    hub.run(task, until=waiting)
    c = hub.paper_id(task, "2601.00103")
    hub.clock.advance(minutes=30)
    hub.run(task, passes=1, until=lambda v: True)
    assert hub.view(task)["supplement"]["decision"] == "timeout_continue"
    r = hub.upload(task, [("c.pdf", supplement_pdf("2601.00103"))], targets=[c])
    assert r.status_code == 409 and r.json()["detail"]["code"] == "request_closed"
    assert hub.choose(task, "supplement").status_code == 409


def test_restart_keeps_the_deadline_and_request(hub):
    hub.prove()
    task = hub.create()
    view = hub.run(task, until=waiting)
    request_id, deadline = view["supplement"]["request_id"], view["supplement"]["deadline_at"]
    hub.clock.advance(minutes=20)
    hub.restart()
    view = hub.view(task)
    assert view["supplement"]["request_id"] == request_id and view["supplement"]["deadline_at"] == deadline
    assert view["supplement"]["remaining_seconds"] == 10 * 60
    hub.run(task, passes=2, until=lambda v: False)
    assert _requests(hub) == 1
    hub.clock.advance(minutes=10)
    view = hub.run(task)
    assert view["status"] == "completed" and view["supplement"]["decision"] == "timeout_continue"


def test_pause_blocks_the_timeout_and_resume_applies_it(hub):
    hub.prove()
    task = hub.create()
    hub.run(task, until=waiting)
    assert hub.api("POST", f"/api/papers/{task}/stage/pause").status_code == 200
    hub.clock.advance(hours=1)
    calls = hub.model_calls()
    for _ in range(3):
        hub.client.app.state.autopilot.tick()
    view = hub.view(task)
    assert view["status"] == "paused" and view["supplement"]["status"] == "awaiting_choice"
    assert hub.model_calls() == calls
    hub.api("POST", f"/api/papers/{task}/stage/resume")
    view = hub.run(task)
    assert view["supplement"]["decision"] == "timeout_continue" and view["status"] == "completed"


def test_stop_while_waiting_never_continues(hub):
    hub.prove()
    task = hub.create()
    hub.run(task, until=waiting)
    calls, fetches = hub.model_calls(), len(hub.fetcher.calls)
    assert hub.api("POST", f"/api/papers/{task}/stage/stop").status_code == 200
    hub.clock.advance(hours=2)
    for _ in range(3):
        hub.client.app.state.autopilot.tick()
    view = hub.view(task)
    assert view["status"] == "stopped" and view["supplement"]["status"] == "awaiting_choice"
    assert hub.model_calls() == calls and len(hub.fetcher.calls) == fetches


def test_no_missing_fulltext_creates_no_question(hub):
    hub.prove()
    task = hub.create(target=2)
    view = hub.run(task)
    assert view["status"] == "completed" and view["supplement"] is None


def test_missing_reference_list_is_downloadable(hub):
    hub.prove()
    task = hub.create()
    hub.run(task, until=waiting)
    r = hub.api("GET", f"/api/papers/{task}/supplement/references.txt")
    lines = [line for line in r.text.splitlines() if line]
    assert r.status_code == 200 and len(lines) == 3
    assert any("Silva Duarte. Human Evaluation Protocols for Retrieval Augmented Generation. 2025." in line
               for line in lines)
