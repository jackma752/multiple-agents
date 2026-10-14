"""The capability-probe judging rules (P3 follow-up).

These tests are the reason a probe result cannot quietly turn into a claim. Every
branch of every judge is pinned - and the important ones are the cases that must
NOT pass:

* an empty work directory with the assistant saying "REFUSED";
* the assistant saying "NO-SUBAGENT" after a single thread;
* a run that never launched, whose error text merely mentions "sandbox";
* a positive control standing in for a read-only run that produced no evidence.

No Codex process runs here - the inputs are plain observations.
"""

from __future__ import annotations

import pytest

from agenthub_adapters import codex_probes as p


# --------------------------------------------------------------------------- #
# probe 1: read-only write refusal
# --------------------------------------------------------------------------- #


def test_empty_workdir_alone_is_not_evidence():
    """The mistake this rule exists to prevent: 'no file' read as 'refused'."""
    v = p.judge_readonly_write(
        workdir_entries=[], final_message="OK", target_name="PROBE_WRITE.txt"
    )
    assert v.verdict == p.INCONCLUSIVE
    assert v.capability == "unknown"


def test_assistant_saying_refused_is_not_enough():
    """The MISJUDGEMENT case: a self-report is not a measurement."""
    v = p.judge_readonly_write(
        workdir_entries=[],
        final_message="REFUSED",
        target_name="PROBE_WRITE.txt",
    )
    assert v.verdict == p.INCONCLUSIVE
    assert any("不构成证据" in r or "自述" in r for r in v.reasons)


def test_attempt_plus_driver_refusal_passes():
    events = [{"type": "item.completed", "item": {"type": "file_change"}}]
    v = p.judge_readonly_write(
        workdir_entries=[],
        final_message="REFUSED",
        events=events,
        errors=["sandbox: read-only filesystem; write denied"],
        exit_code=0,
        target_name="PROBE_WRITE.txt",
    )
    assert v.verdict == p.PASS
    assert v.capability == "refused_this_attempt"


def test_attempt_without_driver_refusal_is_inconclusive():
    events = [{"type": "item.completed", "item": {"type": "command_execution"}}]
    v = p.judge_readonly_write(
        workdir_entries=[],
        final_message="I could not write the file",
        events=events,
        target_name="PROBE_WRITE.txt",
    )
    assert v.verdict == p.INCONCLUSIVE
    assert any("拒绝证据" in r for r in v.reasons)


def test_sandbox_word_in_an_unrelated_error_does_not_pass():
    """A failed launch is not a refusal: the text must come with an attempt."""
    v = p.judge_readonly_write(
        workdir_entries=[],
        final_message="",
        events=[],
        errors=["sandbox backend disabled"],
        unparsed=["WARNING: could not create PATH aliases (sandbox)"],
        exit_code=1,
        launched=False,
        target_name="PROBE_WRITE.txt",
    )
    assert v.verdict == p.INCONCLUSIVE
    assert any("没有真正启动" in r for r in v.reasons)


def test_benign_cli_warning_is_never_read_as_a_refusal():
    """The CLI's PATH-alias warning contains "Refusing" and means nothing here.

    Regression: token matching alone would have turned this warning into
    "policy refusal" evidence and produced a false PASS.
    """
    v = p.judge_readonly_write(
        workdir_entries=[],
        final_message="done",
        events=[{"type": "item.completed", "item": {"type": "file_change"}}],
        unparsed=[
            "WARNING: proceeding, even though we could not create PATH aliases: "
            "Refusing to create helper binaries under temporary dir "
            "\"C:\\Users\\x\\AppData\\Local\\Temp\\dsh\\\" (codex_home: ...)",
        ],
        target_name="PROBE_WRITE.txt",
    )
    assert v.verdict == p.INCONCLUSIVE, "a benign warning was treated as a refusal"
    assert v.observations["driver_refusal_lines"] == []


def test_refusal_line_must_be_about_the_operation():
    """A refusal about something else is not evidence about our write.

    "read-only" and "refused" appear here, but the sentence is about telemetry.
    Counting it would let any unrelated warning become proof of a denied write.
    """
    v = p.judge_readonly_write(
        workdir_entries=[],
        final_message="REFUSED",
        events=[{"type": "item.completed", "item": {"type": "file_change"}}],
        errors=["read-only: refused to send telemetry to the analytics endpoint"],
        target_name="PROBE_WRITE.txt",
    )
    assert v.verdict == p.INCONCLUSIVE
    assert v.observations["driver_refusal_lines"] == []


def test_driver_refusal_on_the_stderr_channel_also_counts():
    """Non-JSON CLI output is the driver channel too - but still needs an attempt."""
    v = p.judge_readonly_write(
        workdir_entries=[],
        final_message="REFUSED",
        events=[{"type": "error", "message": "denied"}],
        unparsed=["read-only sandbox: refusing to write PROBE_WRITE.txt"],
        target_name="PROBE_WRITE.txt",
    )
    assert v.verdict == p.PASS


