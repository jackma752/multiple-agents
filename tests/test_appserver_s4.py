"""S4 interrupt probe: offline only (direct evidence calls or the Python stdio peer).

No Codex, no model, no credentials. Source facts (tag rust-v0.163.0-alpha.11)
are cited in docs/appserver-s4-probe.md.
"""
import importlib.util
import json
from pathlib import Path

import pytest

from agenthub_adapters import appserver as A
from agenthub_adapters.appserver_s4_evidence import S4Evidence

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("s4_probe_under_test", ROOT / "tools/probes/appserver_s4.py")
S4 = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(S4)

IDS = {"threadId": "t", "turnId": "r"}
SECRET = "MODEL-TEXT-NEVER-STORED"


# --------------------------------------------------------------------------- #
# the stdio peer, every scenario
# --------------------------------------------------------------------------- #

def run(monkeypatch, tmp_path, scenario):
    monkeypatch.setattr(S4, "RUN_ROOT", tmp_path / "evidence")
    monkeypatch.setattr(S4.S2, "WORK_ROOT", tmp_path)
    monkeypatch.setattr(S4.S2.S1, "preflight", lambda *_: pytest.fail("offline only"))
    code = S4.run_probe(scenario=scenario)
    raw = next((tmp_path / "evidence").glob("*/result.json")).read_text(encoding="utf-8")
    assert "SYNTHETIC-TEXT" not in raw and "SYNTHETIC rejected" not in raw, "no model or server text is stored"
    result = json.loads(raw)
    # Invariants of every run: one turn, at most one interrupt, never an answer.
    methods = [m["method"] for m in result.get("sent", [])]
    assert methods.count("turn/start") <= 1 and methods.count("turn/interrupt") <= 1
    assert all(m in ("initialize", "initialized", "thread/start", "turn/start", "turn/interrupt") for m in methods)
    assert result["tool_activity"]["server_requests_answered"] == 0 if "tool_activity" in result else True
    assert result.get("exit_code") in (0, None), "the peer exits 3 if it ever receives an answer or a second interrupt"
    return code, result


@pytest.mark.parametrize("scenario,verdict,codes", [
    ("happy", "PASS", []),
    ("late-response", "PASS", []),
    ("response-no-terminal", "INCONCLUSIVE", ["terminal_not_matched", "timeout"]),
    ("interrupt-error", "INCONCLUSIVE", ["interrupt_rejected", "terminal_not_matched", "timeout"]),
    ("completed-first", "INCONCLUSIVE", ["turn_completed_before_interrupt:completed"]),
    ("completed-race", "INCONCLUSIVE", ["interrupt_rejected", "interrupt_not_effective:completed"]),
    ("failed-terminal", "INCONCLUSIVE", ["interrupt_not_effective:failed"]),
    ("wrong-turn-terminal", "FAIL", ["foreign_turn"]),
    ("foreign-thread", "FAIL", ["foreign_thread"]),
    ("duplicate-terminal", "FAIL", ["duplicate_terminal"]),
    ("late-delta", "FAIL", ["event_after_terminal"]),
    ("disconnect", "INCONCLUSIVE", ["terminal_not_matched", "disconnect_before_terminal"]),
    ("timeout", "INCONCLUSIVE", ["interrupt_not_answered", "terminal_not_matched", "timeout"]),
    ("tool-item", "INCONCLUSIVE", ["tool_process_unknown"]),
    ("server-request", "INCONCLUSIVE", ["server_request_unanswered:item/commandExecution/requestApproval",
                                        "tool_process_unknown"]),
    ("auto-review", "INCONCLUSIVE", ["settings_mismatch"]),
])
def test_every_scenario_has_its_verdict_and_reasons(monkeypatch, tmp_path, scenario, verdict, codes):
    code, r = run(monkeypatch, tmp_path, scenario)
    assert r["probe_verdict"] == verdict, r["reason_codes"]
    assert set(codes) <= set(r["reason_codes"]), r["reason_codes"]
    assert code == (0 if verdict == "PASS" else 1)
    if verdict == "PASS":
        assert r["reason_codes"] == [] and r["main_turn"]["status"] == "interrupted"
        assert r["interrupt"]["response"] == "ok" and r["tool_activity"]["state"] == "none"
        assert r["local_cleanup"]["process_stopped"] and r["local_cleanup"]["workdir_removed"]
        assert r["local_cleanup"]["descendants_after_close"] == []


