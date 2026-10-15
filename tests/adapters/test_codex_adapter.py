"""Codex adapter tests - fully offline.

No real Codex process is launched anywhere in this file. The adapter is driven
through an injected ``runner`` that replays canned JSONL, so the event parsing,
store updates, gate enforcement and failure handling are all exercised without
spending quota or touching the network.

The JSONL shape used here is the one captured from the verified real probe
(docs/agent-codex-capabilities.md 5.5), so the fixtures are not invented.
"""

from __future__ import annotations

import json
import os

import pytest

from agenthub.contracts import InvocationSnapshot
from agenthub_adapters.codex import (
    CodexAdapter,
    CodexLaunchFailed,
    CodexRun,
    FakeCodexRunner,
    build_argv,
    build_env,
    file_identity,
    parse_events,
    success_script,
)
from agenthub_adapters.store import FAILED, FINISHED, RUNNING, ExecutionStore

VERIFIED_LINES = [
    '{"type":"thread.started","thread_id":"01a13a34-63e6-7cc2-85d1-ddfbbec0e2ce"}',
    '{"type":"turn.started"}',
    '{"type":"item.completed","item":{"id":"item_0","type":"agent_message","text":"OK"}}',
    '{"type":"turn.completed","usage":{"input_tokens":14481,"cached_input_tokens":12544,'
    '"cache_write_input_tokens":0,"output_tokens":5,"reasoning_output_tokens":0}}',
]
VERIFIED_CAPTURE = "\n".join(VERIFIED_LINES) + "\n"


def snapshot(text: str = "Reply with exactly: OK. Do not modify any files.") -> InvocationSnapshot:
    return InvocationSnapshot(
        agent_id="codex",
        task_input=text,
        parent_task_id=None,
        depth=0,
    )


def make_adapter(tmp_path, script=None, **kw) -> CodexAdapter:
    store = ExecutionStore(path=str(tmp_path / "codex-exec.db"), table="codex_executions")
    runner = FakeCodexRunner(script or success_script())
    return CodexAdapter(
        codex_path=kw.pop("codex_path", __file__),  # any existing file
        codex_home=str(tmp_path / "home"),
        workdir_root=str(tmp_path / "runs"),
        store=store,
        runner=runner,
        **kw,
    )


# --------------------------------------------------------------------------- #
# event parsing (pure function, exact fixture from the real probe)
# --------------------------------------------------------------------------- #


def test_parse_verified_capture():
    p = parse_events(VERIFIED_CAPTURE)
    assert p.thread_id == "01a13a34-63e6-7cc2-85d1-ddfbbec0e2ce"
    assert p.final_message == "OK"
    assert p.usage["input_tokens"] == 14481
    assert p.usage["cached_input_tokens"] == 12544
    assert p.usage["output_tokens"] == 5
    assert p.event_types == {
        "thread.started": 1,
        "turn.started": 1,
        "item.completed": 1,
        "turn.completed": 1,
    }
    assert p.turn_completed is True
    assert p.turn_failed is False
    assert p.errors == []


def test_parse_keeps_non_json_lines_instead_of_dropping_them():
    text = "Reading additional input from stdin...\n" + VERIFIED_CAPTURE
    p = parse_events(text)
    assert len(p.events) == 4
    assert any("Reading additional input" in l for l in p.unparsed_lines)


def test_parse_records_errors_and_reconnect_messages():
    text = "\n".join([
        '{"type":"thread.started","thread_id":"t1"}',
        '{"type":"error","message":"Reconnecting... 2/5 (workspace routing discovery failed)"}',
        '{"type":"turn.failed","error":{"message":"workspace routing discovery failed"}}',
    ])
    p = parse_events(text)
    assert p.turn_failed is True
    assert p.turn_completed is False
    assert len(p.errors) == 2
    assert "Reconnecting" in p.errors[0]


def test_parse_empty_output_is_not_a_success():
    p = parse_events("")
    assert p.events == []
    assert p.thread_id == ""
    assert p.turn_completed is False


# --------------------------------------------------------------------------- #
# argv / env build: the safety posture is asserted, not assumed
# --------------------------------------------------------------------------- #


