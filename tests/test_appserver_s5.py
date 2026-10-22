"""S5 resume probe: offline only (direct calls or the two-process Python stdio peer).

No Codex, no model, no credentials. Source facts (tag rust-v0.163.0-alpha.11)
are cited in docs/appserver-s5-probe.md.
"""
import importlib.util
import json
from pathlib import Path

import pytest

from agenthub_adapters import appserver as A
from agenthub_adapters.appserver_s5_evidence import (
    ALPHABET, RECALL_PROMPT, TurnEvidence, inspect_resume, judge, make_code_word)

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("s5_probe_under_test", ROOT / "tools/probes/appserver_s5.py")
S5 = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(S5)

CODE = "S5-K7QX2M9PLA"


def run(monkeypatch, tmp_path, scenario, *, plant_exit=(0, None)):
    monkeypatch.setattr(S5, "RUN_ROOT", tmp_path / "evidence")
    monkeypatch.setattr(S5.S2, "WORK_ROOT", tmp_path)
    monkeypatch.setattr(S5.S1, "preflight", lambda *_: pytest.fail("offline only"))
    code = S5.run_probe(scenario=scenario, _code_word=CODE)
    raw = next((tmp_path / "evidence").glob("*/result.json")).read_text(encoding="utf-8")
    assert CODE not in raw and "SYNTHETIC" not in raw, "neither the code word nor any reply text is stored"
    r = json.loads(raw)
    plant, recall = r["phases"].get("plant", {}), r["phases"].get("recall", {})
    # Invariants: two phases at most, one turn each, never an answer, never another thread.
    assert [m["method"] for m in plant.get("sent", [])][:3] == ["initialize", "initialized", "thread/start"]
    if recall:
        assert [m["method"] for m in recall["sent"]][:3] == ["initialize", "initialized", "thread/resume"]
        assert recall["pid"] != plant["pid"] and r["process_relation"]["first_process_exited_before_second"]
    assert sum(p.get("turn_attempts", 0) for p in (plant, recall)) <= 2
    assert plant.get("exit_code") in plant_exit and recall.get("exit_code") in (0, None), \
        "the peer exits 3 on an answer or a code word in the recall prompt"
    if plant:
        # The gate before B is exactly the final rule set applied to phase 1.
        assert r["plant_health"]["healthy"] == (not r["plant_health"]["blockers"])
        assert bool(recall) == r["plant_health"]["healthy"] or "first_process_alive_before_resume" in r["reason_codes"]
        assert "plant_health_inconsistent" not in r["reason_codes"]
    return code, r


@pytest.mark.parametrize("scenario,verdict,codes", [
    ("happy", "PASS", []),
    ("loose-recall", "PASS", []),
    ("forgetful", "INCONCLUSIVE", ["context_not_recalled"]),
    ("no-history-returned", "INCONCLUSIVE", ["history_not_returned"]),
    ("history-without-message", "INCONCLUSIVE", ["planted_message_missing"]),
    ("wrong-thread-resume", "FAIL", ["recall:resume_thread_mismatch"]),
    ("resume-error", "INCONCLUSIVE", ["resume_rejected", "recall:request_rejected"]),
    ("resume-writable", "INCONCLUSIVE", ["recall:settings_mismatch"]),
    ("plant-failed", "INCONCLUSIVE", ["plant:turn_status:failed", "recall:not_run", "resume_not_attempted"]),
    ("plant-timeout", "INCONCLUSIVE", ["plant:timeout", "plant:terminal_not_matched", "recall:not_run"]),
    ("same-turn-id", "FAIL", ["recall_turn_reused"]),
    ("foreign-turn", "FAIL", ["recall:foreign_turn"]),
    ("late-event", "FAIL", ["recall:event_after_terminal"]),
    ("recall-tool", "INCONCLUSIVE", ["recall:tool_activity_observed", "recall:terminal_not_matched"]),
    ("recall-approval", "INCONCLUSIVE", ["recall:server_request_unanswered:item/commandExecution/requestApproval"]),
    ("recall-disconnect", "INCONCLUSIVE", ["recall:terminal_not_matched"]),
])
def test_every_scenario_has_its_verdict_and_reasons(monkeypatch, tmp_path, scenario, verdict, codes):
    code, r = run(monkeypatch, tmp_path, scenario)
    assert r["probe_verdict"] == verdict, r["reason_codes"]
    assert set(codes) <= set(r["reason_codes"]), r["reason_codes"]
    assert code == (0 if verdict == "PASS" else 1)