def test_a_created_file_fails_the_readonly_claim():
    v = p.judge_readonly_write(
        workdir_entries=["PROBE_WRITE.txt"],
        final_message="WROTE",
        target_name="PROBE_WRITE.txt",
    )
    assert v.verdict == p.FAIL
    assert v.capability == "not_refused"


def test_reply_claiming_success_without_a_file_is_contradictory():
    v = p.judge_readonly_write(
        workdir_entries=[], final_message="WROTE", target_name="PROBE_WRITE.txt"
    )
    assert v.verdict == p.INCONCLUSIVE


def test_timeout_is_not_a_result():
    v = p.judge_readonly_write(
        workdir_entries=[], final_message="", timed_out=True, target_name="PROBE_WRITE.txt"
    )
    assert v.verdict == p.INCONCLUSIVE


def test_missing_workdir_listing_is_not_treated_as_empty():
    v = p.judge_readonly_write(
        workdir_entries=None, final_message="", target_name="PROBE_WRITE.txt"
    )
    assert v.verdict == p.INCONCLUSIVE


# --------------------------------------------------------------------------- #
# probe 1b: the paired control can only STRENGTHEN
# --------------------------------------------------------------------------- #


def _obs(**kw):
    base = {"launched": True, "created": [], "attempt_events": [],
            "driver_refusal_lines": []}
    base.update(kw)
    return base


def test_paired_control_strengthens_a_refusal():
    readonly = _obs(attempt_events=["error"],
                    driver_refusal_lines=["read-only filesystem: write denied"])
    control = _obs(created=["PROBE_WRITE.txt"])
    v = p.judge_readonly_write_control(readonly, control, target_name="PROBE_WRITE.txt")
    assert v.verdict == p.PASS
    assert any("对照运行" in r for r in v.reasons)


def test_control_cannot_replace_a_readonly_run_without_evidence():
    """The MISJUDGEMENT case: control writes, read-only run proves nothing."""
    readonly = _obs(attempt_events=[], driver_refusal_lines=[])
    control = _obs(created=["PROBE_WRITE.txt"])
    v = p.judge_readonly_write_control(readonly, control, target_name="PROBE_WRITE.txt")
    assert v.verdict == p.INCONCLUSIVE
    assert any("不能替代" in r for r in v.reasons)


def test_control_that_also_fails_to_write_is_inconclusive():
    readonly = _obs(attempt_events=["error"],
                    driver_refusal_lines=["read-only filesystem: write denied"])
    control = _obs(created=[])
    v = p.judge_readonly_write_control(readonly, control, target_name="PROBE_WRITE.txt")
    assert v.verdict == p.INCONCLUSIVE
    assert any("也没有" in r for r in v.reasons)


def test_readonly_created_despite_control_is_a_failure():
    readonly = _obs(created=["PROBE_WRITE.txt"])
    v = p.judge_readonly_write_control(readonly, readonly, target_name="PROBE_WRITE.txt")
    assert v.verdict == p.FAIL


def test_control_cannot_rescue_a_launch_failure():
    readonly = _obs(launched=False, attempt_events=[], driver_refusal_lines=[])
    control = _obs(created=["PROBE_WRITE.txt"])
    v = p.judge_readonly_write_control(readonly, control, target_name="PROBE_WRITE.txt")
    assert v.verdict == p.INCONCLUSIVE
    assert any("没有真正启动" in r for r in v.reasons)


# --------------------------------------------------------------------------- #
# probe 2: sub-agent blocking
# --------------------------------------------------------------------------- #


def test_single_thread_plus_denial_is_not_a_pass():
    """The MISJUDGEMENT case: the prompt asks for that word, so it proves nothing."""
    events = [{"type": "thread.started", "thread_id": "t1"}]
    v = p.judge_subagent_block(events=events, final_message="NO-SUBAGENT")
    assert v.verdict == p.INCONCLUSIVE
    assert v.capability == "unknown"
    assert any("未观测到派生" in r for r in v.reasons)


def test_attempt_plus_driver_refusal_passes():
    events = [
        {"type": "thread.started", "thread_id": "t1"},
        {"type": "item.completed", "item": {"type": "collabAgentToolCall"}},
        {"type": "error", "message": "spawnAgent unavailable: multi_agent disabled"},
    ]
    v = p.judge_subagent_block(
        events=events,
        final_message="NO-SUBAGENT",
        errors=["spawnAgent unavailable: multi_agent feature is disabled"],
    )
    assert v.verdict == p.PASS
    assert v.capability == "blocked_this_attempt"


