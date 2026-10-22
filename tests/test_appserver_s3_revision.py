"""S3 judgment revision (2026-10-22): text is reported, never decisive; reason codes.

Offline only: the Python stdio peer (tools/demo/stub_appserver_s3.py) or direct
evidence calls. No Codex, no model, no credentials.
"""
import importlib.util
import json
from pathlib import Path
import shlex

import pytest

from agenthub_adapters.appserver_s3_evidence import S3Evidence, wrapped_forms

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("s3_probe_revision_test", ROOT / "tools/probes/appserver_s3.py")
S3 = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(S3)

#: Text the stub speaks; none of it may appear in the saved evidence.
STUB_TEXTS = ("I will ask for approval", "to run that command", "I can't run commands",
              "No approval was requested", "The command was declined", "so I did not run it",
              "SYNTHETIC-REASON")  # the approval's model-written reason


def run(monkeypatch, tmp_path, scenario):
    monkeypatch.setattr(S3, "RUN_ROOT", tmp_path / "evidence")
    monkeypatch.setattr(S3.S2, "WORK_ROOT", tmp_path)
    monkeypatch.setattr(S3.S2.S1, "preflight", lambda *_: pytest.fail("offline only"))
    code = S3.run_probe(scenario=scenario)
    path = next((tmp_path / "evidence").glob("*/result.json"))
    raw = path.read_text(encoding="utf-8")
    for text in STUB_TEXTS:
        assert text not in raw, "model text must never be saved"
    return code, json.loads(raw)


def test_explanation_then_approval_then_decline_passes(monkeypatch, tmp_path):
    code, r = run(monkeypatch, tmp_path, "explain-then-decline")
    assert code == 0 and r["probe_verdict"] == "PASS" and r["reason_codes"] == []
    assert r["approval_chain"]["accepted"] and r["terminal"]["matched"]
    assert r["terminal"]["status"] == "completed" and r["terminal"]["usage_received"]
    t = r["text_format"]
    assert (t["assistant_messages"], t["before_approval"], t["after_decline"]) == (2, 1, 1)
    assert t["final_message_exact_DECLINED"] and t["streams_match_text"] and t["delta_count"] == 5
    assert [m["decision"] for m in r["sent"][4:]] == ["decline"]


def test_reply_wording_is_recorded_separately_and_does_not_block_pass(monkeypatch, tmp_path):
    code, r = run(monkeypatch, tmp_path, "explain-loose-reply")
    assert r["probe_verdict"] == "PASS" and r["approval_chain"]["accepted"]
    assert r["text_format"]["final_message_exact_DECLINED"] is False
    assert r["final_reply"] is None  # never the model's own words


def test_no_approval_is_inconclusive_with_a_reason_and_no_abort(monkeypatch, tmp_path):
    code, r = run(monkeypatch, tmp_path, "never-approval")
    assert code == 1 and r["probe_verdict"] == "INCONCLUSIVE"
    assert "no_approval_request" in r["reason_codes"]
    # Not aborted on text: the turn was followed to its matched terminal event.
    assert r["terminal"]["matched"] and r["terminal"]["status"] == "completed"
    assert r["terminal_confirmed"] and not r["client_violation"] and not r["violation"]
    assert r["approval_chain"]["accepted"] is False
    assert r["approval_chain"]["one_scoped_command_request"] is False
    assert r["text_format"]["assistant_messages"] == 2 and r["text_format"]["before_approval"] == 2
    assert [m["method"] for m in r["sent"]] == ["initialize", "initialized", "thread/start", "turn/start"]


def test_execution_evidence_after_explanation_fails(monkeypatch, tmp_path):
    code, r = run(monkeypatch, tmp_path, "explain-then-executed")
    assert code == 1 and r["probe_verdict"] == "FAIL"
    assert "execution_observed" in r["reason_codes"] and r["execution_observed"]
    assert r["text_format"]["before_approval"] == 1  # the text before it is still only counted
    assert [m["method"] for m in r["sent"]] == ["initialize", "initialized", "thread/start", "turn/start"]


