import json

import pytest

from agenthub_adapters.appserver_s3_evidence import S3Evidence, COMMAND


IDS = {"threadId": "t", "turnId": "r"}


def command(status):
    return {**IDS, "item": {"id": "cmd", "type": "commandExecution", "command": COMMAND,
                           "status": status, "processId": None, "exitCode": None}}


def pending():
    e = S3Evidence("t")
    e.settings_verified = True
    e.bind_turn("r")
    e.observe("turn/started", {"threadId": "t", "turn": {"id": "r", "status": "inProgress"}})
    e.observe("item/started", command("inProgress"))
    return e


def approval(**changes):
    return {**IDS, "itemId": "cmd", "command": COMMAND, "startedAtMs": 0, **changes}


def deny(e):
    e.receive_approval("req", approval(), cwd_matches=True)
    e.record_response()
    e.record_response(written=True)
    e.observe("serverRequest/resolved", {"threadId": "t", "requestId": "req"})
    e.observe("item/completed", command("declined"))


def reply(e):
    e.observe("item/started", {**IDS, "item": {"id": "msg", "type": "agentMessage"}})
    e.observe("item/agentMessage/delta", {**IDS, "itemId": "msg", "delta": "DECLINED"})
    e.observe("item/completed", {**IDS, "item": {"id": "msg", "type": "agentMessage", "text": "DECLINED"}})


def finish(e):
    e.observe("thread/tokenUsage/updated", {**IDS, "tokenUsage": {"last": {
        "inputTokens": 10, "outputTokens": 3, "totalTokens": 13, "cachedInputTokens": 0, "reasoningOutputTokens": 0}}})
    e.observe("turn/completed", {"threadId": "t", "turn": {"id": "r", "status": "completed"}})


def complete():
    e = pending()
    deny(e)
    reply(e)
    finish(e)
    return e


def test_matching_denial_chain_is_required_not_just_a_completed_turn():
    result = complete().summary()
    assert result["passed"] and result["probe_verdict"] == "PASS"
    assert result["terminal_confirmed"] and result["final_reply"] == "DECLINED"
    assert not result["execution_observed"]


@pytest.mark.parametrize("field", ["settings_verified", "response_attempted", "response_written", "resolved"])
def test_missing_verification_or_response_evidence_cannot_pass(field):
    e = complete()
    setattr(e, field, False)
    assert not e.summary()["passed"]


@pytest.mark.parametrize("change", [
    {"threadId": "foreign"}, {"turnId": "foreign"}, {"itemId": "foreign"},
    {"command": "another command"}, {"kind": "writeStdin"}, {"kind": "unknown"},
    {"approvalId": "subcallback"}, {"environmentId": "remote"},
    {"networkApprovalContext": {"host": "example.test"}}, {"availableDecisions": ["accept"]},
    {"availableDecisions": "decline"},
])
def test_wrong_scope_other_action_or_remote_approval_never_accepted(change):
    e = pending()
    e.receive_approval("req", approval(**change), cwd_matches=True)
    assert e.request is None and e.summary()["violation"]


@pytest.mark.parametrize("request_id", [True, None, {}, []])
def test_only_json_rpc_string_or_integer_id_allowed(request_id):
    e = pending()
    e.receive_approval(request_id, approval(), cwd_matches=True)
    assert e.request is None and e.summary()["violation"]


def test_notifications_before_turn_response_are_rechecked_when_bound():
    e = pending()
    e.turn_id = ""
    deny(e)
    reply(e)
    finish(e)
    e.bind_turn("foreign")
    assert e.summary()["probe_verdict"] == "FAIL"
    assert not e.summary()["terminal_confirmed"]


def test_duplicate_or_cross_thread_resolved_is_rejected():
    e = complete()
    e.observe("serverRequest/resolved", {"threadId": "foreign", "requestId": "req"})
    assert e.summary()["violation"]


def test_resolved_request_id_comparison_is_type_strict():
    e = pending()
    e.receive_approval(1, approval(), cwd_matches=True)
    e.record_response()
    e.record_response(written=True)
    e.observe("serverRequest/resolved", {"threadId": "t", "requestId": True})
    assert not e.resolved and e.summary()["violation"]


def test_resolved_before_written_denial_is_rejected():
    e = pending()
    e.receive_approval("req", approval(), cwd_matches=True)
    e.observe("serverRequest/resolved", {"threadId": "t", "requestId": "req"})
    assert not e.resolved and e.summary()["violation"]


def test_self_report_before_approval_is_not_denial_continuation():
    e = pending()
    reply(e)
    deny(e)
    finish(e)
    assert not e.summary()["passed"]
    assert not e.summary()["checks"]["reply_after_denial"]


def test_command_after_turn_completion_is_rejected():
    e = complete()
    e.observe("item/started", command("inProgress"))
    assert e.summary()["violation"]


def test_duplicate_approval_is_not_a_second_attempt():
    e = pending()
    e.receive_approval("req", approval(), cwd_matches=True)
    e.receive_approval("req", approval(), cwd_matches=True)
    assert e.summary()["violation"]


@pytest.mark.parametrize("change", [{"processId": "1"}, {"status": "completed"}])
def test_positive_execution_evidence_fails_even_when_marker_is_absent(change):
    e = pending()
    msg = command("inProgress")
    msg["item"].update(change)
    e.observe("item/started", msg)
    assert e.summary()["execution_observed"] and e.summary()["probe_verdict"] == "FAIL"


def test_synthetic_rejection_exit_code_is_not_labeled_as_proven_execution():
    e = pending()
    e.receive_approval("req", approval(), cwd_matches=True)
    e.record_response()
    e.record_response(written=True)
    e.observe("serverRequest/resolved", {"threadId": "t", "requestId": "req"})
    msg = command("declined")
    msg["item"]["exitCode"] = -1
    e.observe("item/completed", msg)
    reply(e)
    finish(e)
    result = e.summary()
    assert not result["passed"] and not result["execution_observed"]
    assert result["probe_verdict"] == "INCONCLUSIVE"


def test_output_or_unrelated_tool_is_not_ignored_or_recorded():
    e = complete()
    e.observe("item/commandExecution/outputDelta", {**IDS, "delta": "PRIVATE-OUTPUT"})
    assert e.summary()["probe_verdict"] == "FAIL"
    assert "PRIVATE" not in json.dumps(e.summary())


def test_generic_failed_command_is_not_a_verified_decline():
    e = pending()
    deny(e)
    e.commands[-1]["status"] = "failed"
    reply(e)
    finish(e)
    assert e.summary()["probe_verdict"] == "INCONCLUSIVE"
