"""The real factory path with a fake pinned file and an offline protocol peer.

No Codex binary is launched. The peer performs its temporary test action only
after receiving accept, so these cover the complete user-card/wire/action loop.
"""
import json
import pytest

from agenthub.stage import ToolRules, tool_decision
from agenthub_adapters.appserver_adapter import request_decisions
from agenthub_adapters import appserver as protocol
from agenthub_adapters.appserver_stub import InMemoryStubFactory, StdioStubFactory
from tests.http.test_appserver_s6 import pinned_file, real_adapter, real_factory  # noqa: F401
from tests.http.test_async_and_tool_approval import cards, eventually, pending_card, settle, start, status_of
from tests.http.test_conversation_stream import make_client, submit  # noqa: F401
from tests.http.test_conversations import USER, conversation

COMMAND = "item/commandExecution/requestApproval"
FILES = "item/fileChange/requestApproval"
PERMISSIONS = "item/permissions/requestApproval"
DIFF = "--- sample.txt\n+++ sample.txt\n@@ -0,0 +1 @@\n+approved content"


def supervised_peer(make_client, tmp_path, pinned_file, monkeypatch, requests):
    peer = InMemoryStubFactory("approval-default")
    responses, applied = [], []

    def wire(argv, *, env, cwd, stderr_path):
        # StdioTransport is the only replaced boundary; pinning, supervised
        # admission, kernel claim, callbacks and response serialization are real.
        transport = peer("offline-only", cwd)
        server = transport.server

        def drive():
            for index, (method, overrides) in enumerate(requests, 1):
                item_id = f"tool-{index}"
                item = {"id": item_id, "type": "fileChange" if method == FILES else "commandExecution",
                        "status": "inProgress", "command": "echo approved"}
                if method == FILES:
                    item["changes"] = [{"path": str(tmp_path / "sample.txt"),
                                        "kind": {"type": "add"}, "diff": DIFF}]
                server.notify("item/started", {"threadId": server.thread_id,
                                              "turnId": server.turn_id, "item": item})
                params = {"threadId": server.thread_id, "turnId": server.turn_id,
                          "itemId": item_id, "startedAtMs": 1, **overrides}
                if method == COMMAND:
                    params = {"command": "echo approved", "cwd": cwd,
                              "availableDecisions": ["accept", {"acceptWithExecpolicyAmendment":
                                                                 {"execpolicy_amendment": ["echo"]}}, "cancel"],
                              **params}
                rid = server.request(method, params)
                result = None
                while result is None and not server._stop.is_set():
                    response = server._wait_for_response(rid, timeout=0.1)
                    if response is not None:
                        result = response.get("result") or {}
                if result is None:
                    return
                responses.append(result)
                decision = result.get("decision", "decline")
                if decision == "accept":
                    applied.append(item_id)
                    (tmp_path / f"applied-{index}.txt").write_text("approved content")
                server.notify("item/completed", {"threadId": server.thread_id, "turnId": server.turn_id,
                                                "item": {**item, "status": "completed" if decision == "accept"
                                                                               else "declined"}})
            server.notify("item/agentMessage/delta", {"threadId": server.thread_id, "turnId": server.turn_id,
                                                     "itemId": "reply", "delta": "TOOL_DONE"})
            server._emit_item("reply", "agentMessage")
            server._finish_turn("completed")

        server._drive_default_approval = drive
        return transport

    monkeypatch.setattr(protocol, "StdioTransport", wire)
    _exe, ca = pinned_file
    adapter = real_adapter(tmp_path, real_factory(tmp_path, ca))
    client = make_client(adapter=adapter)
    client.app.state.offline_peer = peer
    cid = conversation(client)
    turn = submit(client, cid, execution_mode="supervised")
    assert client.post(f"/api/approvals/{turn['approval_id']}/approve", headers=USER,
                       json={"accept_unmanaged_native_agents": True}).status_code == 200
    assert start(client, turn).status_code == 202
    return client, cid, turn, responses, applied


@pytest.mark.parametrize("method", [COMMAND, FILES])
@pytest.mark.parametrize("decision", ["approve", "decline"])
def test_supervised_command_and_file_cards_deliver_only_the_users_decision(
        make_client, tmp_path, pinned_file, monkeypatch, method, decision):
    client, cid, turn, responses, applied = supervised_peer(
        make_client, tmp_path, pinned_file, monkeypatch, [(method, {})])
    card = pending_card(client, cid)
    assert card["available_decisions"] == ["accept", "decline"]
    assert responses == applied == []
    assert not (tmp_path / "applied-1.txt").exists()
    if method == FILES:
        assert card["details"]["changes"][0]["diff"] == DIFF
    else:
        assert card["details"]["command"] == "echo approved"
    path = f"/api/tool-approvals/{card['tool_approval_id']}/{decision}"
    if decision == "approve":
        assert client.post(path, headers=USER, json={"card_hash": "0" * 64}).status_code == 409
        assert not responses
    assert client.post(path, headers=USER, json={"card_hash": card["card_hash"]}).status_code == 200
    settle(client)
    sent = "accept" if decision == "approve" else "decline"
    assert responses == [{"decision": sent}]
    assert applied == (["tool-1"] if decision == "approve" else [])
    assert status_of(client, turn["invocation_id"]) == "succeeded"
    assert cards(client, cid)[0]["decision_sent"] == sent
    assert client.post(path, headers=USER, json={"card_hash": card["card_hash"]}).status_code == 409
    event_data = client.get(f"/api/conversations/{cid}/events", headers=USER).json()
    assert "approved content" not in json.dumps(event_data)