def test_happy_proves_history_and_recall_across_two_processes(monkeypatch, tmp_path):
    _, r = run(monkeypatch, tmp_path, "happy")
    plant, recall = r["phases"]["plant"], r["phases"]["recall"]
    assert recall["resume"]["history_returned"] and recall["resume"]["planted_turn_status"] == "completed"
    assert recall["recall"] == {"contains": True, "exact": True}
    assert recall["thread_id"] == plant["thread_id"] and recall["turn_id"] != plant["turn_id"]
    assert r["recall_prompt_contains_code_word"] is False and r["code_word_length"] == len(CODE)
    assert r["workdir_removed"] and r["process_relation"]["same_pid"] is False
    assert plant["process_tree"]["descendants_after_close"] == recall["process_tree"]["descendants_after_close"] == []


def test_wording_around_the_code_word_is_recorded_but_does_not_decide(monkeypatch, tmp_path):
    _, r = run(monkeypatch, tmp_path, "loose-recall")
    assert r["phases"]["recall"]["recall"] == {"contains": True, "exact": False}


def test_resume_success_alone_is_never_a_pass(monkeypatch, tmp_path):
    _, r = run(monkeypatch, tmp_path, "forgetful")
    resume = r["phases"]["recall"]["resume"]
    assert resume["thread_id_matches"] and resume["history_returned"]  # resume looked perfect
    assert r["phases"]["recall"]["evidence"]["status"] == "completed"
    assert r["probe_verdict"] == "INCONCLUSIVE" and r["phases"]["recall"]["recall"]["contains"] is False


@pytest.mark.parametrize("scenario", ["resume-error", "resume-writable"])
def test_no_recall_turn_without_a_valid_resume(monkeypatch, tmp_path, scenario):
    _, r = run(monkeypatch, tmp_path, scenario)
    assert "turn/start" not in [m["method"] for m in r["phases"]["recall"]["sent"]]


@pytest.mark.parametrize("scenario", ["plant-failed", "plant-timeout"])
def test_no_second_process_after_a_bad_plant(monkeypatch, tmp_path, scenario):
    _, r = run(monkeypatch, tmp_path, scenario)
    assert "recall" not in r["phases"]


# --------------------------------------------------------------------------- #
# full phase-1 health check before process B (counterexamples)
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("scenario,blocker,plant_exit", [
    ("plant-provider-error", "plant:provider_error", (0,)),
    ("plant-exit-nonzero", "plant:process_exit:1", (1,)),
    ("plant-tool", "plant:tool_activity_observed", (0,)),
])
def test_unhealthy_phase_one_never_starts_process_b(monkeypatch, tmp_path, scenario, blocker, plant_exit):
    _, r = run(monkeypatch, tmp_path, scenario, plant_exit=plant_exit)
    assert "recall" not in r["phases"] and r["process_relation"] is None
    assert r["plant_health"]["healthy"] is False and blocker in r["plant_health"]["blockers"]
    assert r["probe_verdict"] == "INCONCLUSIVE" and {blocker, "resume_not_attempted"} <= set(r["reason_codes"])