def test_build_argv_enforces_read_only_and_no_subagents():
    argv = build_argv("codex.exe", "hi", "C:/wd")
    assert argv[0] == "codex.exe"
    assert argv[1] == "exec"
    assert "--json" in argv
    assert "--disable" in argv and "multi_agent" in argv
    assert "--sandbox" in argv and "read-only" in argv
    assert "--skip-git-repo-check" in argv
    assert argv[-3] == "-C" and argv[-2] == "C:/wd"
    assert argv[-1] == "hi"
    # the dangerous bypass flags must never appear
    joined = " ".join(argv)
    assert "dangerously-bypass" not in joined


def test_build_argv_no_subagent_flag_when_disabled_option_off():
    argv = build_argv("codex.exe", "hi", "wd", disable_subagents=False)
    assert "multi_agent" not in argv


def test_build_env_sets_proxy_and_ca_without_touching_the_system():
    base = {"PATH": "x", "USERPROFILE": "y"}
    env = build_env(
        codex_home="HOME",
        proxy="http://127.0.0.1:8090",
        ca_bundle="cacert.pem",
        base_env=base,
    )
    assert env["CODEX_HOME"] == "HOME"
    assert env["HTTPS_PROXY"] == "http://127.0.0.1:8090"
    assert env["https_proxy"] == "http://127.0.0.1:8090"
    assert env["HTTP_PROXY"] == "http://127.0.0.1:8090"
    assert env["NO_PROXY"] == "localhost,127.0.0.1"
    assert env["CODEX_CA_CERTIFICATE"] == "cacert.pem"
    # the caller's dict is not mutated (process-scoped only)
    assert "CODEX_HOME" not in base
    assert "HTTPS_PROXY" not in base


# --------------------------------------------------------------------------- #
# the gate: the central safety requirement
# --------------------------------------------------------------------------- #


def test_start_without_gate_token_launches_nothing(tmp_path):
    a = make_adapter(tmp_path)
    result = a.start("inv-1", snapshot())          # no gate_token
    assert result.ok is False
    assert "no kernel gate token" in result.detail
    assert a.start_count == 0, "process was reached without a gate token"
    assert a.runner.calls == [], "runner was invoked without a gate token"
    assert a.store.get("inv-1") is None


def test_start_with_gate_token_runs_once(tmp_path):
    a = make_adapter(tmp_path)
    result = a.start("inv-1", snapshot(), gate_token="consumed:appr-1:inv-1")
    assert result.ok is True
    assert a.start_count == 1
    assert len(a.runner.calls) == 1
    assert result.adapter_invocation_id == "01a13a34-63e6-7cc2-85d1-ddfbbec0e2ce"


def test_identity_mismatch_refuses_to_launch(tmp_path):
    a = make_adapter(tmp_path, expected_identity={"size": 1, "sha256": "deadbeef"})
    result = a.start("inv-1", snapshot(), gate_token="consumed:appr-1:inv-1")
    assert result.ok is False
    assert result.detail.startswith("refused:")
    assert "mismatch" in result.detail
    assert a.start_count == 0 and a.runner.calls == []


def test_identity_matches_for_real_file(tmp_path):
    a = make_adapter(tmp_path, expected_identity=file_identity(__file__))
    ok, why = a.identity_matches()
    assert ok, why


# --------------------------------------------------------------------------- #
# result plumbing
# --------------------------------------------------------------------------- #


def test_success_records_thread_id_reply_and_usage(tmp_path):
    a = make_adapter(tmp_path)
    a.start("inv-1", snapshot(), gate_token="tok")
    rec = a.store.get("inv-1")
    assert rec["status"] == FINISHED
    payload = rec["payload"]
    assert payload["thread_id"] == "01a13a34-63e6-7cc2-85d1-ddfbbec0e2ce"
    assert payload["final_message"] == "OK"
    assert payload["usage"]["input_tokens"] == 14481
    assert payload["usage"]["output_tokens"] == 5
    assert payload["workdir_entries_after"] == []
    last = a.last_result("inv-1")
    assert last["thread_id"] == "01a13a34-63e6-7cc2-85d1-ddfbbec0e2ce"
    assert last["usage"]["output_tokens"] == 5


