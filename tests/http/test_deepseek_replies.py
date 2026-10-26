"""Complete DeepSeek replies persist, recover once, and satisfy plan dependencies."""
import pytest
from fastapi.testclient import TestClient

from agenthub_adapters.deepseek import DeepSeekAdapter, StubTransport
from agenthub_adapters.store import ExecutionStore, FAILED, FINISHED
from agenthub_kernel.conversations import restore_replies
from agenthub_kernel.db import Database
from agenthub_rt.api import create_app

USER = {"Authorization": "Bearer dev-user-token"}


def setup(tmp_path, scenario="ok"):
    transport = StubTransport(scenario)
    adapter = DeepSeekAdapter(
        "https://api.deepseek.com", "deepseek-flash", stub=scenario,
        credentials_path=str(tmp_path / "not-needed"), transport=transport,
        store=ExecutionStore(str(tmp_path / "adapter.db")),
    )
    app = create_app(db=Database(f"sqlite:///{(tmp_path / 'hub.db').as_posix()}"), adapter=adapter)
    return app, adapter, transport


def submit(client, cid, key="one"):
    response = client.post(f"/api/conversations/{cid}/invocations", headers=USER,
                           json={"agent_id": "deepseek", "text": "offline test", "client_request_id": key})
    assert response.status_code == 200, response.text
    return response.json()


def run(client, turn):
    assert client.post(f"/api/approvals/{turn['approval_id']}/approve", headers=USER).status_code == 200
    return client.post(f"/api/invocations/{turn['invocation_id']}/run", headers=USER, params={"wait": "true"})


def replies(client, cid):
    return [m for m in client.get(f"/api/conversations/{cid}/messages", headers=USER).json()["messages"]
            if m["role"] == "assistant"]


@pytest.mark.parametrize("scenario", ["ok", "truncated"])
def test_only_complete_reply_becomes_a_durable_message(tmp_path, scenario):
    app, adapter, transport = setup(tmp_path, scenario)
    with TestClient(app) as client:
        cid = client.post("/api/conversations", headers=USER, json={}).json()["conversation_id"]
        turn = submit(client, cid)
        run(client, turn)
        saved = adapter.store.get(turn["invocation_id"])["payload"]
        if scenario == "ok":
            assert saved["final_message"] == saved["reply"] == "离线回复。"
            assert [m["text"] for m in replies(client, cid)] == ["离线回复。"]
        else:
            assert "final_message" not in saved
            assert adapter.last_result(turn["invocation_id"]) is None
            assert replies(client, cid) == []
        assert restore_replies(app.state.db, adapter) == 0
        assert len(transport.payloads) == 1
    app.state.db.engine.dispose()


def test_legacy_reply_is_recovered_once_without_model_call_or_store_rewrite(tmp_path):
    app, adapter, transport = setup(tmp_path)
    # Simulate the old adapter having no kernel projection.
    adapter.last_result = lambda invocation_id: None
    with TestClient(app) as client:
        cid = client.post("/api/conversations", headers=USER, json={}).json()["conversation_id"]
        turn = submit(client, cid)
        run(client, turn)
        assert replies(client, cid) == []
        record = adapter.store.get(turn["invocation_id"])
        legacy = dict(record["payload"])
        legacy.pop("final_message")
        adapter.store.set_payload(turn["invocation_id"], legacy)
        del adapter.last_result
        assert restore_replies(app.state.db, adapter) == 1
        assert [m["text"] for m in replies(client, cid)] == [legacy["reply"]]
        assert restore_replies(app.state.db, adapter) == 0
        assert len(transport.payloads) == 1
        assert adapter.store.get(turn["invocation_id"])["payload"] == legacy
        result = adapter.last_result(turn["invocation_id"])
        assert result["thread_id"] is None and "native_session" not in result
    app.state.db.engine.dispose()


def test_successful_deepseek_step_unlocks_dependency_and_collects_results(tmp_path):
    app, adapter, transport = setup(tmp_path)
    with TestClient(app) as client:
        cid = client.post("/api/conversations", headers=USER, json={}).json()["conversation_id"]
        root = submit(client, cid)["task_id"]  # creates a tree without running an opener
        assert client.patch(f"/api/tasks/{root}/policy", headers=USER,
                            json={"collaboration_enabled": True}).status_code == 200
        plan = client.post(f"/api/conversations/{cid}/plans", headers=USER, json={"items": [
            {"agent_id": "deepseek", "goal": "Write a requirement"},
            {"agent_id": "deepseek", "goal": "Review the result", "depends_on": [1]},
        ]}).json()
        pid = plan["plan_id"]
        for index in (1, 2):
            response = client.post(f"/api/conversations/{cid}/plans/{pid}/items/{index}/dispatch",
                                   headers=USER, json={"client_request_id": str(index)})
            assert response.status_code == 200, response.text
            turn = response.json()
            if index == 2:
                assert "离线回复。" in turn["sent_text"] and "Review the result" in turn["sent_text"]
            assert run(client, turn).json()["status"] == "succeeded"
        plans = client.get(f"/api/conversations/{cid}/plans", headers=USER).json()
        assert plans["plans"][0]["all_done"]
        assert [item["state"] for item in plans["plans"][0]["items"]] == ["done", "done"]
        collect = client.post(f"/api/conversations/{cid}/plans/{pid}/collect", headers=USER,
                              json={"agent_id": "deepseek", "client_request_id": "collect"})
        assert collect.status_code == 200, collect.text
        assert len(collect.json()["handoff"]) == 2
        assert run(client, collect.json()).json()["status"] == "succeeded"
        assert len(replies(client, cid)) == 3 and len(transport.payloads) == 3
    app.state.db.engine.dispose()


def test_failed_record_or_missing_finish_cannot_supply_a_final_reply(tmp_path):
    _, adapter, _ = setup(tmp_path)
    adapter.store.record_start("bad", "external-bad")
    adapter.store.set_payload("bad", {"final_message": "not final", "reply": "not final", "finish_reason": "stop"})
    adapter.store.set_status("bad", FAILED)
    assert adapter.last_result("bad") is None
    adapter.store.set_status("bad", FINISHED)
    adapter.store.set_payload("bad", {"reply": "no terminal evidence"})
    assert adapter.last_result("bad") is None
    assert adapter.last_result("missing") is None