@pytest.mark.parametrize("tree,blocker", [
    ({"available": True, "descendants_before_close": ["helper.exe"], "descendants_after_close": ["helper.exe"]},
     "plant:descendants_survived_close"),
    ({"available": False}, "plant:process_tree_unavailable"),
])
def test_phase_one_local_cleanup_is_part_of_the_gate(monkeypatch, tmp_path, tree, blocker):
    original = S5.S5Client.close

    def close(self):
        original(self)
        if self.phase == "plant":
            self.process_tree = tree

    monkeypatch.setattr(S5.S5Client, "close", close)
    _, r = run(monkeypatch, tmp_path, "happy")
    assert "recall" not in r["phases"] and blocker in r["plant_health"]["blockers"]
    assert r["probe_verdict"] == "INCONCLUSIVE" and blocker in r["reason_codes"]


class Transport500:
    pid = 500

    def write(self, data):
        pass

    def close(self):
        pass


@pytest.mark.parametrize("afters,grace,survivors", [
    # mid-exit at the first look, gone at the second: not a survivor
    ([[(501, 999, "helper.exe")], []], 3.0, []),
    # still there when the grace period is over: a survivor
    ([[(501, 999, "helper.exe")]] * 50, 0.3, ["helper.exe"]),
])
def test_descendants_get_a_bounded_grace_period(monkeypatch, afters, grace, survivors):
    tables = iter([[(500, 1, "codex.exe"), (501, 500, "helper.exe")], *afters])
    monkeypatch.setattr(S5.S4, "process_table", lambda: next(tables))
    monkeypatch.setattr(S5, "CLOSE_GRACE_SECONDS", grace)
    c = S5.S5Client(Transport500(), workdir=str(ROOT), timeout=5, phase="plant", code_word=CODE)
    c.close()
    assert c.process_tree["available"] and c.process_tree["descendants_before_close"] == ["helper.exe"]
    assert c.process_tree["descendants_after_close"] == survivors


def test_phase_one_close_error_is_part_of_the_gate(monkeypatch, tmp_path):
    original = S5.S5Client.close

    def close(self):
        original(self)
        if self.phase == "plant":
            raise OSError("synthetic close failure")

    monkeypatch.setattr(S5.S5Client, "close", close)
    _, r = run(monkeypatch, tmp_path, "happy")
    assert "recall" not in r["phases"]
    assert {"plant:cleanup_error:OSError", "plant:process_not_stopped"} <= set(r["plant_health"]["blockers"])


def test_happy_phase_one_is_healthy_and_the_gate_matches_the_verdict(monkeypatch, tmp_path):
    _, r = run(monkeypatch, tmp_path, "happy")
    assert r["plant_health"] == {"healthy": True, "blockers": []} and r["probe_verdict"] == "PASS"


@pytest.mark.parametrize("phase,code", [
    ({}, "plant:not_run"),
    ({"evidence": {"settings_verified": False, "matched": True, "status": "completed", "one_turn_started": True},
      "process_stopped": True, "exit_code": 0, "turn_attempts": 1,
      "process_tree": {"available": True, "descendants_after_close": []}}, "plant:settings_not_verified"),
    ({"evidence": {"settings_verified": True, "matched": True, "status": "completed", "one_turn_started": False},
      "process_stopped": True, "exit_code": 0, "turn_attempts": 1,
      "process_tree": {"available": True, "descendants_after_close": []}}, "plant:turn_not_started_once"),
    ({"evidence": {"settings_verified": True, "matched": True, "status": "completed", "one_turn_started": True},
      "process_stopped": True, "exit_code": 0, "turn_attempts": 1, "reader_error_type": "AppServerProtocolError",
      "process_tree": {"available": True, "descendants_after_close": []}}, "plant:reader_error:AppServerProtocolError"),
    ({"evidence": {"settings_verified": True, "matched": True, "status": "completed", "one_turn_started": True},
      "process_stopped": True, "exit_code": 0, "turn_attempts": 2,
      "process_tree": {"available": True, "descendants_after_close": []}}, "plant:turn_attempts:2"),
])
def test_phase_problems_name_every_blocker(phase, code):
    from agenthub_adapters.appserver_s5_evidence import plant_health
    health = plant_health(phase)
    assert health["healthy"] is False and code in health["blockers"]