def test_success_fills_the_execution_id_in_the_adapters_own_record(tmp_path):
    """The adapter's record must name the external execution it refers to.

    ``record_start`` runs before the launch, when the external id does not exist,
    so the column used to stay empty - leaving the adapter's store unable to say
    WHICH Codex thread a record was about, while the kernel row had the id.
    """
    a = make_adapter(tmp_path)
    result = a.start("inv-1", snapshot(), gate_token="tok")

    rec = a.store.get("inv-1")
    assert rec["adapter_invocation_id"] == "01a13a34-63e6-7cc2-85d1-ddfbbec0e2ce"
    assert rec["adapter_invocation_id"] == result.adapter_invocation_id
    # The payload still carries it too, and the lookup agrees.
    assert rec["payload"]["thread_id"] == rec["adapter_invocation_id"]
    assert a.lookup_by_invocation("inv-1")["adapter_invocation_id"] == result.adapter_invocation_id


def test_unconfirmed_run_still_records_the_execution_id(tmp_path):
    """A timeout keeps the slot, so the record MUST say which thread to look for."""

    def timeout_script(argv, env):
        return CodexRun(
            None, True, 300.0, '{"type":"thread.started","thread_id":"t-timeout"}\n', argv
        )

    a = make_adapter(tmp_path, script=timeout_script)
    result = a.start("inv-1", snapshot(), gate_token="tok")

    assert result.terminal is False
    rec = a.store.get("inv-1")
    assert rec["status"] == RUNNING
    assert rec["adapter_invocation_id"] == "t-timeout", (
        "an unconfirmed run must record the id a human needs to investigate it"
    )


def test_launch_that_never_happened_records_no_execution_id(tmp_path):
    """No process, no id: the record must not invent one."""

    def refused(argv, env):
        raise CodexLaunchFailed("could not launch 'codex.exe': Access is denied")

    a = make_adapter(tmp_path, script=refused)
    result = a.start("inv-1", snapshot(), gate_token="tok")

    assert result.terminal is True
    rec = a.store.get("inv-1")
    assert not rec["adapter_invocation_id"]


def test_turn_failed_marks_failed_with_reason(tmp_path):
    def script(argv, env):
        text = "\n".join([
            '{"type":"thread.started","thread_id":"t9"}',
            '{"type":"turn.started"}',
            '{"type":"error","message":"workspace routing discovery failed"}',
            '{"type":"turn.failed","error":{"message":"workspace routing discovery failed"}}',
        ])
        return CodexRun(1, False, 20.1, text, argv)

    a = make_adapter(tmp_path, script=script)
    result = a.start("inv-1", snapshot(), gate_token="tok")
    assert result.ok is False
    assert "workspace routing discovery failed" in result.detail
    assert a.store.get("inv-1")["status"] == FAILED


def test_exit_zero_without_turn_completed_is_not_success(tmp_path):
    """Exit code alone must not be read as success."""

    def script(argv, env):
        return CodexRun(0, False, 0.1, '{"type":"thread.started","thread_id":"t"}\n', argv)

    a = make_adapter(tmp_path, script=script)
    result = a.start("inv-1", snapshot(), gate_token="tok")
    assert result.ok is False
    assert a.store.get("inv-1")["status"] == FAILED


# --------------------------------------------------------------------------- #
# requirement 4: timeout / unconfirmed cancel => unknown, slot kept
# --------------------------------------------------------------------------- #


def test_timeout_keeps_record_running_so_the_kernel_cannot_release(tmp_path):
    """A local kill is NOT proof the remote turn ended."""

    def script(argv, env):
        # Partial output, then the process had to be killed.
        return CodexRun(None, True, 300.0, '{"type":"thread.started","thread_id":"t1"}\n', argv)

    a = make_adapter(tmp_path, script=script)
    result = a.start("inv-1", snapshot(), gate_token="tok")
    assert result.ok is False
    assert result.terminal is False, "a timeout must not be reported as terminal"
    assert "timed out" in result.detail
    rec = a.store.get("inv-1")
    assert rec["status"] == RUNNING, "timeout must not look terminal to the kernel"
    assert "not confirmed stopped" in rec["detail"]


def test_cancel_is_reported_unsupported_not_faked(tmp_path):
    a = make_adapter(tmp_path)
    out = a.cancel_invocation("inv-1")
    assert out["ok"] is False
    assert out["reason"] == "unsupported"
    assert "unverified" in out["detail"]


