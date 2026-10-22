"""S3 command scope (2026-10-22): the wrapped argv, strictly; never a substring.

Source facts (tag rust-v0.163.0-alpha.10.2) are cited in
docs/appserver-s3-2026-10-22-run2.md. Offline only: direct evidence calls or
the Python stdio peer (tools/demo/stub_appserver_s3.py). No Codex, no model.
"""
import importlib.util
import json
from pathlib import Path
import shlex

import pytest

from agenthub_adapters.appserver_s3_evidence import (
    COMMAND, S3Evidence, classify_command, resolve_session_shell, wrapped_forms)

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("s3_probe_scope_test", ROOT / "tools/probes/appserver_s3.py")
S3 = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(S3)

PS = r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"
SHELL = ("powershell", PS)
WRAPPED = shlex.join([PS, "-Command", COMMAND])
NOPROFILE = shlex.join([PS, "-NoProfile", "-Command", COMMAND])
#: What shlex's try_join produces for this argv: single-quoted words.
RUST_JOINED = "'" + PS + "' -Command 'cmd.exe /d /c \"echo S3_EXECUTED>S3_EXECUTED.txt\"'"
UTF8_PREFIX = "try { [Console]::OutputEncoding=[System.Text.Encoding]::UTF8 } catch {}\n"


# --------------------------------------------------------------------------- #
# the matcher
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("text,form", [
    (WRAPPED, "powershell_command"),
    (NOPROFILE, "powershell_noprofile_command"),
    (RUST_JOINED, "powershell_command"),
    # Another valid POSIX quoting of the very same argv.
    ('"C:\\\\Windows\\\\System32\\\\WindowsPowerShell\\\\v1.0\\\\powershell.exe" -Command '
     '"cmd.exe /d /c \\"echo S3_EXECUTED>S3_EXECUTED.txt\\""', "powershell_command"),
    # Windows paths are case-insensitive (which() may report PATHEXT's ".EXE").
    (shlex.join([PS.lower().replace(".exe", ".EXE"), "-Command", COMMAND]), "powershell_command"),
])
def test_legal_wrappings_match_exactly_one_derived_argv(text, form):
    assert classify_command(text, SHELL) == (form, "match")


def test_cmd_session_shell_has_only_its_own_form():
    shell = ("cmd", "cmd.exe")
    assert classify_command(shlex.join(["cmd.exe", "/c", COMMAND]), shell) == ("cmd_c", "match")
    assert classify_command(WRAPPED, shell)[0] is None


REWRITTEN = [
    COMMAND.replace(">S3_EXECUTED.txt", ">S3_OTHER.txt"),
    COMMAND.replace("cmd.exe", "CMD.EXE"),
    COMMAND.replace(" /d ", " "),
    COMMAND.replace("/c", "/C"),
    COMMAND.replace("echo S3_EXECUTED", "echo  S3_EXECUTED"),
    COMMAND + " ",
    " " + COMMAND,
    'cmd /d /c "echo S3_EXECUTED>S3_EXECUTED.txt"',
]
APPENDED = [
    COMMAND + " & del S3_EXECUTED.txt",
    COMMAND + " && whoami",
    COMMAND + "; Remove-Item S3_EXECUTED.txt",
    COMMAND + "\nRemove-Item S3_EXECUTED.txt",
    "Get-ChildItem; " + COMMAND,
    UTF8_PREFIX + COMMAND,  # the spawn-time form, never the approved argv
]


@pytest.mark.parametrize("inner", REWRITTEN + APPENDED)
def test_rewritten_or_extended_inner_command_is_rejected(inner):
    text = shlex.join([PS, "-Command", inner])
    assert classify_command(text, SHELL) == (None, "inner_command_mismatch")


@pytest.mark.parametrize("argv", [
    [PS, "-Command", COMMAND, "-NoExit"],
    [PS, "-Command", COMMAND, "&", "del", "S3_EXECUTED.txt"],
    [PS, "-command", COMMAND],
    [PS, "-NoProfile", "-NoProfile", "-Command", COMMAND],
    [PS, "-Command", "-NoProfile", COMMAND],
    [PS, "-NonInteractive", "-Command", COMMAND],
    [PS, "-ExecutionPolicy", "Bypass", "-Command", COMMAND],
    [PS, COMMAND],
])
def test_additional_or_reordered_arguments_are_rejected(argv):
    assert classify_command(shlex.join(argv), SHELL) == (None, "arguments_mismatch")


