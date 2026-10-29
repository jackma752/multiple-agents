"""Claude Code adapter, offline only.

Fixtures are hand-written from the documentation (not captured): the in-process
FakeClaudeRunner and the bundled stdio stub (tools/demo/stub_claude_code.py).
A fake file stands in for the pinned executable; any real launch path is
replaced by a sentinel. No Claude Code process, model call or credential.
"""

import hashlib
import json
import math
import os
import subprocess

import pytest

from agenthub.contracts import InvocationSnapshot, execution_admission
from agenthub_adapters import claude_code as cc
from agenthub_adapters.claude_code import (
    CLAUDE_ARGV_TAIL, PINNED_CLAUDE_CODE, REAL_BLOCK_REASON, STUB_SCENARIOS, SUPERVISED_REASON,
    ClaudeCodeAdapter, ClaudeCodeLauncher, ClaudeRun, FakeClaudeRunner, ProcessRunner,
    StubClaudeLauncher, build_env, classify, parse_result, result_object, scripted,
)
from agenthub_adapters.store import ExecutionStore

PROMPT = "--dangerously-skip-permissions /loop say hi"  # flag- and command-shaped on purpose


def snap(adapter, text=PROMPT, **extra):
    return InvocationSnapshot(agent_id="claude-code", task_input=text, parent_task_id=None, depth=0,
                              adapter_version=adapter.binding_identity(), **extra)


def fake_adapter(tmp_path, script=None, launcher=None, **kw):
    return ClaudeCodeAdapter(
        launcher=launcher or StubClaudeLauncher("success-usage"),
        workdir_root=str(tmp_path / "runs"),
        store=ExecutionStore(str(tmp_path / "cc.db"), table="claude_code_executions"),
        runner=FakeClaudeRunner(script or scripted(result_object())), **kw)


def stub_adapter(tmp_path, scenario, **kw):
    marker = tmp_path / "marker.jsonl"
    adapter = ClaudeCodeAdapter(
        launcher=StubClaudeLauncher(scenario, config_dir=str(tmp_path / "cfg"), marker=str(marker)),
        workdir_root=str(tmp_path / "runs"),
        store=ExecutionStore(str(tmp_path / "cc.db"), table="claude_code_executions"), **kw)
    return adapter, marker


def payload(adapter, inv):
    return adapter.store.get(inv)["payload"]


# --------------------------------------------------------------------------- #
# result parsing and classification (documented shape)
# --------------------------------------------------------------------------- #


def run_of(exit_code=0, **kw):
    return ClaudeRun(exit_code=exit_code, timed_out=kw.pop("timed_out", False), elapsed=0.1,
                     stdout_text="", argv=[], **kw)


@pytest.mark.parametrize("obj, exit_code, verdict, terminal", [
    (result_object(), 0, "finished", True),
    (result_object(with_usage=False), 0, "finished", True),
    (result_object(is_error=True, reply="Not logged in"), 1, "failed", True),
    (result_object(subtype="error_max_turns", is_error=True, reply=None), 1, "failed", True),
    (result_object(subtype="error_during_execution", is_error=True, reply=None), 0, "failed", True),
    (result_object(), 3, "running", False),  # success + non-zero exit: contradictory
])
def test_classification_of_result_objects(obj, exit_code, verdict, terminal):
    parsed = parse_result(json.dumps(obj))
    assert parsed.result is not None
    assert classify(run_of(exit_code), parsed)[:2] == (verdict, terminal)


@pytest.mark.parametrize("text", [
    "not json", '{"type": "result", "subtype": "success", "result": "O',
    json.dumps({"type": "assistant"}), json.dumps([1, 2]),
    json.dumps({"type": "result", "subtype": "success", "is_error": "no", "result": "OK"}),
    json.dumps({"type": "result", "subtype": "success", "is_error": False}),  # success without reply
    json.dumps(result_object()) + "\n" + json.dumps(result_object()),  # two objects
])
@pytest.mark.parametrize("exit_code", [0, 1])
def test_unreadable_output_is_unconfirmed(text, exit_code):
    parsed = parse_result(text)
    assert parsed.result is None and parsed.malformed
    assert classify(run_of(exit_code), parsed)[:2] == ("running", False)