# --------------------------------------------------------------------------- #
# work directory cleanup is part of the verdict
# --------------------------------------------------------------------------- #

def test_failed_workdir_removal_prevents_pass(monkeypatch, tmp_path):
    original = Path.rmdir

    def rmdir(self, *args, **kwargs):
        if self.name.startswith("agenthub-s5-work-"):
            raise PermissionError("synthetic: directory in use")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, "rmdir", rmdir)
    _, r = run(monkeypatch, tmp_path, "happy")
    assert r["workdir_empty"] is True and r["workdir_removed"] is False
    assert r["probe_verdict"] == "INCONCLUSIVE" and "workdir_cleanup_failed:PermissionError" in r["reason_codes"]
    assert r["phases"]["recall"]["recall"]["contains"] is True  # everything else succeeded


def happy_result():
    phase = {"evidence": {"settings_verified": True, "matched": True, "status": "completed", "one_turn_started": True,
                          "usage_received": True, "reasons": [], "notes": [], "tool_activity": {"seen": False}},
             "process_stopped": True, "exit_code": 0, "turn_attempts": 1, "client_reasons": [],
             "process_tree": {"available": True, "descendants_after_close": []}}
    recall = {**phase, "turn_id": "turn-2", "recall": {"contains": True, "exact": True},
              "resume": {"rejected": False, "thread_id_matches": True, "history_returned": True}}
    return {"phases": {"plant": {**phase, "turn_id": "turn-1"}, "recall": recall},
            "plant_health": {"healthy": True, "blockers": []},
            "process_relation": {"first_process_exited_before_second": True, "same_pid": False},
            "workdir_empty": True, "workdir_removed": True}


def test_judge_needs_the_workdir_removed_and_a_consistent_gate():
    assert judge(happy_result()) == ("PASS", [])
    r = happy_result()
    r["workdir_removed"] = False
    assert judge(r) == ("INCONCLUSIVE", ["workdir_not_removed"])
    r = happy_result()
    r["workdir_removed"], r["cleanup_error_type"] = False, "PermissionError"
    assert judge(r) == ("INCONCLUSIVE", ["workdir_cleanup_failed:PermissionError"])
    r = happy_result()
    r["workdir_empty"] = None
    assert judge(r)[0] == "INCONCLUSIVE" and "workdir_unreadable" in judge(r)[1]
    r = happy_result()
    r["plant_health"] = {"healthy": True, "blockers": ["plant:something_else"]}
    assert judge(r) == ("INCONCLUSIVE", ["plant_health_inconsistent"])


@pytest.mark.parametrize("alive", [True, None])
def test_second_process_only_after_the_first_is_confirmed_gone(monkeypatch, tmp_path, alive):
    monkeypatch.setattr(S5, "pid_alive", lambda pid: alive)
    _, r = run(monkeypatch, tmp_path, "happy")
    assert "recall" not in r["phases"] and "first_process_alive_before_resume" in r["reason_codes"]
    assert r["probe_verdict"] == "INCONCLUSIVE"


# --------------------------------------------------------------------------- #
# real mode gate and identity
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("approved,risk", [(False, False), (True, False), (False, True)])
def test_real_mode_requires_both_acknowledgements_before_any_side_effect(monkeypatch, tmp_path, approved, risk):
    target = tmp_path / "absent"
    monkeypatch.setattr(S5, "RUN_ROOT", target)
    monkeypatch.setattr(S5.S1, "preflight", lambda *_: pytest.fail("must not touch the real home"))
    monkeypatch.setattr(S5.A, "StdioTransport", lambda *a, **kw: pytest.fail("must not launch"))
    assert S5.run_probe(real=True, approved=approved, acknowledge_risk=risk) == 2
    assert not target.exists()


