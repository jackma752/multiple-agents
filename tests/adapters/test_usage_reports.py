"""T-1a: what each adapter reports to the usage ledger (offline stubs only).

App-server: the in-memory scripted server now reports usage the real way
(``thread/tokenUsage/updated`` per request; ``turn/completed`` carries none).
Claude Code: the in-process fake runner with documented result shapes.
"""

import json
import threading

import pytest

from agenthub.contracts import InvocationSnapshot
from agenthub.usage import COMPLETE, PARTIAL, UNKNOWN, normalize_usage
from agenthub_adapters import appserver_stub as stub
from agenthub_adapters.appserver_adapter import AppServerCodexAdapter
from agenthub_adapters.appserver_stub import (
    CHILD_USAGE, REPLAY_USAGE, S3_REQUESTS, InMemoryStubFactory, add_usage, stub_thread_total,
    stub_turn_usage,
)
from agenthub_adapters.claude_code import (
    ClaudeCodeAdapter, ClaudeLaunchFailed, FakeClaudeRunner, StubClaudeLauncher, result_object, scripted,
)
from agenthub_adapters.store import ExecutionStore

S3_TURN = add_usage(*S3_REQUESTS)


def appserver(tmp_path, scenario, thread_id="stub-thread-0001"):
    return AppServerCodexAdapter(
        transport_factory=InMemoryStubFactory(scenario, thread_id=thread_id),
        workdir_root=str(tmp_path / "runs"),
        store=ExecutionStore(str(tmp_path / "appserver.db"), table="appserver_executions"),
        timeout_seconds=5.0)


def snap(adapter, agent="codex", text="x", **extra):
    return InvocationSnapshot(agent_id=agent, task_input=text, parent_task_id=None, depth=0,
                              adapter_version=adapter.binding_identity(), **extra)


def run(adapter, inv="inv-1", **extra):
    return adapter.start_streaming(inv, snap(adapter, **extra), gate_token="t", emit=lambda *_: None)


def record(adapter, inv="inv-1"):
    return normalize_usage(adapter.usage_report(inv))


def payload(adapter, inv="inv-1"):
    return adapter.store.get(inv)["payload"]


# ------------------------------------------------------------------ app-server


def test_a_two_request_turn_is_counted_whole(tmp_path):
    adapter = appserver(tmp_path, "usage-multi-request")
    assert run(adapter).ok
    p = payload(adapter)
    assert p["usage"] == S3_TURN and p["usage_source"] == "thread_total_delta"
    assert len(p["usage_updates"]) == 2
    rec = record(adapter)
    assert rec.completeness == COMPLETE
    assert (rec.input_total, rec.cache_read, rec.output_total) == (30539, 24832, 79)


@pytest.mark.parametrize("scenario, completeness, note", [
    ("usage-missed-update", PARTIAL, "last_sum_mismatch"),
    ("usage-late", PARTIAL, "late_usage"),
    ("usage-cache-write", PARTIAL, "mapping_unverified"),
    ("usage-none", UNKNOWN, "no_usage_reported"),
])
def test_usage_edge_cases_are_labelled(tmp_path, scenario, completeness, note):
    adapter = appserver(tmp_path, scenario)
    assert run(adapter).ok
    rec = record(adapter)
    assert rec.completeness == completeness and note in rec.notes


def test_a_late_update_is_kept_and_still_counted_late(tmp_path):
    adapter = appserver(tmp_path, "usage-late")
    assert run(adapter).ok
    p = payload(adapter)
    assert p["late_events"] >= 1 and p["usage"] == stub_turn_usage(1)


def test_usage_written_after_the_terminal_queue_is_drained_is_not_lost(tmp_path, monkeypatch):
    from agenthub_adapters.appserver import AppServerClient
    from agenthub_adapters.appserver_adapter import TOKEN_USAGE_UPDATED

    terminal_observed = threading.Event()
    drain_waiting = threading.Event()
    original_pump = AppServerCodexAdapter._pump
    original_wait = AppServerClient.wait_for
    original_notify = stub.ScriptedAppServer.notify

    def pump(self, *args, **kwargs):
        result = original_pump(self, *args, **kwargs)
        terminal_observed.set()
        return result

    def wait(self, *args, **kwargs):
        if terminal_observed.is_set():
            drain_waiting.set()
        return original_wait(self, *args, **kwargs)

    def notify(self, method, params):
        if self.scenario == 'usage-late' and method == TOKEN_USAGE_UPDATED:
            # Hold the producer until after terminal was consumed and the
            # adapter explicitly waits for trailing events. No timing sleeps.
            if not drain_waiting.wait(1):
                return
        return original_notify(self, method, params)

    monkeypatch.setattr(AppServerCodexAdapter, '_pump', pump)
    monkeypatch.setattr(AppServerClient, 'wait_for', wait)
    monkeypatch.setattr(stub.ScriptedAppServer, 'notify', notify)
    adapter = appserver(tmp_path, 'usage-late')
    assert run(adapter).ok
    assert drain_waiting.is_set()
    assert payload(adapter)['usage'] == stub_turn_usage(1)
    assert record(adapter).completeness == PARTIAL
    assert 'late_usage' in record(adapter).notes