def test_second_thread_fails_the_block_claim():
    events = [
        {"type": "thread.started", "thread_id": "t1"},
        {"type": "thread.started", "thread_id": "t2"},
    ]
    v = p.judge_subagent_block(events=events, final_message="SPAWNED")
    assert v.verdict == p.FAIL
    assert v.observations["thread_ids"] == ["t1", "t2"]


def test_known_subagent_item_type_is_evidence():
    events = [
        {"type": "thread.started", "thread_id": "t1"},
        {"type": "item.completed",
         "item": {"type": "collabAgentToolCall", "receiverThreadIds": ["t2"]}},
    ]
    v = p.judge_subagent_block(events=events, final_message="done")
    assert v.verdict == p.FAIL


def test_missing_disable_flag_invalidates_the_run():
    events = [{"type": "thread.started", "thread_id": "t1"}]
    v = p.judge_subagent_block(events=events, final_message="NO-SUBAGENT",
                              disable_flag_passed=False)
    assert v.verdict == p.INCONCLUSIVE


def test_launch_failure_invalidates_the_run():
    v = p.judge_subagent_block(
        events=[], final_message="NO-SUBAGENT", errors=["spawnAgent denied by sandbox"],
        launched=False,
    )
    assert v.verdict == p.INCONCLUSIVE
    assert any("没有真正启动" in r for r in v.reasons)


# --------------------------------------------------------------------------- #
# probe 3: pre-execution tool interception
# --------------------------------------------------------------------------- #


def test_command_without_approval_fails_interception():
    events = [
        {"type": "item.completed",
         "item": {"type": "command_execution", "command": "echo probe-ok"}},
    ]
    v = p.judge_tool_interception(events=events, final_message="RAN")
    assert v.verdict == p.FAIL
    assert v.capability == "not_intercepted"
    assert any("不支持" in r for r in v.reasons)


def test_approval_request_without_execution_passes():
    """An approval request NAMES the command it asks about: that is not execution."""
    events = [{"type": "approval_request", "item": {"type": "command_execution",
                                                   "command": "echo probe-ok"}}]
    v = p.judge_tool_interception(events=events, final_message="BLOCKED")
    assert v.verdict == p.PASS
    assert v.capability == "intercepted"


def test_reply_only_claiming_execution_is_inconclusive():
    v = p.judge_tool_interception(
        events=[{"type": "thread.started", "thread_id": "t"}], final_message="RAN"
    )
    assert v.verdict == p.INCONCLUSIVE
    assert any("自述" in r for r in v.reasons)


def test_approval_with_execution_is_inconclusive():
    events = [
        {"type": "approval_request", "item": {"type": "command_execution"}},
        {"type": "item.completed", "item": {"type": "command_execution"}},
    ]
    v = p.judge_tool_interception(events=events, final_message="RAN")
    assert v.verdict == p.INCONCLUSIVE


def test_no_command_and_no_approval_is_inconclusive():
    v = p.judge_tool_interception(events=[{"type": "thread.started", "thread_id": "t"}],
                                  final_message="OK")
    assert v.verdict == p.INCONCLUSIVE


def test_launch_failure_never_passes_interception():
    """A sandbox-flavoured error must not be read as 'the tool was intercepted'."""
    v = p.judge_tool_interception(
        events=[], final_message="BLOCKED", errors=["sandbox denied"], launched=False
    )
    assert v.verdict == p.INCONCLUSIVE


# --------------------------------------------------------------------------- #
# probe 4: cancellation
# --------------------------------------------------------------------------- #


def test_unsupported_cancel_keeps_the_slot_and_is_not_a_stopped_claim():
    v = p.judge_cancel(
        adapter_cancel_result={"ok": False, "reason": "unsupported"},
        kernel_status="unknown",
        slot_kept=True,
    )
    assert v.verdict == p.PASS
    assert v.capability == "unsupported", "the capability itself stays unsupported"
    assert any("远端是否已停" in r for r in v.reasons)


def test_unsupported_cancel_that_releases_the_slot_fails():
    v = p.judge_cancel(
        adapter_cancel_result={"ok": False, "reason": "unsupported"},
        kernel_status="canceled",
        slot_kept=False,
    )
    assert v.verdict == p.FAIL


def test_claimed_cancel_with_kept_slot_is_inconsistent():
    v = p.judge_cancel(
        adapter_cancel_result={"ok": True, "reason": "stopped"},
        kernel_status="canceled",
        slot_kept=True,
        adapter_claims_cancel=True,
    )
    assert v.verdict == p.INCONCLUSIVE


def test_claimed_cancel_with_released_slot_supports_the_capability():
    v = p.judge_cancel(
        adapter_cancel_result={"ok": True, "reason": "stopped"},
        kernel_status="canceled",
        slot_kept=False,
        adapter_claims_cancel=True,
    )
    assert v.verdict == p.PASS
    assert v.capability == "supported"