def test_unquoted_trailing_words_are_extra_arguments_not_part_of_the_script():
    assert classify_command(WRAPPED + " ; del S3_EXECUTED.txt", SHELL) == (None, "arguments_mismatch")
    assert classify_command(WRAPPED + " & whoami", SHELL) == (None, "arguments_mismatch")


@pytest.mark.parametrize("program", [
    "powershell.exe", "powershell",
    r"C:\Program Files\PowerShell\7\pwsh.exe",
    r"C:\Windows\SysWOW64\WindowsPowerShell\v1.0\powershell.exe",
    r"C:\Windows\System32\cmd.exe", "cmd.exe",
    PS + ".bak",
])
def test_other_outer_program_is_rejected(program):
    assert classify_command(shlex.join([program, "-Command", COMMAND]), SHELL) == (None, "program_mismatch")
    assert classify_command(shlex.join([program, "/c", COMMAND]), SHELL)[0] is None


@pytest.mark.parametrize("text,check", [
    (COMMAND, "unwrapped_fixed_command"),
    (None, "not_a_command_string"), ("", "not_a_command_string"), (["argv"], "not_a_command_string"),
    (WRAPPED + "\0", "not_a_command_string"),
    ("<command included NUL byte>", "program_mismatch"),  # shlex_join's NUL fallback
    ("'" + PS + " -Command x", "unparseable"),
])
def test_bare_malformed_or_unparseable_strings_are_rejected(text, check):
    assert classify_command(text, SHELL) == (None, check)


def test_no_string_that_merely_contains_the_fixed_command_is_accepted():
    """A substring test would have passed every one of these."""
    candidates = ([shlex.join([PS, "-Command", inner]) for inner in APPENDED]
                  + [shlex.join([PS, "-Command", COMMAND, "-NoExit"]), WRAPPED + " & whoami",
                     shlex.join([r"C:\Windows\System32\cmd.exe", "/c", COMMAND]), COMMAND])
    for text in candidates:
        assert COMMAND in text
        assert classify_command(text, SHELL)[0] is None, text


def test_session_shell_follows_codex_detection_order():
    def files(*present):
        lowered = {p.lower() for p in present}
        return lambda p: p.lower() in lowered

    env = {"PATH": "C:\\Tools;relative\\dir;D:\\PS\\", "PATHEXT": ".COM;.EXE"}
    ps_on_path = r"D:\PS\powershell.EXE"
    # pwsh on PATH wins over everything.
    assert resolve_session_shell(env, files(r"C:\Tools\pwsh.EXE", ps_on_path)) == ("powershell", r"C:\Tools\pwsh.EXE")
    # pwsh's fallback path comes before powershell on PATH.
    pwsh7 = r"C:\Program Files\PowerShell\7\pwsh.exe"
    assert resolve_session_shell(env, files(pwsh7, ps_on_path)) == ("powershell", pwsh7)
    # powershell on PATH before its fallback, in PATHEXT order; then the fallback; then cmd.exe.
    assert resolve_session_shell(env, files(ps_on_path, PS)) == ("powershell", ps_on_path)
    assert resolve_session_shell(env, files(r"D:\PS\powershell.COM", ps_on_path)) == ("powershell", r"D:\PS\powershell.COM")
    assert resolve_session_shell({"PATH": ""}, files(PS)) == ("powershell", PS)
    assert resolve_session_shell({"PATH": ""}, files()) == ("cmd", "cmd.exe")
    # Relative PATH entries are not searched.
    assert resolve_session_shell({"PATH": "relative", "PATHEXT": ".EXE"}, files(r"relative\pwsh.EXE")) == ("cmd", "cmd.exe")


# --------------------------------------------------------------------------- #
# the evidence chain, shaped like the second real run
# --------------------------------------------------------------------------- #

IDS = {"threadId": "t", "turnId": "r"}
ITEM = "exec-c7ef8bd6-02cc-40b5-b671-7df14479ecc2"  # the item id the second run reported