def test_happy_interrupts_on_the_first_delta_and_answer_precedes_the_terminal(monkeypatch, tmp_path):
    _, r = run(monkeypatch, tmp_path, "happy")
    assert r["interrupt"]["trigger"] == "first_delta" and r["interrupt"]["response_after_terminal"] is False
    assert [m["method"] for m in r["sent"]][-1] == "turn/interrupt" and len(r["sent"]) == 5
    assert r["main_turn"]["terminal_after_interrupt"] and r["text_format"]["decides_verdict"] is False
    # The interrupted message never completes; that is expected, not a defect.
    assert r["text_format"]["assistant_messages"] == 1 and r["text_format"]["completed_messages"] == 0


def test_answer_after_the_terminal_is_only_an_ordering_fact(monkeypatch, tmp_path):
    _, r = run(monkeypatch, tmp_path, "late-response")
    assert r["probe_verdict"] == "PASS" and r["interrupt"]["response_after_terminal"] is True


def test_fallback_timer_interrupts_a_turn_with_no_visible_output(monkeypatch, tmp_path):
    monkeypatch.setattr(S4.S4Client, "FALLBACK_SECONDS", 0.2)
    _, r = run(monkeypatch, tmp_path, "timer")
    assert r["probe_verdict"] == "PASS" and r["interrupt"]["trigger"] == "timer"


def test_a_turn_that_completes_first_is_never_interrupted(monkeypatch, tmp_path):
    _, r = run(monkeypatch, tmp_path, "completed-first")
    assert [m["method"] for m in r["sent"]] == ["initialize", "initialized", "thread/start", "turn/start"]
    assert r["interrupt"]["sent"] is False and r["main_turn"]["status"] == "completed"


@pytest.mark.parametrize("scenario", ["response-no-terminal", "timeout", "disconnect"])
def test_without_the_turns_own_terminal_nothing_is_claimed(monkeypatch, tmp_path, scenario):
    _, r = run(monkeypatch, tmp_path, scenario)
    assert r["terminal_confirmed"] is False and r["verdict"] == "unconfirmed"
    assert r["main_turn"]["matched"] is False and r["manual_review_required"] is True
    # Local cleanup is reported on its own: our process stopped, which proves nothing remote.
    assert r["local_cleanup"]["process_stopped"] is True


def test_tool_activity_is_reported_apart_from_the_main_turn(monkeypatch, tmp_path):
    _, r = run(monkeypatch, tmp_path, "tool-item")
    assert r["main_turn"]["status"] == "interrupted" and r["interrupt"]["trigger"] == "tool_activity"
    assert r["tool_activity"]["state"] == "unknown"
    assert r["tool_activity"]["items"] == [{"type": "commandExecution", "process_seen": True, "end": None}]


def test_server_request_triggers_the_interrupt_and_is_never_answered(monkeypatch, tmp_path):
    _, r = run(monkeypatch, tmp_path, "server-request")
    assert r["interrupt"]["trigger"] == "server_request"
    assert [m["method"] for m in r["sent"]][-1] == "turn/interrupt"
    assert r["tool_activity"]["server_requests"] == ["item/commandExecution/requestApproval"]


def test_settings_mismatch_means_no_turn_and_no_interrupt(monkeypatch, tmp_path):
    _, r = run(monkeypatch, tmp_path, "auto-review")
    assert r["turn_attempts"] == 0 and [m["method"] for m in r["sent"]] == ["initialize", "initialized", "thread/start"]


# --------------------------------------------------------------------------- #
# real mode gate and identity
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("approved,risk", [(False, False), (True, False), (False, True)])
def test_real_mode_requires_both_acknowledgements_before_any_side_effect(monkeypatch, tmp_path, approved, risk):
    target = tmp_path / "absent"
    monkeypatch.setattr(S4, "RUN_ROOT", target)
    monkeypatch.setattr(S4.S2.S1, "preflight", lambda *_: pytest.fail("must not touch the real home"))
    monkeypatch.setattr(S4.A, "StdioTransport", lambda *a, **kw: pytest.fail("must not launch"))
    assert S4.run_probe(real=True, approved=approved, acknowledge_risk=risk) == 2
    assert not target.exists()