def test_lookup_reflects_the_external_record(tmp_path):
    a = make_adapter(tmp_path)
    assert a.lookup_by_invocation("inv-1") is None
    a.start("inv-1", snapshot(), gate_token="tok")
    rec = a.lookup_by_invocation("inv-1")
    assert rec is not None and rec["status"] == FINISHED


# --------------------------------------------------------------------------- #
# capabilities tell the truth
# --------------------------------------------------------------------------- #


def test_capabilities_mark_unverified_items():
    caps = CodexAdapter.capabilities(CodexAdapter.__new__(CodexAdapter))
    assert caps["post_message"] is True
    assert caps["stream"] is True
    assert caps["real_usage"] is True
    # never claim what was not demonstrated
    assert caps["cancel"] is False
    assert caps["intercept_tool"] is False
    assert caps["limit_dir"] == "unknown"
    assert caps["control_subagent"] is False  # observed native spawn despite disable flag


# --------------------------------------------------------------------------- #
# agent identity matching (regression)
# --------------------------------------------------------------------------- #


def test_start_refuses_when_agent_id_does_not_match(tmp_path):
    """A call registered for another agent must not launch Codex.

    Without this, an invocation created as e.g. agent_id='mock' would quietly
    boot a real Codex process - executing something the user never approved.
    """
    a = make_adapter(tmp_path)
    wrong = InvocationSnapshot(
        agent_id="mock", task_input="hi", parent_task_id=None, depth=0
    )
    result = a.start("inv-1", wrong, gate_token="tok")

    assert result.ok is False
    assert result.terminal is True
    assert "agent_id" in result.detail and "not served" in result.detail
    assert a.start_count == 0, "a mismatched agent id still reached the process"
    assert a.runner.calls == []
    assert a.store.get("inv-1") is None


@pytest.mark.parametrize("agent_id", ["", "  ", "Codex", "codexx", "claude"])
def test_start_refuses_various_non_matching_agent_ids(tmp_path, agent_id):
    a = make_adapter(tmp_path)
    snap = InvocationSnapshot(agent_id=agent_id, task_input="x", parent_task_id=None, depth=0)
    result = a.start("inv-1", snap, gate_token="tok")
    assert result.ok is False
    assert a.start_count == 0


def test_start_accepts_the_declared_agent_id(tmp_path):
    a = make_adapter(tmp_path)
    ok_snap = InvocationSnapshot(agent_id="codex", task_input="x", parent_task_id=None, depth=0)
    assert a.start("inv-1", ok_snap, gate_token="tok").ok is True
    assert a.start_count == 1


def test_agent_ids_declared_on_the_adapter():
    assert CodexAdapter.AGENT_IDS == frozenset({"codex"})


# --------------------------------------------------------------------------- #
# abnormal terminal states (regression)
# --------------------------------------------------------------------------- #


def _run(exit_code, text, timed_out=False):
    def script(argv, env):
        return CodexRun(exit_code, timed_out, 1.0, text, argv)

    return script


def test_turn_started_without_any_outcome_is_unconfirmed(tmp_path):
    """THE abnormal end: the work began and we never learned its result.

    Treating this as a terminal failure would free a slot for work that may still
    be running; it must stay unconfirmed instead.
    """
    text = '{"type":"thread.started","thread_id":"t1"}\n{"type":"turn.started"}\n'
    a = make_adapter(tmp_path, script=_run(0, text))
    result = a.start("inv-1", snapshot(), gate_token="tok")

    assert result.ok is False
    assert result.terminal is False, "an unreported turn outcome must not be terminal"
    assert "abnormal end" in result.detail
    assert a.store.get("inv-1")["status"] == RUNNING


def test_turn_completed_but_nonzero_exit_is_contradictory(tmp_path):
    """Conflicting evidence: the turn says done, the process says failure."""
    text = "\n".join(VERIFIED_LINES) + "\n"
    a = make_adapter(tmp_path, script=_run(3, text))
    result = a.start("inv-1", snapshot(), gate_token="tok")

    assert result.ok is False
    assert result.terminal is False, "contradictory evidence must not be settled"
    assert "contradictory" in result.detail
    assert a.store.get("inv-1")["status"] == RUNNING