def test_real_mode_preflights_the_pinned_alpha11_identity_and_only_the_happy_scenario(monkeypatch, tmp_path):
    seen = []

    def preflight(home, identity=None):
        seen.append(identity)
        raise S5.S1.CheckFailed("stop here: offline test")

    monkeypatch.setattr(S5, "RUN_ROOT", tmp_path / "runs")
    monkeypatch.setattr(S5.S2, "WORK_ROOT", tmp_path)
    monkeypatch.setattr(S5.S1, "preflight", preflight)
    monkeypatch.setattr(S5.A, "StdioTransport", lambda *a, **kw: pytest.fail("must not launch"))
    assert S5.run_probe(real=True, approved=True, acknowledge_risk=True, codex_home=str(tmp_path / "h")) == 1
    assert seen == [S5.S3.S3_CODEX_IDENTITY] and seen[0][0].endswith(r"bin\6257d9e8f63c0f2b\codex.exe")
    result = json.loads(next((tmp_path / "runs").glob("*/result.json")).read_text(encoding="utf-8"))
    assert result["reason_codes"] == ["preflight_failed"] and result["phases"] == {}
    with pytest.raises(ValueError):
        S5.run_probe(real=True, approved=True, acknowledge_risk=True, scenario="forgetful")


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


def client(phase="recall"):
    transport = RecordingTransport()
    c = S5.S5Client(transport, workdir=str(ROOT), timeout=5, phase=phase, code_word=CODE,
                    thread_id="t" if phase == "recall" else None, planted_turn_id="turn-1")
    c._send({"id": 1, "method": "initialize", "params": {"clientInfo": {"name": "agent-hub-s5", "version": "0.1.0"}}})
    c._dispatch({"id": 1, "result": {}})
    c.notify("initialized")
    return c, transport


@pytest.mark.parametrize("phase,message", [
    ("recall", {"jsonrpc": "2.0", "id": 2, "method": "thread/start", "params": {}}),
    ("recall", {"jsonrpc": "2.0", "id": 2, "method": "thread/resume", "params": {"threadId": "another"}}),
    ("plant", {"jsonrpc": "2.0", "id": 2, "method": "thread/resume", "params": {"threadId": "t"}}),
    ("recall", {"jsonrpc": "2.0", "id": "approval-1", "result": {"decision": "accept"}}),
    ("recall", {"jsonrpc": "2.0", "id": 2, "method": "turn/start", "params": {}}),
    ("recall", {"jsonrpc": "2.0", "id": 2, "method": "thread/fork", "params": {"threadId": "t"}}),
])
def test_only_the_fixed_sequence_can_be_sent(phase, message):
    c, transport = client(phase)
    before = len(transport.messages)
    with pytest.raises(A.AppServerProtocolError):
        c._send(message)
    assert len(transport.messages) == before


def test_recall_opening_is_exactly_the_planted_thread_with_the_fixed_settings():
    c, transport = client("recall")
    c._send({"jsonrpc": "2.0", "id": 2, "method": "thread/resume", "params": {
        "threadId": "t", "cwd": str(ROOT), "sandbox": "read-only", "approvalPolicy": "on-request",
        "approvalsReviewer": "user"}})
    assert transport.messages[-1]["method"] == "thread/resume"
    assert CODE not in RECALL_PROMPT and "S5-" not in RECALL_PROMPT


# --------------------------------------------------------------------------- #
# evidence-level rules
# --------------------------------------------------------------------------- #

def test_code_words_are_random_and_well_formed():
    words = {make_code_word() for _ in range(1000)}
    assert len(words) == 1000
    assert all(w.startswith("S5-") and len(w) == 13 and set(w[3:]) <= set(ALPHABET) for w in words)


def resume_result(**thread):
    base = {"id": "t", "turns": [{"id": "turn-1", "status": "completed", "items": [
        {"type": "userMessage", "content": [{"type": "text", "text": f"remember {CODE}"}]}]}]}
    return {"thread": {**base, **thread}}