def test_real_mode_preflights_the_pinned_alpha11_identity_and_only_the_happy_scenario(monkeypatch, tmp_path):
    seen = []

    def preflight(home, identity=None):
        seen.append(identity)
        raise S4.S2.S1.CheckFailed("stop here: offline test")

    monkeypatch.setattr(S4, "RUN_ROOT", tmp_path / "runs")
    monkeypatch.setattr(S4.S2, "WORK_ROOT", tmp_path)
    monkeypatch.setattr(S4.S2.S1, "preflight", preflight)
    monkeypatch.setattr(S4.A, "StdioTransport", lambda *a, **kw: pytest.fail("must not launch"))
    assert S4.run_probe(real=True, approved=True, acknowledge_risk=True, codex_home=str(tmp_path / "h")) == 1
    assert seen == [S4.S3.S3_CODEX_IDENTITY]
    assert seen[0][0].endswith(r"bin\6257d9e8f63c0f2b\codex.exe")
    with pytest.raises(ValueError):
        S4.run_probe(real=True, approved=True, acknowledge_risk=True, scenario="tool-item")


# --------------------------------------------------------------------------- #
# the client's outgoing rules
# --------------------------------------------------------------------------- #

class RecordingTransport:
    pid = None

    def __init__(self):
        self.messages = []

    def write(self, data):
        self.messages.append(json.loads(data))

    def close(self):
        pass


def ready(*, started=True):
    transport = RecordingTransport()
    client = S4.S4Client(transport, workdir=str(ROOT), timeout=5)
    client._send({"id": 1, "method": "initialize", "params": {"clientInfo": {"name": "agent-hub-s4", "version": "0.1.0"}}})
    client._dispatch({"id": 1, "result": {}})
    client.notify("initialized")
    client._send({"id": 2, "method": "thread/start", "params": client.thread_params()})
    client._dispatch({"id": 2, "result": {}})
    client.evidence = S4Evidence("t")
    client.validate_thread_settings({"cwd": str(ROOT), "approvalPolicy": "on-request", "approvalsReviewer": "user",
                                     "sandbox": {"type": "readOnly", "networkAccess": False}})
    client._send({"id": 3, "method": "turn/start", "params": client.turn_params()})
    client.evidence.bind_turn("r")
    if started:
        client._dispatch({"method": "turn/started", "params": {"threadId": "t", "turn": {"id": "r", "status": "inProgress"}}})
    return client, transport


def test_interrupt_is_bound_to_the_turn_and_sent_once():
    client, transport = ready()
    client._dispatch({"method": "item/agentMessage/delta", "params": {**IDS, "itemId": "m", "delta": SECRET}})
    client.tick()
    client.tick()
    interrupts = [m for m in transport.messages if m.get("method") == "turn/interrupt"]
    assert interrupts == [{"jsonrpc": "2.0", "id": interrupts[0]["id"], "method": "turn/interrupt", "params": IDS}]
    with pytest.raises(A.AppServerProtocolError):
        client.send_interrupt("again")
    assert len([m for m in transport.messages if m.get("method") == "turn/interrupt"]) == 1


def test_no_interrupt_before_the_turn_started():
    client, transport = ready(started=False)
    client.tick()
    assert not any(m.get("method") == "turn/interrupt" for m in transport.messages)
    with pytest.raises(A.AppServerProtocolError):
        client.send_interrupt("early")


@pytest.mark.parametrize("message", [
    {"jsonrpc": "2.0", "id": "approval-1", "result": {"decision": "decline"}},
    {"jsonrpc": "2.0", "id": "approval-1", "result": {"decision": "accept"}},
    {"jsonrpc": "2.0", "id": "approval-1", "error": {"code": 1, "message": "no"}},
    {"jsonrpc": "2.0", "id": 99, "method": "turn/interrupt", "params": IDS},
    {"jsonrpc": "2.0", "id": 99, "method": "turn/start", "params": {}},
])
def test_no_answer_no_hand_made_interrupt_no_second_turn(message):
    client, transport = ready()
    before = len(transport.messages)
    with pytest.raises(A.AppServerProtocolError):
        client._send(message)
    assert len(transport.messages) == before


