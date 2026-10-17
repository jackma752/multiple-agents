import copy
import json

import pytest

from agenthub_adapters.appserver_s2_evidence import S2Evidence


def events():
    ids = {"threadId": "t", "turnId": "r"}
    return [
        ("turn/started", {"threadId": "t", "turn": {"id": "r", "status": "inProgress"}}),
        ("item/started", {**ids, "item": {"id": "m", "type": "agentMessage"}}),
        ("item/agentMessage/delta", {**ids, "itemId": "m", "delta": "O"}),
        ("item/agentMessage/delta", {**ids, "itemId": "m", "delta": "K"}),
        ("item/completed", {**ids, "item": {"id": "m", "type": "agentMessage", "text": "OK"}}),
        ("thread/tokenUsage/updated", {**ids, "tokenUsage": {"last": {
            "inputTokens": 11, "outputTokens": 2, "cachedInputTokens": 4,
            "reasoningOutputTokens": 0, "totalTokens": 13}}}),
        ("turn/completed", {"threadId": "t", "turn": {"id": "r", "status": "completed"}}),
    ]


def evidence(messages=None, *, bind=True):
    result = S2Evidence("t")
    for method, params in events() if messages is None else messages:
        result.observe(method, params)
    if bind:
        result.bind_turn("r")
    return result


def test_early_notifications_and_independent_usage_are_reduced_after_binding():
    e = evidence(bind=False)
    assert not e.summary()["terminal_confirmed"]
    e.bind_turn("r")
    result = e.summary()
    assert result["passed"] and result["verdict"] == "finished"
    assert result["usage"]["inputTokens"] == 11
    assert result["final_reply"] == "OK"


@pytest.mark.parametrize("missing", range(7))
def test_every_success_evidence_component_is_required(missing):
    messages = events()
    messages.pop(missing)
    assert not evidence(messages).summary()["passed"]


@pytest.mark.parametrize("status,terminal", [("failed", True), ("interrupted", True), ("inProgress", False), (None, False)])
def test_terminal_does_not_mean_success(status, terminal):
    messages = events()
    messages[-1][1]["turn"]["status"] = status
    result = evidence(messages).summary()
    assert result["terminal_confirmed"] is terminal
    assert not result["passed"]


@pytest.mark.parametrize("position", range(7))
@pytest.mark.parametrize("field,value", [("threadId", "other"), ("threadId", None), ("turnId", "other"), ("turnId", None)])
def test_foreign_or_missing_ids_never_settle(position, field, value):
    messages = events()
    payload = messages[position][1]
    if field == "turnId" and "turn" in payload:
        payload["turn"]["id"] = value
    else:
        payload[field] = value
    result = evidence(messages).summary()
    assert not result["passed"] and not result["terminal_confirmed"]


@pytest.mark.parametrize("mutation", ["delta_before_start", "delta_wrong_item", "final_before_delta", "duplicate_terminal", "second_turn"])
def test_invalid_stream_order_or_identity(mutation):
    messages = events()
    if mutation == "delta_before_start":
        messages[1], messages[2] = messages[2], messages[1]
    elif mutation == "delta_wrong_item":
        messages[2][1]["itemId"] = "another"
    elif mutation == "final_before_delta":
        messages[3], messages[4] = messages[4], messages[3]
    elif mutation == "duplicate_terminal":
        messages.append(copy.deepcopy(messages[-1]))
    else:
        messages.append(copy.deepcopy(messages[0]))
    assert not evidence(messages).summary()["passed"]


def test_delta_for_an_unstarted_other_item_cannot_hide_behind_a_valid_final():
    messages = events()
    messages.insert(4, ("item/agentMessage/delta", {
        "threadId": "t", "turnId": "r", "itemId": "unstarted-other-item", "delta": "OK"}))
    result = evidence(messages).summary()
    assert not result["passed"] and result["violation"]


@pytest.mark.parametrize("status", ["failed", "completed", "interrupted", None])
def test_started_notification_must_be_in_progress(status):
    messages = events()
    messages[0][1]["turn"]["status"] = status
    assert not evidence(messages).summary()["passed"]


@pytest.mark.parametrize("value", [None, True, -1, "12", 1.0])
def test_usage_is_received_as_nonnegative_integers_not_fabricated(value):
    messages = events()
    messages[5][1]["tokenUsage"]["last"]["inputTokens"] = value
    assert not evidence(messages).summary()["passed"]


def test_usage_after_terminal_is_accepted():
    messages = events()
    messages[5], messages[6] = messages[6], messages[5]
    assert evidence(messages).summary()["passed"]


@pytest.mark.parametrize("kind", ["commandExecution", "fileChange", "collabAgentToolCall", "unknown"])
def test_tool_and_child_activity_rejected(kind):
    messages = events()
    messages[1][1]["item"]["type"] = kind
    assert evidence(messages).summary()["violation"]


def test_unknown_tool_delta_is_not_ignored():
    e = evidence()
    e.observe("item/commandExecution/outputDelta", {"threadId": "t", "turnId": "r", "delta": "private"})
    assert e.summary()["violation"]


def test_remote_text_and_error_details_are_not_stored():
    e = evidence()
    e.observe("item/completed", {"threadId": "t", "turnId": "r", "item": {
        "id": "extra", "type": "agentMessage", "text": "SECRET-TEXT"}})
    e.observe("error", {"threadId": "t", "turnId": "r", "message": "SECRET-ERROR"})
    assert "SECRET" not in json.dumps([e.events, e.summary()])
    assert not e.summary()["passed"]


def test_bounded_events_and_single_turn_binding():
    e = evidence()
    e.bind_turn("r")
    assert e.summary()["violation"]
    for _ in range(600):
        e.observe("server/notice", {})
    assert len(e.events) == e.MAX_EVENTS
    assert not e.summary()["passed"]