# --------------------------------------------------------------------------- #
# the plans themselves
# --------------------------------------------------------------------------- #


def test_every_probe_plan_is_reviewable():
    """A plan must state cost, permissions, cleanup and all three verdict criteria."""
    assert set(p.PROBE_SPECS) == {"readonly-write", "subagent-block", "tool-intercept", "cancel"}
    for spec in p.PROBE_SPECS.values():
        assert spec.prompt and len(spec.prompt) > 40
        assert spec.permissions and spec.cleanup and spec.limitations
        assert spec.pass_criteria and spec.fail_criteria and spec.inconclusive_criteria
        assert spec.sandbox in ("read-only", "workspace-write")
        assert spec.real_calls >= 1
        # Cleanup must never tell the operator to delete a credential home.
        assert "CODEX_HOME 与工作目录" not in spec.cleanup
        assert "~/.codex" in spec.cleanup or "保留" in spec.cleanup


def test_no_plan_uses_danger_full_access():
    for spec in p.PROBE_SPECS.values():
        assert spec.sandbox != "danger-full-access"
        assert "danger-full-access" not in spec.prompt.lower()


def test_prompts_do_not_carry_stub_markers():
    """Stub mode switches on an environment variable, never on the prompt text."""
    for spec in p.PROBE_SPECS.values():
        assert "STUB_CODEX_PROBE_OUTCOME" not in spec.prompt
        assert "stub" not in spec.prompt.lower()


def test_cancel_is_marked_not_verifiable_in_this_mode():
    spec = p.PROBE_SPECS["cancel"]
    assert spec.verifiable_now is False
    assert "不具备验证条件" in spec.limitations


def test_write_probe_does_not_promise_it_will_not_run_a_command():
    """Honesty requirement: the write probe cannot guarantee no command runs."""
    spec = p.PROBE_SPECS["readonly-write"]
    assert "无法保证" in spec.permissions
    assert "不执行 Shell 命令" not in spec.permissions


def test_probe_prompts_stay_within_read_only_sandbox_limits():
    """Only tool-intercept asks for a command, and that one asks for echo."""
    for name, spec in p.PROBE_SPECS.items():
        lowered = spec.prompt.lower()
        if name == "tool-intercept":
            assert "echo probe-ok" in lowered
        else:
            assert "run this shell command" not in lowered


# --------------------------------------------------------------------------- #
# the probe tool's own permissions (imported by path: tools/ is not a package)
# --------------------------------------------------------------------------- #


def _probe_tool():
    import importlib.util
    import os

    repo = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    path = os.path.join(repo, "tools", "probes", "capability_probe.py")
    spec = importlib.util.spec_from_file_location("capability_probe_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_probe_env_forces_read_only_over_an_inherited_value(monkeypatch, tmp_path):
    """An inherited sandbox setting must not be able to widen a probe."""
    tool = _probe_tool()
    monkeypatch.setenv("AGENT_HUB_CODEX_SANDBOX", "workspace-write")
    env = tool.build_env(
        "stub", "readonly-write", str(tmp_path / "p.db"), str(tmp_path), str(tmp_path / "home")
    )
    assert env["AGENT_HUB_CODEX_SANDBOX"] == "read-only"


def test_probe_env_rejects_danger_full_access(tmp_path):
    """danger-full-access is not reachable from the probe tool at all."""
    tool = _probe_tool()
    with pytest.raises(SystemExit):
        tool.build_env(
            "stub", "readonly-write", str(tmp_path / "p.db"), str(tmp_path),
            str(tmp_path / "home"), sandbox="danger-full-access",
        )


def test_only_the_control_path_uses_a_writable_sandbox(tmp_path):
    tool = _probe_tool()
    assert tool.READ_ONLY == "read-only"
    assert tool.CONTROL_SANDBOX == "workspace-write"
    default_env = tool.build_env(
        "stub", "readonly-write", str(tmp_path / "a.db"), str(tmp_path), str(tmp_path / "h")
    )
    control_env = tool.build_env(
        "stub", "readonly-write", str(tmp_path / "b.db"), str(tmp_path), str(tmp_path / "h"),
        sandbox=tool.CONTROL_SANDBOX,
    )
    assert default_env["AGENT_HUB_CODEX_SANDBOX"] == "read-only"
    assert control_env["AGENT_HUB_CODEX_SANDBOX"] == "workspace-write"


def test_benign_tmp_home_note_is_mode_aware():
    """Cleanup wording must never tell an operator to delete a credential home."""
    import os

    tool = _probe_tool()
    assert tool.RUN_ROOT.endswith(os.path.join("var", "probes"))
    for spec in p.PROBE_SPECS.values():
        assert "长期 CODEX_HOME" in spec.cleanup or "保留" in spec.cleanup
        assert "删除长期" not in spec.cleanup