def test_a_duplicate_update_counts_once(tmp_path):
    adapter = appserver(tmp_path, "usage-duplicate")
    assert run(adapter).ok
    rec = record(adapter)
    assert rec.completeness == COMPLETE and rec.input_total == stub_turn_usage(1)["inputTokens"]
    assert "duplicate_usage_update" in rec.notes


def test_turn_completed_usage_is_never_read_d_t0_4(tmp_path, monkeypatch):
    original = stub.ScriptedAppServer.notify

    def with_turn_usage(self, method, params):
        if method == "turn/completed":  # a field the schema's Turn does not have
            params = {**params, "turn": {**params["turn"], "usage": {"inputTokens": 11, "outputTokens": 2}}}
        original(self, method, params)

    monkeypatch.setattr(stub.ScriptedAppServer, "notify", with_turn_usage)
    adapter = appserver(tmp_path, "usage-none")
    assert run(adapter).ok
    assert payload(adapter)["usage"] == {} and record(adapter).completeness == UNKNOWN


def test_a_resumed_turn_excludes_the_replay(tmp_path):
    adapter = appserver(tmp_path, ["stream", "stream"], thread_id="thread-r")
    assert run(adapter, "inv-1").ok
    assert run(adapter, "inv-2", context_mode="native_resume", resume_thread_id="thread-r").ok
    p = payload(adapter, "inv-2")
    assert p["replayed_usage"] == REPLAY_USAGE and p["replayed_usage_total"] == stub_thread_total(1)
    assert p["usage"] == stub_turn_usage(2) and p["usage_source"] == "thread_total_delta"
    assert record(adapter, "inv-2").completeness == COMPLETE


def test_a_reused_turn_id_makes_the_usage_unknown(tmp_path):
    adapter = appserver(tmp_path, ["recall", "recall-same-turn-id"], thread_id="thread-r")
    assert run(adapter, "inv-1").ok
    run(adapter, "inv-2", context_mode="native_resume", resume_thread_id="thread-r")
    rec = record(adapter, "inv-2")
    assert rec.completeness == UNKNOWN and "turn_id_reused" in rec.notes and rec.input_total is None


def test_native_child_usage_is_kept_apart_d_t0_7(tmp_path):
    adapter = appserver(tmp_path, "native-spawn-usage")
    run(adapter)
    p = payload(adapter)
    assert p["native_child_usage"]["child-thread-1"]["last"] == CHILD_USAGE
    assert p["usage"] == stub_turn_usage(1)  # the parent's own usage only
    assert p["usage_anomalies"] == 1
    rec = record(adapter)
    assert rec.completeness == PARTIAL
    assert {"native_children_not_metered", "native_child_usage_seen"} <= set(rec.notes)


def test_a_refusal_before_turn_start_is_a_known_zero(tmp_path):
    adapter = appserver(tmp_path, "settings-mismatch")
    result = run(adapter)
    assert not result.ok and result.terminal
    rec = record(adapter)
    assert rec.completeness == COMPLETE and rec.source == "no_turn_requested"
    assert rec.input_total == 0 and rec.output_total == 0


def test_a_failed_turn_still_reports_its_usage(tmp_path):
    adapter = appserver(tmp_path, "turn-failed")
    assert not run(adapter).ok
    assert record(adapter).completeness == COMPLETE


def test_records_from_before_t1a_are_legacy_partial(tmp_path):
    adapter = appserver(tmp_path, "happy")
    adapter.store.record_start("old-1", adapter_invocation_id="")
    adapter.store.set_payload("old-1", {"usage": stub_turn_usage(1), "verdict": "finished"})
    rec = record(adapter, "old-1")
    assert rec.completeness == PARTIAL and rec.notes == ("legacy_last",) and rec.source == "legacy_last"
    assert adapter.usage_report("never-seen") is None


# ----------------------------------------------------------------- Claude Code


def claude(tmp_path, script):
    return ClaudeCodeAdapter(
        launcher=StubClaudeLauncher("success-usage"), workdir_root=str(tmp_path / "cc-runs"),
        store=ExecutionStore(str(tmp_path / "cc.db"), table="claude_code_executions"),
        runner=FakeClaudeRunner(script))