def test_empty_output_depends_on_the_exit_code():
    empty = parse_result("  \n")
    assert classify(run_of(1), empty)[:2] == ("failed", True)
    assert classify(run_of(0), empty)[:2] == ("running", False)


def test_timeout_and_local_stop_are_never_terminal():
    good = parse_result(json.dumps(result_object()))
    assert classify(run_of(None, timed_out=True), good)[:2] == ("running", False)
    assert classify(run_of(0, local_stop_requested=True), good)[:2] == ("running", False)


def test_usage_keeps_documented_numbers_only():
    usage = parse_result(json.dumps(result_object())).usage
    assert usage == {"input_tokens": 12, "output_tokens": 3, "cache_creation_input_tokens": 0,
                     "cache_read_input_tokens": 0}
    assert parse_result(json.dumps(result_object(with_usage=False))).usage == {}


# --------------------------------------------------------------------------- #
# start() through the in-process fake
# --------------------------------------------------------------------------- #


def test_success_records_reply_usage_and_session(tmp_path):
    adapter = fake_adapter(tmp_path)
    result = adapter.start("inv-1", snap(adapter), gate_token="t")
    assert result.ok and result.terminal and result.adapter_invocation_id == "5f0c4d3e-1a2b-4c5d-8e9f-000000000001"
    record = adapter.store.get("inv-1")
    assert record["status"] == "finished"
    p = record["payload"]
    assert p["final_message"] == "OK" and p["usage"]["output_tokens"] == 3
    assert p["cost_usd_estimate"] == 0.0123 and p["models"] == ["claude-model"]
    assert adapter.last_result("inv-1") == {"status": "finished", "thread_id": result.adapter_invocation_id,
                                            "final_message": "OK", "usage": p["usage"]}
    assert "native_session" not in adapter.last_result("inv-1")


def test_the_rest_of_the_result_keeps_numbers_flags_and_ids_only(tmp_path):
    obj = {**result_object(reply="REPLY-TEXT"), "num_turns": 2, "stop_reason": "end_turn",
           "fast_mode": False, "notes": ["NOTE-TEXT"], "extra": {"n": 1, "why": "WHY-TEXT"},
           "parent_id": "not-a-uuid"}
    adapter = fake_adapter(tmp_path, scripted(obj))
    adapter.start("inv-1", snap(adapter), gate_token="t")
    meta = payload(adapter, "inv-1")["result_meta"]
    assert meta["numbers"] == {"duration_ms": 1200, "duration_api_ms": 1100, "num_turns": 2,
                               "total_cost_usd": 0.0123, "extra": {"n": 1}}
    assert meta["flags"] == {"is_error": False, "fast_mode": False}
    assert meta["ids"] == {"session_id": obj["session_id"], "uuid": obj["uuid"]}
    assert {"result", "stop_reason", "notes", "parent_id", "usage", "modelUsage"} <= set(meta["fields"])
    assert not any(t in json.dumps(meta) for t in ("REPLY-TEXT", "NOTE-TEXT", "WHY-TEXT", "end_turn", "not-a-uuid"))


def test_success_without_usage_has_empty_usage(tmp_path):
    adapter = fake_adapter(tmp_path, scripted(result_object(with_usage=False)))
    assert adapter.start("inv-1", snap(adapter), gate_token="t").ok
    p = payload(adapter, "inv-1")
    assert p["usage"] == {} and p["cost_usd_estimate"] is None


def test_error_result_keeps_no_reply(tmp_path):
    adapter = fake_adapter(tmp_path, scripted(result_object(is_error=True, reply="Not logged in"), exit_code=1))
    result = adapter.start("inv-1", snap(adapter), gate_token="t")
    assert not result.ok and result.terminal
    p = payload(adapter, "inv-1")
    assert p["final_message"] == "" and p["error_message"] == "Not logged in"
    assert adapter.last_result("inv-1")["final_message"] == ""