def item(status, *, command=WRAPPED, **changes):
    return {**IDS, "item": {"id": ITEM, "type": "commandExecution", "command": command, "commandActions": [],
                           "cwd": "C:\\w", "source": "agent", "status": status, "processId": None,
                           "exitCode": None, **changes}}


def approval(**changes):
    return {**IDS, "itemId": ITEM, "startedAtMs": 0, "command": WRAPPED, "cwd": "C:\\w", "kind": "command",
            "environmentId": "local", "availableDecisions": ["accept", "cancel"],
            "reason": "MODEL-WRITTEN-REASON", **changes}


def started(command=WRAPPED, **changes):
    e = S3Evidence("t", session_shell=SHELL)
    e.settings_verified = True
    e.bind_turn("r")
    e.observe("turn/started", {"threadId": "t", "turn": {"id": "r", "status": "inProgress"}})
    e.observe("item/started", item("inProgress", command=command, **changes))
    return e


def decline(e, completed=None):
    e.receive_approval("req", approval(), cwd_matches=True)
    e.record_response()
    e.record_response(written=True)
    e.observe("serverRequest/resolved", {"threadId": "t", "requestId": "req"})
    e.observe("item/completed", completed or item("declined"))


def finish(e):
    e.observe("item/started", {**IDS, "item": {"id": "m", "type": "agentMessage"}})
    e.observe("item/completed", {**IDS, "item": {"id": "m", "type": "agentMessage", "text": "DECLINED"}})
    e.observe("thread/tokenUsage/updated", {**IDS, "tokenUsage": {"last": {
        "inputTokens": 9, "outputTokens": 1, "totalTokens": 10, "cachedInputTokens": 0, "reasoningOutputTokens": 0}}})
    e.observe("turn/completed", {"threadId": "t", "turn": {"id": "r", "status": "completed"}})


def test_second_run_pending_item_is_now_in_scope_and_waits_for_the_approval():
    e = started()
    s = e.summary()
    assert not s["violation"] and s["approval"] is None and not s["execution_observed"]
    assert s["audit"][-1]["command_form"] == "powershell_command" and s["audit"][-1]["source"] == "agent"
    # The old rule compared the reported string with the bare command; that
    # shape is still out of scope and labeled, not silently accepted.
    old = started(command=COMMAND).summary()
    assert old["violation"] and "command_scope_mismatch" in old["reason_codes"]
    assert old["audit"][-1]["failed_checks"] == ["command:unwrapped_fixed_command"]


def test_full_chain_with_the_tested_build_shapes_passes_and_keeps_no_text():
    e = started()
    decline(e)
    finish(e)
    s = e.summary()
    assert s["probe_verdict"] == "PASS" and s["approval_chain"]["accepted"], s["reason_codes"]
    assert s["approval"]["environment"] == "local" and s["approval"]["decline_listed"] is False
    assert s["command_scope"]["session_shell_path"] == PS
    assert s["command_scope"]["match_rule"] == "shlex_split_exact_argv"
    raw = json.dumps(s)
    assert "MODEL-WRITTEN-REASON" not in raw and COMMAND not in raw and "S3_EXECUTED>" not in raw


@pytest.mark.parametrize("change,label", [
    ({"command": NOPROFILE}, "command_differs_from_item"),
    ({"command": shlex.join([PS, "-Command", COMMAND + " & whoami"])}, "command:inner_command_mismatch"),
    ({"command": COMMAND}, "command:unwrapped_fixed_command"),
    ({"command": None}, "command:not_a_command_string"),
    ({"environmentId": "remote"}, "environment"),
    ({"availableDecisions": "decline"}, "available_decisions"),
])
def test_approval_must_repeat_the_same_in_scope_command(change, label):
    e = started()
    e.receive_approval("req", approval(**change), cwd_matches=True)
    s = e.summary()
    assert e.request is None and "approval_scope_mismatch" in s["reason_codes"]
    assert label in s["audit"][-1]["failed_checks"]


@pytest.mark.parametrize("inner", [COMMAND + " & del S3_EXECUTED.txt", COMMAND.replace(">S3_EXECUTED.txt", ">x")])
def test_rewritten_or_appended_pending_command_fails_before_any_answer(inner):
    e = started(command=shlex.join([PS, "-Command", inner]))
    e.receive_approval("req", approval(command=shlex.join([PS, "-Command", inner])), cwd_matches=True)
    s = e.summary()
    assert s["probe_verdict"] == "FAIL" and e.request is None
    assert {"command_scope_mismatch", "approval_scope_mismatch"} <= set(s["reason_codes"])