TWO_AGENT_USAGE = {"input_tokens": 2, "cache_creation_input_tokens": 635, "cache_read_input_tokens": 1161,
                   "output_tokens": 15, "server_tool_use": {"web_search_requests": 0},
                   "service_tier": "standard", "cache_creation": {"ephemeral_5m_input_tokens": 635}}


def test_claude_code_total_input_includes_the_cache(tmp_path):
    adapter = claude(tmp_path, scripted(result_object(usage=TWO_AGENT_USAGE)))
    assert adapter.start("inv-1", snap(adapter, agent="claude-code"), gate_token="t").ok
    rec = record(adapter)
    assert rec.completeness == COMPLETE and rec.input_total == 1798 and rec.output_total == 15
    assert rec.model == "claude-model"
    p = payload(adapter)
    assert p["usage_raw"]["cache_creation"] == {"ephemeral_5m_input_tokens": 635}  # D-T0-5
    assert p["usage_raw"]["service_tier"] == "standard"
    assert p["model_usage"] == {"claude-model": {"inputTokens": 12, "outputTokens": 3}}
    assert "OK" not in json.dumps(rec.raw)


def test_claude_thinking_is_an_output_subset_and_is_not_counted_twice(tmp_path):
    usage = {**TWO_AGENT_USAGE, 'output_tokens': 106876, 'output_tokens_details': {'thinking_tokens': 77406}}
    adapter = claude(tmp_path, scripted(result_object(usage=usage)))
    assert adapter.start('thinking', snap(adapter, agent='claude-code'), gate_token='t').ok
    rec = record(adapter, 'thinking')
    assert rec.mapping_version == 2 and rec.output_total == 106876 and rec.reasoning == 77406
    assert rec.raw['output_tokens_details']['thinking_tokens'] == 77406


@pytest.mark.parametrize('mode', ['strict', 'supervised'])
def test_claude_approved_request_limit_is_forwarded_in_both_modes(tmp_path, mode):
    seen = []
    callback = scripted(result_object())
    def script(argv, env, prompt):
        seen.append(dict(env))
        return callback(argv, env, prompt)
    adapter = claude(tmp_path, script)
    snapshot = snap(adapter, agent='claude-code', execution_mode=mode, resources={'max_output_tokens': 32768})
    assert adapter.start('limited', snapshot, gate_token='t').ok
    assert seen[0]['CLAUDE_CODE_MAX_OUTPUT_TOKENS'] == '32768'
    assert snapshot.payload_hash() != snap(adapter, agent='claude-code', execution_mode=mode).payload_hash()


@pytest.mark.parametrize('bad', [-1, True, 16])
def test_claude_invalid_thinking_does_not_inflate_output(bad):
    from agenthub.usage import MAPPING_CLAUDE_CODE
    usage = {**TWO_AGENT_USAGE, 'output_tokens_details': {'thinking_tokens': bad}}
    rec = normalize_usage({'mapping': MAPPING_CLAUDE_CODE, 'usage': usage, 'source': 'cli_result'})
    assert rec.output_total == 15 and rec.reasoning is None and rec.completeness == PARTIAL


def test_claude_code_without_usage_is_unknown(tmp_path):
    adapter = claude(tmp_path, scripted(result_object(with_usage=False)))
    assert adapter.start("inv-1", snap(adapter, agent="claude-code"), gate_token="t").ok
    assert record(adapter).completeness == UNKNOWN


def test_claude_code_launch_failure_is_a_known_zero(tmp_path):
    def refuse(argv, env, prompt):
        raise ClaudeLaunchFailed("not created")

    adapter = claude(tmp_path, refuse)
    assert not adapter.start("inv-1", snap(adapter, agent="claude-code"), gate_token="t").ok
    rec = record(adapter)
    assert rec.completeness == COMPLETE and rec.source == "not_launched" and rec.input_total == 0


def test_claude_code_runner_error_is_unknown(tmp_path):
    def boom(argv, env, prompt):
        raise RuntimeError("after spawn")

    adapter = claude(tmp_path, boom)
    assert adapter.start("inv-1", snap(adapter, agent="claude-code"), gate_token="t").terminal is False
    assert record(adapter).completeness == UNKNOWN


def test_claude_code_several_models_are_partial(tmp_path):
    obj = result_object()
    obj["modelUsage"] = {"claude-a": {"inputTokens": 1}, "claude-b": {"inputTokens": 2}}
    adapter = claude(tmp_path, scripted(obj))
    assert adapter.start("inv-1", snap(adapter, agent="claude-code"), gate_token="t").ok
    rec = record(adapter)
    assert rec.completeness == PARTIAL and "multi_model_usage_unverified" in rec.notes and rec.model is None