def test_prompt_never_reaches_argv_or_the_record(tmp_path):
    adapter = fake_adapter(tmp_path)
    adapter.start("inv-1", snap(adapter), gate_token="t")
    call = adapter.runner.calls[0]
    assert PROMPT not in call["argv"] and call["argv"][-len(CLAUDE_ARGV_TAIL):] == list(CLAUDE_ARGV_TAIL)
    record = json.dumps(adapter.store.get("inv-1"), ensure_ascii=False)
    assert PROMPT not in record
    assert payload(adapter, "inv-1")["input_sha256"] == hashlib.sha256(PROMPT.encode()).hexdigest()


@pytest.mark.parametrize("change, expected", [
    (lambda s: {"gate_token": None}, "no kernel gate token"),
    (lambda s: {"snapshot": InvocationSnapshot(agent_id="claude-code", task_input="x",
                                               parent_task_id=None, depth=0)}, "another adapter identity"),
    (lambda s: {"snapshot": InvocationSnapshot(agent_id="codex", task_input="x", parent_task_id=None,
                                               depth=0, adapter_version=s)}, "not served"),
    (lambda s: {"snapshot": InvocationSnapshot(agent_id="claude-code", task_input="x", parent_task_id=None,
                                               depth=0, adapter_version=s, context_mode="native_resume",
                                               resume_thread_id="t")}, "cannot resume"),
])
def test_refusals_launch_nothing(tmp_path, change, expected):
    adapter = fake_adapter(tmp_path)
    kwargs = {"snapshot": snap(adapter), "gate_token": "t", **change(adapter.binding_identity())}
    result = adapter.start("inv-1", kwargs["snapshot"], gate_token=kwargs["gate_token"])
    assert not result.ok and result.terminal and expected in result.detail
    assert adapter.start_count == 0 and adapter.runner.calls == []
    assert adapter.store.get("inv-1") is None


def test_ambiguous_runner_error_keeps_the_slot(tmp_path):
    def boom(argv, env, prompt):
        raise RuntimeError("after spawn")

    adapter = fake_adapter(tmp_path, boom)
    result = adapter.start("inv-1", snap(adapter), gate_token="t")
    assert not result.ok and result.terminal is False
    assert adapter.store.get("inv-1")["status"] == "running"


def test_cancel_is_unsupported(tmp_path):
    adapter = fake_adapter(tmp_path)
    assert adapter.cancel_invocation("x")["reason"] == "unsupported"
    assert adapter.stop_local_invocation("x")["reason"] == "unsupported"  # no process runner


# --------------------------------------------------------------------------- #
# the bundled stdio stub through the real ProcessRunner
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("scenario, ok, terminal, status", [
    ("success-usage", True, True, "finished"),
    ("success-no-usage", True, True, "finished"),
    ("error-result", False, True, "failed"),
    ("max-turns", False, True, "failed"),
    ("malformed", False, False, "running"),
    ("nonzero-exit", False, True, "failed"),
    ("success-nonzero", False, False, "running"),
])
def test_stub_scenarios(tmp_path, scenario, ok, terminal, status):
    adapter, marker = stub_adapter(tmp_path, scenario)
    assert execution_admission(adapter).mode == "simulation"
    result = adapter.start("inv-1", snap(adapter), gate_token="t")
    assert (result.ok, result.terminal) == (ok, terminal)
    assert adapter.store.get("inv-1")["status"] == status
    seen = json.loads(marker.read_text(encoding="utf-8").splitlines()[0])
    # The prompt arrived on stdin, unchanged; argv is exactly the fixed tail.
    assert seen["stdin_sha256"] == hashlib.sha256(PROMPT.encode()).hexdigest()
    assert seen["argv"][2:] == list(CLAUDE_ARGV_TAIL)
    assert os.path.normcase(seen["cwd"]) == os.path.normcase(str(tmp_path / "runs" / "inv-1"))
    p = payload(adapter, "inv-1")
    assert p["workdir_entries_after"] == []
    assert list((tmp_path / "runs" / "_scratch").iterdir()) == []  # stdout deleted after parsing
    assert (tmp_path / "runs" / "_logs" / "inv-1-claude-stderr.log").exists()
    assert PROMPT not in json.dumps(adapter.store.get("inv-1"), ensure_ascii=False)


