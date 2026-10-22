"""S6-3 driver, offline only: two calls through the whole Hub path on the stdio stub.

The stub keeps a small thread store beside the logs so that the second stub
PROCESS can return the planted turn, like a cold resume. Real mode is exercised
only up to its refusals; every launch path is a sentinel there.
"""

import importlib.util
import json
import os
from pathlib import Path

import pytest

from agenthub_adapters import appserver as appserver_module
from agenthub_adapters.appserver_adapter import PINNED_APPSERVER

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("s6_3_probe_under_test", ROOT / "tools/probes/appserver_s6_3.py")
S63 = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(S63)

CODE = "S6-K7QX2M9PLA"


def run(tmp_path, **kwargs):
    kwargs.setdefault("_code", CODE)
    code = S63.run_probe(run_root=tmp_path / "evidence", work_root=tmp_path, **kwargs)
    [result_file] = list((tmp_path / "evidence").glob("*/result.json"))
    text = result_file.read_text(encoding="utf-8")
    assert CODE not in text  # only the code word's hash is ever written
    return code, json.loads(text)


def test_recall_through_the_hub_passes(tmp_path):
    exit_code, result = run(tmp_path, scenario="recall")
    assert exit_code == 0 and result["verdict"] == "PASS" and result["reason_codes"] == []
    plant, recall = result["plant"], result["recall"]
    assert result["model_turns"] == 2 and plant["launches"] == 1 and recall["launches"] == 1
    assert plant["record"]["opening_method"] == "thread/start"
    assert recall["record"]["opening_method"] == "thread/resume"
    assert recall["record"]["thread_id"] == plant["record"]["thread_id"]
    assert recall["record"]["turn_id"] != plant["record"]["turn_id"]
    assert recall["record"]["previous_turn_id"] == plant["record"]["turn_id"]
    assert recall["record"]["resume_check"]["previous_turn_present"] is True
    assert recall["record"]["resume_check"]["previous_turn_user_message"] is True
    assert recall["record"]["workdir"] == plant["record"]["workdir"]
    assert recall["record"]["input_sha256"] == S63.digest(S63.RECALL_PROMPT)
    # The planted turn's usage replay is recorded apart, not as the recall turn's usage.
    assert recall["record"]["replayed_usage"] is not None
    assert recall["record"]["usage"] != recall["record"]["replayed_usage"]
    assert result["process_exit"] == {"pid": plant["record"]["process_id"], "exited": True}
    assert result["recall_contains"] is True and result["recall_exact"] is True
    assert plant["held_slots"] == 0 and recall["held_slots"] == 0
    assert plant["status"] == recall["status"] == "succeeded"
    assert result["code_sha256"] == S63.digest(CODE)


def test_resume_that_works_but_forgets_is_inconclusive(tmp_path):
    exit_code, result = run(tmp_path, scenario="recall-forgetful")
    assert exit_code == 1 and result["verdict"] == "INCONCLUSIVE"
    assert result["reason_codes"] == ["recall:answer_missing"]
    assert result["recall"]["record"]["opening_method"] == "thread/resume"  # resume alone is not PASS


def test_tool_activity_stops_the_recall_claim_even_with_the_right_answer(tmp_path):
    exit_code, result = run(tmp_path, scenario="recall-tool")
    assert exit_code == 1 and result["verdict"] == "INCONCLUSIVE"
    assert result["reason_codes"] == ["recall:tool_activity"]
    assert result["recall_contains"] is True  # answered, but cannot count as a recall success


def test_native_child_stops_the_recall_claim_and_keeps_the_slot(tmp_path):
    exit_code, result = run(tmp_path, scenario="recall-native")
    assert exit_code == 1 and result["verdict"] == "INCONCLUSIVE"
    # No end evidence for the child: the call stays unknown with its slot held
    # (existing rule), so no reply is captured at all.
    assert {"recall:native_agent", "recall:status_unknown", "quota_slot_held"} <= set(result["reason_codes"])
    assert result["recall"]["status"] == "unknown" and result["recall_contains"] is False