@pytest.mark.parametrize("change,key,value", [
    ({}, "history_returned", True),
    ({"id": "other"}, "thread_id_matches", False),
    ({"turns": []}, "planted_turn_present", False),
    ({"turns": [{"id": "turn-1", "status": "failed", "items": [
        {"type": "userMessage", "content": [{"type": "text", "text": CODE}]}]}]}, "history_returned", False),
    ({"turns": [{"id": "turn-1", "status": "completed", "items": [
        {"type": "agentMessage", "text": CODE}]}]}, "planted_message_present", False),
    ({"turns": [{"id": "turn-9", "status": "completed", "items": [
        {"type": "userMessage", "content": [{"type": "text", "text": CODE}]}]}]}, "planted_turn_present", False),
])
def test_resume_response_is_inspected_structurally(change, key, value):
    facts = inspect_resume(resume_result(**change), thread_id="t", planted_turn_id="turn-1", code_word=CODE)
    assert facts[key] is value
    assert CODE not in json.dumps(facts)


def test_recall_reply_is_compared_in_memory_and_never_summarized():
    e = TurnEvidence("t", phase="recall")
    e.bind_turn("turn-2")
    e.observe("turn/started", {"threadId": "t", "turn": {"id": "turn-2", "status": "inProgress"}})
    e.observe("item/completed", {"threadId": "t", "turnId": "turn-2",
                                 "item": {"id": "m", "type": "agentMessage", "text": f"It was {CODE}"}})
    assert e.recall(CODE) == (True, False) and e.recall("S5-AAAAAAAAAA") == (False, False)
    assert CODE not in json.dumps(e.summary())


def test_tool_activity_stops_the_phase_at_once():
    e = TurnEvidence("t", phase="recall")
    e.bind_turn("turn-2")
    e.observe("turn/started", {"threadId": "t", "turn": {"id": "turn-2", "status": "inProgress"}})
    e.observe("item/started", {"threadId": "t", "turnId": "turn-2",
                               "item": {"id": "c", "type": "commandExecution", "processId": "1"}})
    assert e.stop_requested and e.summary()["tool_activity"]["seen"]


def test_more_than_two_turns_in_total_fails():
    r = {"phases": {"plant": {"turn_attempts": 2}, "recall": {"turn_attempts": 1}}, "process_relation": {}}
    verdict, codes = judge(r)
    assert verdict == "FAIL" and "turn_attempts_total:3" in codes


# --------------------------------------------------------------------------- #
# offline schema facts
# --------------------------------------------------------------------------- #

def write_capture(root, *, drop=None):
    root.mkdir(parents=True)
    defs = {
        "ThreadResumeParams": {"properties": {k: {} for k in ("threadId", "cwd", "approvalPolicy",
                                                              "approvalsReviewer", "sandbox", "excludeTurns")},
                               "required": ["threadId"]},
        "ThreadResumeResponse": {"required": ["thread", "cwd", "approvalPolicy", "approvalsReviewer", "sandbox"]},
        "Thread": {"required": ["id", "turns"]},
        "Turn": {"properties": {"id": {}, "status": {}, "items": {}}},
        "ThreadItem": {"oneOf": [{"properties": {"type": {"enum": ["userMessage"]}}},
                                 {"properties": {"type": {"enum": ["agentMessage"]}}}]}}
    if drop:
        defs.pop(drop)
    (root / "codex_app_server_protocol.v2.schemas.json").write_text(json.dumps({"definitions": defs}), encoding="utf-8")

    def tag(*methods):
        return {"oneOf": [{"properties": {"method": {"enum": [m]}}} for m in methods]}

    (root / "ClientRequest.json").write_text(json.dumps(tag("thread/resume", "turn/start")), encoding="utf-8")
    (root / "ServerNotification.json").write_text(json.dumps(tag(
        "turn/started", "turn/completed", "item/completed", "thread/tokenUsage/updated")), encoding="utf-8")
    return root


def test_schema_facts_from_an_existing_capture(tmp_path):
    assert S5.schema_facts(write_capture(tmp_path / "ok"))["compatible"] is True
    broken = S5.schema_facts(write_capture(tmp_path / "broken", drop="ThreadResumeParams"))
    assert broken["compatible"] is False and "resume_params:threadId (required)" in broken["missing"]