def test_stub_hang_times_out_unconfirmed(tmp_path):
    adapter, _ = stub_adapter(tmp_path, "hang", timeout_seconds=1.0)
    result = adapter.start("inv-1", snap(adapter), gate_token="t")
    assert not result.ok and result.terminal is False and "timed out" in result.detail
    assert payload(adapter, "inv-1")["timed_out"] is True


def test_child_env_drops_inherited_claude_and_tls_variables(tmp_path, monkeypatch):
    for name in ("ANTHROPIC_BASE_URL", "ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN", "CLAUDECODE",
                 "NODE_TLS_REJECT_UNAUTHORIZED", "NODE_OPTIONS"):
        monkeypatch.setenv(name, "inherited-placeholder")
    adapter, marker = stub_adapter(tmp_path, "success-usage")
    assert adapter.start("inv-1", snap(adapter), gate_token="t").ok
    names = set(json.loads(marker.read_text(encoding="utf-8").splitlines()[0])["env_names"])
    assert not names & {"ANTHROPIC_BASE_URL", "ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN", "CLAUDECODE",
                        "NODE_TLS_REJECT_UNAUTHORIZED", "NODE_OPTIONS"}
    assert {"CLAUDE_CONFIG_DIR", "DISABLE_AUTOUPDATER"} <= names
    p = payload(adapter, "inv-1")
    assert "ANTHROPIC_API_KEY" in p["env_stripped"]
    assert "inherited-placeholder" not in json.dumps(p)


def test_build_env_sets_proxy_and_extra_ca_but_never_disables_verification():
    env, stripped, added = build_env(config_dir="C:\\cfg", proxy="http://127.0.0.1:8090", ca_bundle="C:\\ca.pem",
                                     base_env={"PATH": "x", "HTTPS_PROXY": "http://other:1",
                                               "NODE_TLS_REJECT_UNAUTHORIZED": "0"})
    assert env["HTTPS_PROXY"] == env["HTTP_PROXY"] == "http://127.0.0.1:8090"
    assert env["NODE_EXTRA_CA_CERTS"] == "C:\\ca.pem" and "NODE_TLS_REJECT_UNAUTHORIZED" not in env
    assert env["CLAUDE_CONFIG_DIR"] == "C:\\cfg" and env["PATH"] == "x"
    assert stripped == ["HTTPS_PROXY", "NODE_TLS_REJECT_UNAUTHORIZED"]
    assert "CLAUDE_CONFIG_DIR" in added


@pytest.mark.parametrize('timeout',[30,0])
def test_local_stop_ends_only_our_process(tmp_path,timeout):
    import threading

    adapter, _ = stub_adapter(tmp_path, "hang", timeout_seconds=timeout)
    box = {}
    worker = threading.Thread(target=lambda: box.update(r=adapter.start("inv-1", snap(adapter), gate_token="t")))
    worker.start()
    deadline = __import__("time").monotonic() + 10
    while "inv-1" not in adapter.runner._processes and __import__("time").monotonic() < deadline:
        __import__("time").sleep(0.05)
    stopped = adapter.stop_local_invocation("inv-1")
    worker.join(timeout=15)
    assert stopped["local_stop_requested"] and stopped["remote_stop_confirmed"] is False
    assert box["r"].terminal is False and "local process stop" in box["r"].detail


def test_stub_scenario_lists_agree():
    import importlib.util

    spec = importlib.util.spec_from_file_location("stub_cc", cc.bundled_stub_path())
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert tuple(module.SCENARIOS) == STUB_SCENARIOS


# --------------------------------------------------------------------------- #
# admission matrix
# --------------------------------------------------------------------------- #