def test_tool_activity_while_planting_stops_before_the_second_call(tmp_path):
    exit_code, result = run(tmp_path, scenario="plant-tool")
    assert exit_code == 1 and result["verdict"] == "INCONCLUSIVE"
    assert "plant:tool_activity" in result["reason_codes"]
    assert result["recall"] is None and result["model_turns"] == 1


def test_reused_turn_id_fails(tmp_path):
    exit_code, result = run(tmp_path, scenario="recall-same-turn-id")
    assert exit_code == 1 and result["verdict"] == "FAIL" and "recall:turn_id_reused" in result["reason_codes"]
    assert result["recall"]["record"]["turn_id_reused"] is True


def test_resume_of_the_wrong_thread_fails_before_any_turn(tmp_path):
    exit_code, result = run(tmp_path, scenario="recall-wrong-thread")
    assert exit_code == 1 and result["verdict"] == "FAIL"
    assert "recall:wrong_thread" in result["reason_codes"]
    assert "recall:refused_before_turn" in result["reason_codes"]
    assert result["model_turns"] == 1  # the refused resume spent no model turn


def test_a_second_replay_is_a_usage_failure(tmp_path):
    exit_code, result = run(tmp_path, scenario="recall-replay-twice")
    assert exit_code == 1 and result["verdict"] == "FAIL"
    assert "recall:replay_usage_miscounted" in result["reason_codes"]
    assert result["recall"]["record"]["usage_anomalies"] == 1


def test_call_without_resume_is_refused_before_it_runs(tmp_path):
    exit_code, result = run(tmp_path, scenario="recall", _recall_context="independent")
    assert exit_code == 1 and result["verdict"] == "FAIL" and result["reason_codes"] == ["recall:not_resumed"]
    assert result["recall"] is None and result["model_turns"] == 1


def test_first_process_still_alive_stops_before_the_second_call(tmp_path):
    exit_code, result = run(tmp_path, scenario="recall", _alive=lambda pid: True)
    assert exit_code == 1 and result["verdict"] == "INCONCLUSIVE"
    assert result["reason_codes"] == ["plant:process_not_exited"]
    assert result["recall"] is None and result["model_turns"] == 1


def test_unreadable_process_table_stops_before_the_second_call(tmp_path):
    exit_code, result = run(tmp_path, scenario="recall", _alive=lambda pid: None)
    assert result["reason_codes"] == ["plant:process_exit_unverifiable"] and result["recall"] is None


def test_code_word_in_the_second_input_is_never_sent(tmp_path):
    exit_code, result = run(tmp_path, scenario="recall", _recall_prompt=f"The code was {CODE}. Repeat it.")
    assert exit_code == 1 and result["reason_codes"] == ["recall:input_contains_code"]
    assert result["recall"] is None and result["model_turns"] == 1


def test_planting_text_reused_in_the_second_input_is_never_sent(tmp_path):
    exit_code, result = run(tmp_path, scenario="recall",
                            _recall_prompt="Earlier: " + S63.PLANT_PROMPT.format(code=CODE))
    assert result["reason_codes"] == ["recall:input_contains_code"] and result["recall"] is None


@pytest.mark.parametrize("scenario, contains, codes", [
    ("recall-failed", True, ["recall:status_failed", "recall:turn_not_completed"]),
    ("recall-failed-nocode", False, ["recall:status_failed", "recall:turn_not_completed",
                                     "recall:answer_missing"]),
])
def test_a_failed_recall_turn_never_passes(tmp_path, scenario, contains, codes):
    """Review finding, through the Hub: failed is not PASS even with the right code word."""
    exit_code, result = run(tmp_path, scenario=scenario)
    assert exit_code == 1 and result["verdict"] == "INCONCLUSIVE" and result["reason_codes"] == codes
    assert result["recall"]["status"] == "failed" and result["recall"]["record"]["turn_status"] == "failed"
    assert result["recall_contains"] is contains