def test_turn_failed_is_a_definitive_failure(tmp_path):
    text = "\n".join([
        '{"type":"thread.started","thread_id":"t1"}',
        '{"type":"turn.started"}',
        '{"type":"turn.failed","error":{"message":"boom"}}',
    ]) + "\n"
    a = make_adapter(tmp_path, script=_run(1, text))
    result = a.start("inv-1", snapshot(), gate_token="tok")

    assert result.ok is False
    assert result.terminal is True
    assert "boom" in result.detail
    assert a.store.get("inv-1")["status"] == FAILED


def test_no_events_at_all_is_failed_and_flagged_abnormal(tmp_path):
    a = make_adapter(tmp_path, script=_run(0, ""))
    result = a.start("inv-1", snapshot(), gate_token="tok")

    assert result.ok is False
    assert result.terminal is True
    assert "abnormal end" in result.detail and "no events" in result.detail
    assert a.store.get("inv-1")["status"] == FAILED


def test_success_path_is_terminal(tmp_path):
    a = make_adapter(tmp_path)
    result = a.start("inv-1", snapshot(), gate_token="tok")
    assert result.ok is True and result.terminal is True
    assert a.store.get("inv-1")["status"] == FINISHED


def test_classify_outcome_matrix():
    """The decision table, asserted directly."""
    from agenthub_adapters.codex import classify_outcome

    ok = parse_events(VERIFIED_CAPTURE)
    started_only = parse_events('{"type":"turn.started"}\n')
    failed = parse_events('{"type":"turn.started"}\n{"type":"turn.failed"}\n')
    empty = parse_events("")

    assert classify_outcome(CodexRun(0, False, 1, "", []), ok)[:2] == (FINISHED, True)
    assert classify_outcome(CodexRun(9, False, 1, "", []), ok)[:2] == (RUNNING, False)
    assert classify_outcome(CodexRun(None, True, 1, "", []), ok)[:2] == (RUNNING, False)
    assert classify_outcome(CodexRun(0, False, 1, "", []), started_only)[:2] == (RUNNING, False)
    assert classify_outcome(CodexRun(1, False, 1, "", []), failed)[:2] == (FAILED, True)
    assert classify_outcome(CodexRun(0, False, 1, "", []), empty)[:2] == (FAILED, True)


# --------------------------------------------------------------------------- #
# the real subprocess machinery, using a stub "process" instead of Codex
# --------------------------------------------------------------------------- #


def test_subprocess_runner_captures_output_from_a_stub_process(tmp_path):
    """Exercises SubprocessCodexRunner without involving Codex.

    The stub is a tiny python script that prints the same JSONL. This proves the
    file-based stdout capture path works (pipes are denied in this sandbox).
    """
    import sys

    from agenthub_adapters.codex import SubprocessCodexRunner

    stub = tmp_path / "stub_codex.py"
    stub.write_text(
        "import json,sys\n"
        "sys.stdout.write(json.dumps({'type':'thread.started','thread_id':'stub-1'})+'\\n')\n"
        "sys.stdout.write(json.dumps({'type':'turn.started'})+'\\n')\n"
        "sys.stdout.write(json.dumps({'type':'item.completed','item':{'id':'i','type':"
        "'agent_message','text':'OK'}})+'\\n')\n"
        "sys.stdout.write(json.dumps({'type':'turn.completed','usage':{'input_tokens':7,"
        "'output_tokens':1}})+'\\n')\n",
        encoding="utf-8",
    )
    runner = SubprocessCodexRunner(scratch_dir=str(tmp_path / "scratch"))
    run = runner.run([sys.executable, str(stub)], dict(os.environ), timeout=60)

    assert run.ok, run.stdout_text
    parsed = parse_events(run.stdout_text)
    assert parsed.thread_id == "stub-1"
    assert parsed.final_message == "OK"
    assert parsed.usage == {"input_tokens": 7, "output_tokens": 1}


def test_subprocess_runner_enforces_timeout(tmp_path):
    import sys

    from agenthub_adapters.codex import SubprocessCodexRunner

    slow = tmp_path / "slow.py"
    slow.write_text("import time\nprint('{\"type\":\"turn.started\"}', flush=True)\ntime.sleep(60)\n",
                    encoding="utf-8")
    runner = SubprocessCodexRunner(scratch_dir=str(tmp_path / "scratch"))
    run = runner.run([sys.executable, str(slow)], dict(os.environ), timeout=2)

    assert run.timed_out is True
    assert run.ok is False