def test_server_request_is_kept_unanswered_and_triggers_the_interrupt():
    client, transport = ready()
    client._dispatch({"jsonrpc": "2.0", "id": "approval-1", "method": "item/commandExecution/requestApproval",
                      "params": {**IDS, "itemId": "c"}})
    client.tick()
    assert transport.messages[-1]["method"] == "turn/interrupt"
    assert client.evidence.summary()["tool_activity"]["server_requests"] == ["item/commandExecution/requestApproval"]


# --------------------------------------------------------------------------- #
# evidence-level rules
# --------------------------------------------------------------------------- #

def evidence(started=True):
    e = S4Evidence("t")
    e.settings_verified = True
    e.bind_turn("r")
    if started:
        e.observe("turn/started", {"threadId": "t", "turn": {"id": "r", "status": "inProgress"}})
    return e


def interrupted(e, *, answer=True):
    e.mark_interrupt_sent("first_delta")
    if answer:
        e.record_interrupt_response()
    e.observe("turn/completed", {"threadId": "t", "turn": {"id": "r", "status": "interrupted"}})


def test_text_never_decides_and_is_never_kept():
    e = evidence()
    e.observe("item/started", {**IDS, "item": {"id": "m", "type": "agentMessage"}})
    for _ in range(50):
        e.observe("item/agentMessage/delta", {**IDS, "itemId": "m", "delta": SECRET + " I have stopped."})
    interrupted(e)
    s = e.summary()
    assert s["probe_verdict"] == "PASS" and SECRET not in json.dumps(s) and "stopped" not in json.dumps(s)
    assert s["text_format"]["delta_count"] == 50


def test_a_completed_message_saying_stopped_cannot_replace_the_terminal():
    e = evidence()
    e.observe("item/completed", {**IDS, "item": {"id": "m", "type": "agentMessage", "text": "Interrupted. Stopped."}})
    e.mark_interrupt_sent("first_delta")
    e.record_interrupt_response()
    s = e.summary()
    assert s["probe_verdict"] == "INCONCLUSIVE" and "terminal_not_matched" in s["reason_codes"]


def test_answer_without_terminal_is_not_a_stop():
    e = evidence()
    e.mark_interrupt_sent("first_delta")
    e.record_interrupt_response()
    s = e.summary()
    assert not s["terminal_confirmed"] and s["probe_verdict"] == "INCONCLUSIVE"
    assert s["interrupt"]["response"] == "ok" and s["main_turn"]["matched"] is False


def test_interrupt_trigger_rules():
    e = evidence(started=False)
    assert e.interrupt_trigger(timer_expired=True) is None  # not started
    e.observe("turn/started", {"threadId": "t", "turn": {"id": "r", "status": "inProgress"}})
    assert e.interrupt_trigger() is None and e.interrupt_trigger(timer_expired=True) == "timer"
    e.observe("item/agentMessage/delta", {**IDS, "itemId": "m", "delta": "x"})
    assert e.interrupt_trigger() == "first_delta"
    e.mark_interrupt_sent("first_delta")
    assert e.interrupt_trigger(timer_expired=True) is None  # never twice
    assert e.mark_interrupt_sent("again") is False and "duplicate_interrupt_attempt" in e.summary()["reason_codes"]


def test_no_trigger_after_the_terminal():
    e = evidence()
    e.observe("turn/completed", {"threadId": "t", "turn": {"id": "r", "status": "completed"}})
    e.observe("thread/tokenUsage/updated", {**IDS, "tokenUsage": {"last": {
        "inputTokens": 1, "outputTokens": 1, "totalTokens": 2, "cachedInputTokens": 0, "reasoningOutputTokens": 0}}})
    assert e.interrupt_trigger(timer_expired=True) is None
    s = e.summary()
    assert "turn_completed_before_interrupt:completed" in s["reason_codes"] and not s["violation"]  # usage may trail