@pytest.mark.parametrize("completed,label", [
    (item("declined", command=NOPROFILE), "command_changed"),
    (item("declined", source="unifiedExecStartup"), "source"),
    (item("declined", id="exec-other"), "item_id"),
])
def test_declined_completion_must_be_the_same_pending_command(completed, label):
    e = started()
    decline(e, completed)
    s = e.summary()
    assert s["probe_verdict"] == "FAIL" and "command_scope_mismatch" in s["reason_codes"]
    assert label in s["audit"][-1]["failed_checks"]


def test_real_execution_begin_is_execution_evidence_not_scope():
    e = started()
    e.observe("item/started", item("inProgress", source="unifiedExecStartup", processId="1"))
    s = e.summary()
    assert s["probe_verdict"] == "FAIL" and s["execution_observed"]


@pytest.mark.parametrize("changes,label", [
    ({"source": "userShell"}, "source"),
    ({"source": "unifiedExecInteraction"}, "source"),
])
def test_pending_item_from_another_source_is_out_of_scope(changes, label):
    s = started(**changes).summary()
    assert "command_scope_mismatch" in s["reason_codes"] and label in s["audit"][-1]["failed_checks"]


def test_second_command_item_is_out_of_scope():
    e = started()
    e.observe("item/started", {**IDS, "item": {**item("inProgress")["item"], "id": "exec-second"}})
    s = e.summary()
    assert "command_scope_mismatch" in s["reason_codes"]
    assert "not_first_command" in s["audit"][-1]["failed_checks"]


# --------------------------------------------------------------------------- #
# the real stdio peer
# --------------------------------------------------------------------------- #

def run(monkeypatch, tmp_path, scenario):
    monkeypatch.setattr(S3, "RUN_ROOT", tmp_path / "evidence")
    monkeypatch.setattr(S3.S2, "WORK_ROOT", tmp_path)
    monkeypatch.setattr(S3.S2.S1, "preflight", lambda *_: pytest.fail("offline only"))
    code = S3.run_probe(scenario=scenario)
    raw = next((tmp_path / "evidence").glob("*/result.json")).read_text(encoding="utf-8")
    assert "SYNTHETIC-REASON" not in raw and COMMAND not in raw
    return code, json.loads(raw)


@pytest.mark.parametrize("scenario", ["happy", "noprofile"])
def test_peer_wrapped_command_passes(monkeypatch, tmp_path, scenario):
    if scenario == "noprofile" and resolve_session_shell()[0] != "powershell":
        pytest.skip("no PowerShell on this host: only the cmd form exists")
    code, r = run(monkeypatch, tmp_path, scenario)
    assert code == 0 and r["probe_verdict"] == "PASS", r["reason_codes"]
    assert [m["decision"] for m in r["sent"][4:]] == ["decline"]
    assert r["command_scope"]["session_shell_path"] == resolve_session_shell()[1]


@pytest.mark.parametrize("scenario,code_", [
    ("bare-command", "command_scope_mismatch"),
    ("rewritten-command", "command_scope_mismatch"),
    ("appended-command", "command_scope_mismatch"),
    ("extra-argument", "command_scope_mismatch"),
    ("other-shell", "command_scope_mismatch"),
    ("approval-command-differs", "approval_scope_mismatch"),
    ("remote-environment", "approval_scope_mismatch"),
])
def test_peer_out_of_scope_commands_fail_without_any_answer(monkeypatch, tmp_path, scenario, code_):
    if scenario == "approval-command-differs" and resolve_session_shell()[0] != "powershell":
        pytest.skip("needs two PowerShell forms")
    code, r = run(monkeypatch, tmp_path, scenario)
    assert code == 1 and r["probe_verdict"] == "FAIL" and code_ in r["reason_codes"]
    assert [m["method"] for m in r["sent"]] == ["initialize", "initialized", "thread/start", "turn/start"]
    assert not r["decline_written"] and r["turn_attempts"] == 1 and r["process_stopped"]