@pytest.fixture
def pinned_file(tmp_path, monkeypatch):
    exe = tmp_path / "bin" / "claude.exe"
    exe.parent.mkdir()
    exe.write_bytes(b"not a real claude binary")
    monkeypatch.setitem(PINNED_CLAUDE_CODE, "path", str(exe))
    monkeypatch.setitem(PINNED_CLAUDE_CODE, "sha256", hashlib.sha256(exe.read_bytes()).hexdigest())
    monkeypatch.setitem(PINNED_CLAUDE_CODE, "size", exe.stat().st_size)
    ca = tmp_path / "ca.pem"
    ca.write_text("-----BEGIN CERTIFICATE-----\n")
    return exe, ca


@pytest.fixture
def confirmed(monkeypatch):
    monkeypatch.setattr(cc, "ZERO_MODEL_IDENTITY_REPORT", "0" * 64)


@pytest.fixture
def no_launch(monkeypatch):
    calls = []

    def sentinel(argv, **kwargs):
        calls.append(list(argv))
        raise OSError("sentinel: launch blocked in tests")

    monkeypatch.setattr(subprocess, "Popen", sentinel)
    return calls


def real_adapter(tmp_path, ca, **kw):
    launcher = ClaudeCodeLauncher.pinned(config_dir=str(tmp_path / "cfg"), proxy="http://127.0.0.1:8090",
                                         ca_bundle=str(ca))
    return ClaudeCodeAdapter(launcher=launcher, workdir_root=str(tmp_path / "runs"),
                             store=ExecutionStore(str(tmp_path / "cc.db"), table="claude_code_executions"), **kw)


def test_strict_never_admits_the_real_launcher(tmp_path, pinned_file, confirmed):
    adapter = real_adapter(tmp_path, pinned_file[1])
    strict = execution_admission(adapter, "strict")
    assert not strict.allowed and strict.code == "claude_code_controls_unverified"
    assert execution_admission(adapter, "supervised").mode == "supervised"


@pytest.mark.parametrize('timeout',[0,3600])
def test_adopted_runtime_uses_the_explicit_desktop_time_limit(tmp_path,pinned_file,confirmed,timeout):
    adapter=real_adapter(tmp_path,pinned_file[1],timeout_seconds=timeout)
    adapter.launcher._adopted_identity=dict(PINNED_CLAUDE_CODE)
    assert execution_admission(adapter,'supervised').allowed
    assert not execution_admission(adapter,'strict').allowed


def test_supervised_stays_refused_until_the_zero_model_check_is_recorded(tmp_path, pinned_file, monkeypatch):
    monkeypatch.setattr(cc, "ZERO_MODEL_IDENTITY_REPORT", None)
    adapter = real_adapter(tmp_path, pinned_file[1])
    admission = execution_admission(adapter, "supervised")
    assert not admission.allowed and "identity_unconfirmed" in admission.reason


def test_the_shipped_constant_is_the_reviewed_identity_report():
    # report.json of the 2026-10-23 run (evidence/cc-identity-real-run-1-inconclusive),
    # recorded by the user's decision to keep --max-turns.
    assert cc.ZERO_MODEL_IDENTITY_REPORT == "b57a1d5f0d2a0bd1b7c35031c3fdafb74e2a763d82a12a1f40844b8b8cbd5089"


@pytest.mark.parametrize("change, problem", [
    (lambda a, t, e: setattr(a.launcher, "proxy", None), "proxy_missing"),
    (lambda a, t, e: setattr(a.launcher, "ca_bundle", str(t / "missing.pem")), "ca_unreadable"),
    (lambda a, t, e: a.launcher.argv.append("--extra"), "argv_changed"),
    (lambda a, t, e: setattr(a.launcher, "version", "9.9.9"), "identity_not_pinned_constant"),
    (lambda a, t, e: e.write_bytes(b"different length now!"), "size_mismatch"),
    (lambda a, t, e: e.unlink(), "executable_missing"),
    (lambda a, t, e: setattr(a.launcher, "config_dir", os.path.join(cc.REPO_ROOT, "var", "cfg")),
     "config_dir_not_outside_repository"),
    (lambda a, t, e: setattr(a, "workdir_root", os.path.join(cc.REPO_ROOT, "var", "runs")),
     "workdir_inside_repository"),
])
def test_each_supervised_condition_blocks(tmp_path, pinned_file, confirmed, change, problem):
    exe, ca = pinned_file
    adapter = real_adapter(tmp_path, ca)
    change(adapter, tmp_path, exe)
    admission = execution_admission(adapter, "supervised")
    assert not admission.allowed and admission.code == "claude_code_supervised_conditions"
    assert problem in admission.reason