def test_allowing_one_request_does_not_approve_the_next(make_client, tmp_path, pinned_file, monkeypatch):
    client, cid, _turn, responses, applied = supervised_peer(
        make_client, tmp_path, pinned_file, monkeypatch, [(COMMAND, {}), (COMMAND, {})])
    first = pending_card(client, cid)
    client.post(f"/api/tool-approvals/{first['tool_approval_id']}/approve", headers=USER,
                json={"card_hash": first["card_hash"]})
    second = eventually(lambda: next((c for c in cards(client, cid, status="pending")
                                     if c["tool_approval_id"] != first["tool_approval_id"]), None))
    assert responses == [{"decision": "accept"}] and applied == ["tool-1"]
    assert not (tmp_path / "applied-2.txt").exists()
    client.post(f"/api/tool-approvals/{second['tool_approval_id']}/decline", headers=USER)
    settle(client)
    assert responses == [{"decision": "accept"}, {"decision": "decline"}]


@pytest.mark.parametrize("method, params", [
    (FILES, {"grantRoot": "C:\\"}),
    (COMMAND, {"availableDecisions": ["acceptForSession", "cancel"]}),
    (COMMAND, {"networkApprovalContext": {"host": "example.invalid", "protocol": "https"}}),
    (PERMISSIONS, {}),
])
def test_broader_grants_are_not_treated_as_one_operation(make_client, tmp_path, pinned_file, monkeypatch,
                                                        method, params):
    client, cid, _turn, responses, applied = supervised_peer(
        make_client, tmp_path, pinned_file, monkeypatch, [(method, params)])
    card = pending_card(client, cid)
    assert card["available_decisions"] == ["decline"]
    assert card["details"]["approval_block_reason"]
    path = f"/api/tool-approvals/{card['tool_approval_id']}"
    assert client.post(path + "/approve", headers=USER, json={"card_hash": card["card_hash"]}).status_code == 409
    client.post(path + "/decline", headers=USER)
    settle(client)
    assert responses == ([{"permissions": {}, "scope": "turn"}] if method == PERMISSIONS
                         else [{"decision": "decline"}])
    assert applied == []


def test_automatic_command_rule_does_not_grant_extra_permissions():
    decision, _why = tool_decision(ToolRules(command_prefixes=("echo",)), COMMAND,
        {"command_view": {"parsed": True, "argv": ["echo", "approved"]},
         "additionalPermissions": {"fileSystem": {"write": ["C:\\"]}}})
    assert decision == "declined"


def test_file_allow_over_the_bundled_stdio_transport(make_client):
    client = make_client(StdioStubFactory("approval-file"))
    cid = conversation(client)
    turn = submit(client, cid)
    client.post(f"/api/approvals/{turn['approval_id']}/approve", headers=USER)
    assert start(client, turn).status_code == 202
    card = pending_card(client, cid)
    assert card["method"] == FILES and "accept" in card["available_decisions"]
    assert "offline approved content" in card["details"]["changes"][0]["diff"]
    client.post(f"/api/tool-approvals/{card['tool_approval_id']}/approve", headers=USER,
                json={"card_hash": card["card_hash"]})
    settle(client)
    assert status_of(client, turn["invocation_id"]) == "succeeded"
    assert cards(client, cid)[0]["decision_sent"] == "accept"


@pytest.mark.parametrize("method, params", [
    (COMMAND, {"command": "echo " + "x" * 8000}),
    (FILES, {"changes": [{"path": "sample.txt", "diff": "x" * 8000}]}),
])
def test_an_incomplete_preview_cannot_be_approved(method, params):
    choices, why = request_decisions(method, params, enabled=True)
    assert choices == ["decline"] and "完整预览" in why


@pytest.mark.parametrize("foreign", [False, True])
def test_provider_resolution_voids_only_its_own_thread_card(make_client, tmp_path, pinned_file, monkeypatch, foreign):
    client, cid, turn, responses, applied = supervised_peer(
        make_client, tmp_path, pinned_file, monkeypatch, [(COMMAND, {})])
    card = pending_card(client, cid)
    server = client.app.state.offline_peer.servers[0]
    server.notify("serverRequest/resolved", {"threadId": "other-thread" if foreign else server.thread_id,
                                           "requestId": "srv-1"})
    path = f"/api/tool-approvals/{card['tool_approval_id']}"
    if foreign:
        client.post(path + "/approve", headers=USER, json={"card_hash": card["card_hash"]})
        settle(client)
        assert responses == [{"decision": "accept"}]
    else:
        eventually(lambda: client.get(path, headers=USER).json()["status"] == "void")
        assert client.post(path + "/approve", headers=USER,
                           json={"card_hash": card["card_hash"]}).status_code == 409
        assert responses == applied == []
        server._finish_turn("completed")
        settle(client)
        assert status_of(client, turn["invocation_id"]) == "succeeded"