@pytest.mark.parametrize("late,code", [
    (("turn/completed", {"threadId": "t", "turn": {"id": "r", "status": "interrupted"}}), "duplicate_terminal"),
    (("item/agentMessage/delta", {**IDS, "itemId": "m", "delta": "late"}), "event_after_terminal"),
    (("item/completed", {**IDS, "item": {"id": "m", "type": "agentMessage", "text": "late"}}), "event_after_terminal"),
])
def test_duplicate_or_late_events_after_the_terminal_fail(late, code):
    e = evidence()
    interrupted(e)
    e.observe(*late)
    s = e.summary()
    assert s["probe_verdict"] == "FAIL" and code in s["reason_codes"]


@pytest.mark.parametrize("method,params,code", [
    ("turn/completed", {"threadId": "t", "turn": {"id": "other", "status": "interrupted"}}, "foreign_turn"),
    ("turn/completed", {"threadId": "other", "turn": {"id": "r", "status": "interrupted"}}, "foreign_thread"),
    ("item/agentMessage/delta", {"threadId": "t", "itemId": "m", "delta": "x"}, "unbound_event"),
])
def test_wrong_ids_fail(method, params, code):
    e = evidence()
    e.mark_interrupt_sent("first_delta")
    e.observe(method, params)
    assert code in e.summary()["reason_codes"] and e.summary()["probe_verdict"] == "FAIL"


def test_unbound_turn_events_are_rechecked_when_the_turn_binds():
    e = S4Evidence("t")
    e.observe("turn/started", {"threadId": "t", "turn": {"id": "foreign", "status": "inProgress"}})
    e.bind_turn("r")
    assert "foreign_turn" in e.summary()["reason_codes"]


@pytest.mark.parametrize("item_completed,state,code", [
    (True, "ended", "tool_activity_observed"),
    (False, "unknown", "tool_process_unknown"),
])
def test_tool_end_state_comes_from_the_tools_own_item(item_completed, state, code):
    e = evidence()
    e.observe("item/started", {**IDS, "item": {"id": "c", "type": "commandExecution", "status": "inProgress",
                                               "processId": "1"}})
    if item_completed:
        e.observe("item/completed", {**IDS, "item": {"id": "c", "type": "commandExecution", "status": "failed"}})
    interrupted(e)
    s = e.summary()
    assert s["main_turn"]["status"] == "interrupted"  # the main turn ended either way
    assert s["tool_activity"]["state"] == state and code in s["reason_codes"]
    assert s["probe_verdict"] == "INCONCLUSIVE"


def test_tool_output_without_an_item_is_unknown():
    e = evidence()
    e.observe("item/commandExecution/outputDelta", {**IDS, "itemId": "c", "delta": "PRIVATE"})
    interrupted(e)
    s = e.summary()
    assert s["tool_activity"]["state"] == "unknown" and "PRIVATE" not in json.dumps(s)


def test_interrupt_answer_error_is_recorded_with_code_only():
    e = evidence()
    e.mark_interrupt_sent("first_delta")
    e.record_interrupt_response(error_code=-32600)
    e.observe("turn/completed", {"threadId": "t", "turn": {"id": "r", "status": "completed"}})
    s = e.summary()
    assert s["interrupt"]["response"] == "error" and s["interrupt"]["response_code"] == -32600
    assert {"interrupt_rejected", "interrupt_not_effective:completed"} <= set(s["reason_codes"])


def test_provider_error_notification_blocks_pass_without_failing():
    e = evidence()
    e.observe("error", {**IDS, "error": {"message": "x"}})
    interrupted(e)
    s = e.summary()
    assert s["probe_verdict"] == "INCONCLUSIVE" and "provider_error" in s["reason_codes"]


# --------------------------------------------------------------------------- #
# local cleanup and the runner's verdict
# --------------------------------------------------------------------------- #

def test_descendants_follow_the_parent_chain_only():
    table = [(10, 1, "codex.exe"), (11, 10, "codex-code-mode-host.exe"), (12, 11, "node.exe"),
             (13, 1, "unrelated.exe"), (14, 14, "self-parent.exe")]
    assert S4.descendants(10, table) == [(11, "codex-code-mode-host.exe"), (12, "node.exe")]
    assert S4.descendants(99, table) == []