def test_unavailable_descendant_check_in_call_1_stops_before_call_2(tmp_path, monkeypatch):
    from agenthub_adapters import proc_tree

    monkeypatch.setattr(proc_tree, "process_table", lambda: None)
    exit_code, result = run(tmp_path, scenario="recall")
    assert exit_code == 1 and result["verdict"] == "INCONCLUSIVE"
    assert result["reason_codes"] == ["plant:descendants_unverifiable"]
    assert result["plant"]["record"]["descendants"] is None
    assert result["recall"] is None and result["model_turns"] == 1


def test_unavailable_descendant_check_in_call_2_is_inconclusive(tmp_path, monkeypatch):
    from agenthub_adapters import proc_tree

    real_table = proc_tree.process_table
    state = {"off": False}
    monkeypatch.setattr(proc_tree, "process_table", lambda: None if state["off"] else real_table())

    def alive(pid):
        now = S63.process_alive(pid)
        if now is False:
            state["off"] = True  # the table becomes unreadable once call 1's exit is confirmed
        return now

    exit_code, result = run(tmp_path, scenario="recall", _alive=alive)
    assert exit_code == 1 and result["verdict"] == "INCONCLUSIVE"
    assert result["reason_codes"] == ["recall:descendants_unverifiable"]
    assert result["recall_contains"] is True and result["model_turns"] == 2  # right answer, still not PASS


def test_second_call_without_approval_does_not_run(tmp_path):
    exit_code, result = run(tmp_path, scenario="recall", _approve_recall=False)
    assert exit_code == 1 and result["verdict"] == "INCONCLUSIVE"
    assert result["reason_codes"] == ["recall:not_approved"]
    recall = result["recall"]
    assert recall["run_status"] == 409 and recall["launches"] == 0 and recall["status"] == "awaiting_approval"
    assert result["model_turns"] == 1


def test_real_mode_without_both_flags_is_refused(tmp_path, capsys):
    for flags in ({}, {"approved": True}, {"acknowledged": True}):
        assert S63.run_probe(real=True, run_root=tmp_path / "evidence", work_root=tmp_path, **flags) == 2
    assert list(tmp_path.iterdir()) == [] and "REFUSED" in capsys.readouterr().out


def test_real_mode_preflight_failure_launches_nothing(tmp_path, monkeypatch):
    launched = []
    monkeypatch.setattr(appserver_module, "StdioTransport",
                        lambda argv, **kw: launched.append(argv) or (_ for _ in ()).throw(OSError("sentinel")))
    monkeypatch.setitem(PINNED_APPSERVER, "path", str(tmp_path / "missing" / "codex.exe"))
    exit_code, result = run(tmp_path, real=True, approved=True, acknowledged=True,
                            codex_home=str(tmp_path / "home"))
    assert exit_code == 1 and result["reason_codes"][0].startswith("preflight:")
    assert launched == [] and result["plant"] is None and result["model_turns"] == 0
    assert not (Path(result["data_dir"]) / "agent-hub.db").exists()


# --------------------------------------------------------------------------- #
# judge, on crafted evidence
# --------------------------------------------------------------------------- #


def _phases():
    plant = {"record": {"thread_id": "t1", "turn_id": "a", "workdir": "W"}}
    recall = {"status": "succeeded", "held_slots": 0, "reply_contains_code": True, "input_contains_code": False,
              "record": {"opening_method": "thread/resume", "thread_id": "t1", "turn_id": "b",
                         "previous_turn_id": "a", "resume_check": {"previous_turn_present": True},
                         "input_sha256": S63.digest(S63.RECALL_PROMPT), "usage": {"inputTokens": 9},
                         "replayed_usage": {"inputTokens": 5}, "usage_anomalies": 0, "workdir": "W",
                         "turn_requested": True, "tool_process_state": "none", "turn_status": "completed",
                         "terminal": True, "local_process_exit_confirmed": True,
                         "descendants": {"before_close": [], "after_grace": []}}}
    return plant, recall


