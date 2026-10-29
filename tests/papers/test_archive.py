"""Archive: hide conversations and research from the lists, keep everything, undo any time."""

from __future__ import annotations

from tests.papers.conftest import waiting


def _ids(hub, path, key, field, **params):
    return [x[field] for x in hub.api("GET", path, params=params).json()[key]]


def test_conversation_archive_hides_keeps_and_restores(hub):
    hub.prove(agents=("claude-code",))
    cid = _ids(hub, "/api/conversations", "conversations", "conversation_id")[0]
    before = hub.api("GET", f"/api/conversations/{cid}/messages").json()
    r = hub.api("POST", f"/api/conversations/{cid}/archive")
    assert r.status_code == 200 and r.json()["changed"] is True
    assert hub.api("POST", f"/api/conversations/{cid}/archive").json()["changed"] is False  # repeated click
    assert cid not in _ids(hub, "/api/conversations", "conversations", "conversation_id")
    assert cid in _ids(hub, "/api/conversations", "conversations", "conversation_id", archived="true")
    assert hub.api("GET", f"/api/conversations/{cid}/messages").json() == before  # nothing removed
    assert hub.api("POST", f"/api/conversations/{cid}/unarchive").status_code == 200
    assert cid in _ids(hub, "/api/conversations", "conversations", "conversation_id")
    assert cid not in _ids(hub, "/api/conversations", "conversations", "conversation_id", archived="true")
    assert hub.client.post(f"/api/conversations/{cid}/archive",
                           headers={"Authorization": "Bearer dev-agent-token"}).status_code == 403


def test_research_archive_takes_its_conversation_and_refuses_while_running(hub):
    hub.prove()
    task = hub.create()
    hub.client.app.state.autopilot.tick()
    r = hub.api("POST", f"/api/papers/{task}/archive")
    assert r.status_code == 409 and r.json()["detail"]["code"] == "item_busy"  # an active stage keeps working
    hub.run(task, until=waiting)
    hub.api("POST", f"/api/papers/{task}/stage/pause")  # a paused one may be put away
    assert hub.api("POST", f"/api/papers/{task}/archive").status_code == 200
    view = hub.view(task)
    assert view["archived"] is True and view["status"] == "paused"
    assert task not in _ids(hub, "/api/papers", "tasks", "task_id")
    assert task in _ids(hub, "/api/papers", "tasks", "task_id", archived="true")
    assert view["conversation_id"] in _ids(hub, "/api/conversations", "conversations", "conversation_id",
                                           archived="true")
    assert hub.api("POST", f"/api/papers/{task}/unarchive").status_code == 200
    assert task in _ids(hub, "/api/papers", "tasks", "task_id")
    assert view["conversation_id"] in _ids(hub, "/api/conversations", "conversations", "conversation_id")
    # archiving changed nothing about the work: resume and finish as before
    hub.api("POST", f"/api/papers/{task}/stage/resume")
    hub.choose(task, "continue")
    assert hub.run(task)["status"] == "completed"


def test_conversation_routes_after_the_archive_routes_still_work(hub):
    """The archive routes must not shadow /messages, /invocations and the other POST routes."""
    cid = hub.api("POST", "/api/conversations", json={}).json()["conversation_id"]
    assert hub.api("POST", f"/api/conversations/{cid}/messages", json={"text": "note"}).status_code == 200