@pytest.mark.parametrize("scenario,verdict,codes", [
    ("happy", "PASS", []),
    ("selfreport", "INCONCLUSIVE", ["no_approval_request"]),
    ("executed", "FAIL", ["execution_observed"]),
    # The approval arrives before the turn id is bound; the mismatch is caught
    # at binding time and the decline is refused (nothing is sent).
    ("wrong-turn", "FAIL", ["command_ids_mismatch", "outgoing_response_rejected"]),
    ("wrong-item", "FAIL", ["approval_scope_mismatch"]),
    ("duplicate", "FAIL", ["duplicate_approval"]),
    ("no-resolved", "INCONCLUSIVE", ["request_not_resolved"]),
    ("failed-item", "INCONCLUSIVE", ["command_not_declined:failed"]),
    ("disconnect", "INCONCLUSIVE", ["terminal_not_matched"]),
    ("timeout", "INCONCLUSIVE", ["timeout", "terminal_not_matched"]),
    ("failed-turn", "INCONCLUSIVE", ["turn_status:failed"]),
    ("auto-review", "INCONCLUSIVE", ["settings_mismatch"]),
    ("writable-sandbox", "INCONCLUSIVE", ["settings_mismatch"]),
])
def test_every_scenario_names_its_reason(monkeypatch, tmp_path, scenario, verdict, codes):
    _, r = run(monkeypatch, tmp_path, scenario)
    assert r["probe_verdict"] == verdict
    assert set(codes) <= set(r["reason_codes"]), r["reason_codes"]
    if verdict != "PASS":
        assert r["reason_codes"], "a non-PASS must carry at least one reason code"


# --------------------------------------------------------------------------- #
# evidence-level details
# --------------------------------------------------------------------------- #

IDS = {"threadId": "t", "turnId": "r"}
SHELL = ("powershell", r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe")
WRAPPED = shlex.join(wrapped_forms(SHELL)["powershell_command"])


def started():
    e = S3Evidence("t", session_shell=SHELL)
    e.settings_verified = True
    e.bind_turn("r")
    e.observe("turn/started", {"threadId": "t", "turn": {"id": "r", "status": "inProgress"}})
    return e


def say(e, item_id, pieces, final=None):
    e.observe("item/started", {**IDS, "item": {"id": item_id, "type": "agentMessage"}})
    for piece in pieces:
        e.observe("item/agentMessage/delta", {**IDS, "itemId": item_id, "delta": piece})
    e.observe("item/completed", {**IDS, "item": {"id": item_id, "type": "agentMessage",
                                                "text": "".join(pieces) if final is None else final}})


def test_many_deltas_and_messages_are_counted_not_stored():
    e = started()
    for n in range(30):
        say(e, f"m{n}", [f"secret-{n}-"] * 40)
    summary = e.summary()
    assert summary["text_format"]["assistant_messages"] == 30
    assert summary["text_format"]["delta_count"] == 1200
    assert "secret-" not in json.dumps(summary)
    assert not summary["violation"]  # far beyond the old 512-event limit, no abort


def test_stream_text_mismatch_is_reported_not_judged():
    e = started()
    say(e, "m", ["abc"], final="abd")
    summary = e.summary()
    assert summary["text_format"]["streams_match_text"] is False
    assert not summary["violation"] and "abd" not in json.dumps(summary)


def test_other_tool_items_and_foreign_ids_stay_scope_violations():
    e = started()
    e.observe("item/started", {**IDS, "item": {"id": "w", "type": "webSearch"}})
    assert e.summary()["probe_verdict"] == "FAIL"
    assert "unexpected_item_type:webSearch" in e.summary()["reason_codes"]
    f = started()
    f.observe("item/agentMessage/delta", {"threadId": "t", "turnId": "foreign", "itemId": "x", "delta": "hi"})
    assert "foreign_turn" in f.summary()["reason_codes"] and f.summary()["probe_verdict"] == "FAIL"


def test_command_output_notification_is_execution_evidence():
    e = started()
    e.observe("item/started", {**IDS, "item": {"id": "cmd", "type": "commandExecution", "command": WRAPPED,
                                                "status": "inProgress", "processId": None, "exitCode": None}})
    e.observe("item/commandExecution/outputDelta", {**IDS, "itemId": "cmd", "delta": "x"})
    summary = e.summary()
    assert summary["probe_verdict"] == "FAIL" and summary["execution_observed"]
    assert "execution_output_observed" in summary["reason_codes"]