@pytest.mark.parametrize("change, code", [
    ({"opening_method": "thread/start"}, "recall:not_resumed"),
    ({"thread_id": "t9"}, "recall:wrong_thread"),
    ({"turn_id": "a"}, "recall:turn_id_reused"),
    ({"previous_turn_id": "z"}, "recall:previous_turn_mismatch"),
    ({"resume_check": {"previous_turn_present": False}}, "recall:history_not_confirmed"),
    ({"input_sha256": "0" * 64}, "recall:input_not_the_recall_prompt"),
    ({"usage": {"inputTokens": 5}}, "recall:replay_usage_miscounted"),  # equals the replay
    ({"usage": {}}, "recall:replay_usage_miscounted"),
    ({"workdir": "V"}, "recall:workdir_changed"),
])
def test_judge_fails_each_structural_violation(change, code):
    plant, recall = _phases()
    recall["record"].update(change)
    fail, _inconclusive = S63.recall_codes(plant, recall, recall_text=S63.RECALL_PROMPT)
    assert code in fail


def test_judge_passes_clean_evidence_and_keeps_answer_separate():
    plant, recall = _phases()
    assert S63.recall_codes(plant, recall, recall_text=S63.RECALL_PROMPT) == ([], [])
    assert S63.passes(recall, [], []) is True
    recall["reply_contains_code"] = False
    assert S63.recall_codes(plant, recall, recall_text=S63.RECALL_PROMPT) == ([], ["recall:answer_missing"])


@pytest.mark.parametrize("contains, expected", [
    (True, ["recall:status_failed", "recall:turn_not_completed"]),
    (False, ["recall:status_failed", "recall:turn_not_completed", "recall:answer_missing"]),
])
def test_judge_never_passes_a_failed_call(contains, expected):
    """Review finding: a failed call with the right code word must not pass."""
    plant, recall = _phases()
    recall["status"] = "failed"
    recall["record"].update(turn_status="failed", terminal=True)
    recall["reply_contains_code"] = contains
    fail, inconclusive = S63.recall_codes(plant, recall, recall_text=S63.RECALL_PROMPT)
    assert fail == [] and inconclusive == expected
    assert S63.passes(recall, fail, []) is False  # even with the codes ignored


@pytest.mark.parametrize("turn_status, terminal", [("interrupted", True), ("", False), ("completed", False)])
def test_judge_requires_this_turns_own_completed_terminal(turn_status, terminal):
    plant, recall = _phases()
    recall["record"].update(turn_status=turn_status, terminal=terminal)
    assert "recall:turn_not_completed" in S63.recall_codes(plant, recall, recall_text=S63.RECALL_PROMPT)[1]


@pytest.mark.parametrize("report", [None, {"before_close": [], "after_grace": None},
                                    {"before_close": None, "after_grace": []}, {}])
def test_judge_never_reads_an_unavailable_descendant_check_as_none(report):
    """Review finding: "checked and empty" and "check unavailable" are different."""
    plant, recall = _phases()
    recall["record"]["descendants"] = report
    assert S63.descendant_check(recall["record"]) == "unavailable"
    assert "recall:descendants_unverifiable" in S63.recall_codes(plant, recall, recall_text=S63.RECALL_PROMPT)[1]
    plant_phase = {"status": "succeeded", "reply_is_stored": True, "native_session_recorded": True,
                   "held_slots": 0, "record": {**recall["record"], "opening_method": "thread/start",
                                               "process_id": 7}}
    assert S63.plant_codes(plant_phase) == ["plant:descendants_unverifiable"]
    plant_phase["record"]["descendants"] = {"before_close": [], "after_grace": []}
    assert S63.plant_codes(plant_phase) == []


def test_code_words_are_random_and_not_in_the_recall_prompt():
    words = {S63.make_code_word() for _ in range(50)}
    assert len(words) == 50 and all(w.startswith("S6-") and len(w) == 13 for w in words)
    assert not any(w in S63.RECALL_PROMPT for w in words)