@pytest.mark.parametrize("timeout", [0, 601, math.inf, math.nan, -1])
def test_supervised_needs_a_bounded_deadline(tmp_path, pinned_file, confirmed, timeout):
    adapter = real_adapter(tmp_path, pinned_file[1], timeout_seconds=timeout)
    assert "timeout" in execution_admission(adapter, "supervised").reason


def test_lookalike_launcher_and_untrusted_stub_are_refused(tmp_path, pinned_file, confirmed):
    class Lookalike(ClaudeCodeLauncher):
        pass

    adapter = real_adapter(tmp_path, pinned_file[1])
    adapter.launcher.__class__ = Lookalike
    assert not execution_admission(adapter, "supervised").allowed
    stub, _ = stub_adapter(tmp_path, "success-usage")
    stub.launcher.argv[1] = __file__  # not the bundled stub
    assert execution_admission(stub, "supervised").allowed is False


def test_replaced_file_is_never_launched(tmp_path, pinned_file, confirmed, no_launch):
    exe, ca = pinned_file
    adapter = real_adapter(tmp_path, ca)
    exe.write_bytes(b"NOT a real claude binary")  # same size, other content
    result = adapter.start("inv-1", snap(adapter, execution_mode="supervised"), gate_token="t")
    assert not result.ok and result.terminal and "sha256_mismatch" in result.detail
    assert no_launch == [] and adapter.start_count == 0


def test_matching_file_reaches_the_launch_with_the_fixed_argv(tmp_path, pinned_file, confirmed, no_launch):
    exe, ca = pinned_file
    adapter = real_adapter(tmp_path, ca)
    result = adapter.start("inv-1", snap(adapter, execution_mode="supervised"), gate_token="t")
    assert not result.ok and result.terminal  # the sentinel refused: provably nothing started
    assert no_launch == [[str(exe), *CLAUDE_ARGV_TAIL]]
    assert adapter.store.get("inv-1")["status"] == "failed"


def test_shipped_pin_and_argv():
    assert PINNED_CLAUDE_CODE["version"] == "2.1.308" and PINNED_CLAUDE_CODE["size"] == 297376928
    assert PINNED_CLAUDE_CODE["sha256"] == "c9796ba22a9221586b36371b36f5b66198d66eb4badf6a4cf4a5f7b07becea53"
    assert PINNED_CLAUDE_CODE["path"].endswith(os.path.join("2.1.308", "c17a819afb40", "claude.exe"))
    for flag in ("-p", "json", "--safe-mode", "--strict-mcp-config", "--disable-slash-commands",
                 "--no-session-persistence"):
        assert flag in CLAUDE_ARGV_TAIL
    assert CLAUDE_ARGV_TAIL[CLAUDE_ARGV_TAIL.index("--tools") + 1] == ""
    assert CLAUDE_ARGV_TAIL[CLAUDE_ARGV_TAIL.index("--max-turns") + 1] == "1"
    assert "--dangerously-skip-permissions" not in CLAUDE_ARGV_TAIL and "--bare" not in CLAUDE_ARGV_TAIL


def test_capabilities_claim_nothing_unverified(tmp_path, pinned_file):
    caps = real_adapter(tmp_path, pinned_file[1]).capabilities()
    assert not any(caps["verified"].values())
    for key in ("stream", "resume_session", "cancel", "intercept_tool", "control_subagent"):
        assert caps[key] is False
    assert caps["real_usage"] == "unknown" and caps["limit_dir"] == "unknown"
    for text in (SUPERVISED_REASON, REAL_BLOCK_REASON):
        assert "支持" not in text and "已禁用" not in text and "未验证" in text