def base_result(**changes):
    result = {"passed": True, "probe_verdict": "PASS", "reason_codes": [], "client_reasons": [],
              "process_started": True, "process_stopped": True, "exit_code": 0, "workdir_empty": True,
              "workdir_removed": True, "turn_attempts": 1, "error_type": None, "reader_error_type": None,
              "process_tree": {"available": True, "descendants_before_close": ["helper.exe"],
                               "descendants_after_close": []}}
    result.update(changes)
    return result


def test_clean_local_cleanup_keeps_pass():
    result = base_result()
    S4.S4Client.finalize_result(result)
    assert result["probe_verdict"] == "PASS" and result["local_cleanup"]["descendants_before_close"] == ["helper.exe"]


@pytest.mark.parametrize("changes,verdict,code", [
    ({"process_tree": {"available": True, "descendants_before_close": ["helper.exe"],
                       "descendants_after_close": ["helper.exe"]}}, "INCONCLUSIVE", "descendants_survived_close"),
    ({"process_tree": {"available": False}}, "INCONCLUSIVE", "process_tree_unavailable"),
    ({"process_stopped": False}, "INCONCLUSIVE", "process_not_stopped"),
    ({"exit_code": 1}, "INCONCLUSIVE", "process_exit:1"),
    ({"workdir_empty": False, "workdir_removed": False}, "FAIL", "workdir_not_empty"),
    ({"client_reasons": ["outgoing_response_rejected"]}, "FAIL", "outgoing_response_rejected"),
])
def test_local_cleanup_is_judged_apart_from_the_main_turn(changes, verdict, code):
    # The runner's "passed" already reflects process/workdir checks; mirror that here.
    passed = not any(k in changes for k in ("process_stopped", "exit_code", "workdir_empty", "client_reasons"))
    result = base_result(passed=passed, **changes)
    S4.S4Client.finalize_result(result)
    assert result["probe_verdict"] == verdict and code in result["reason_codes"]


def test_process_tree_survivors_are_measured_around_close(monkeypatch):
    tables = iter([[(500, 1, "codex.exe"), (501, 500, "helper.exe"), (502, 500, "runner.exe")],
                   [(501, 999, "helper.exe")]])
    monkeypatch.setattr(S4, "process_table", lambda: next(tables))
    client, transport = ready()
    transport.pid = 500
    client.close()
    tree = client.evidence.process_tree
    assert tree == {"available": True, "descendants_before_close": ["helper.exe", "runner.exe"],
                    "descendants_after_close": ["helper.exe"]}


# --------------------------------------------------------------------------- #
# offline schema facts
# --------------------------------------------------------------------------- #

def write_capture(root, *, drop=None):
    root.mkdir(parents=True)
    v2 = {"definitions": {
        "TurnInterruptParams": {"properties": {"threadId": {}, "turnId": {}}, "required": ["threadId", "turnId"]},
        "TurnInterruptResponse": {"type": "object"},
        "TurnStatus": {"enum": ["completed", "interrupted", "failed", "inProgress"]},
        "TurnCompletedNotification": {"properties": {"threadId": {}, "turn": {}}},
        "Turn": {"properties": {"id": {}, "status": {}}}}}
    if drop:
        v2["definitions"].pop(drop)
    (root / "codex_app_server_protocol.v2.schemas.json").write_text(json.dumps(v2), encoding="utf-8")

    def tag(*methods):
        return {"oneOf": [{"properties": {"method": {"enum": [m]}}} for m in methods]}

    (root / "ClientRequest.json").write_text(json.dumps(tag("turn/start", "turn/interrupt")), encoding="utf-8")
    (root / "ServerNotification.json").write_text(json.dumps(tag(
        "turn/started", "turn/completed", "item/agentMessage/delta", "item/started", "item/completed",
        "serverRequest/resolved", "thread/tokenUsage/updated")), encoding="utf-8")
    return root


def test_schema_facts_from_an_existing_capture(tmp_path):
    assert S4.schema_facts(write_capture(tmp_path / "ok"))["compatible"] is True
    broken = S4.schema_facts(write_capture(tmp_path / "broken", drop="TurnInterruptParams"))
    assert broken["compatible"] is False and broken["missing"] == ["interrupt_params:threadId,turnId (required)"]
