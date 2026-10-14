"""The capability-probe judging rules (P3 follow-up).

These tests are the reason a probe result cannot quietly turn into a claim. Every
branch of every judge is pinned, including the ones that must NOT pass: a missing
file, a missing thread count, an absent approval request, or a cancel that cannot
be confirmed.

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


def test_absence_plus_refusal_evidence_passes():
    v = p.judge_readonly_write(
        workdir_entries=[],
        final_message="REFUSED",
        errors=["sandbox: read-only filesystem; write to PROBE_WRITE.txt denied"],
        exit_code=0,
        target_name="PROBE_WRITE.txt",
    )
    assert v.verdict == p.PASS
    assert v.capability == "refused_this_attempt"
    assert any("仅代表本次" in r for r in v.reasons)


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
    assert any("矛盾" in r for r in v.reasons)


def test_timeout_is_not_a_result():
    v = p.judge_readonly_write(
        workdir_entries=[], final_message="", timed_out=True, target_name="PROBE_WRITE.txt"
    )
    assert v.verdict == p.INCONCLUSIVE


def test_missing_workdir_listing_is_not_treated_as_empty():
    """If we could not even look, we must not claim the directory was empty."""
    v = p.judge_readonly_write(
        workdir_entries=None, final_message="", target_name="PROBE_WRITE.txt"
    )
    assert v.verdict == p.INCONCLUSIVE


def test_paired_control_strengthens_a_refusal():
    readonly = p.judge_readonly_write(
        workdir_entries=[],
        final_message="REFUSED",
        errors=["denied: read-only"],
        target_name="PROBE_WRITE.txt",
    ).observations
    control = p.judge_readonly_write(
        workdir_entries=["PROBE_WRITE.txt"],
        final_message="WROTE",
        target_name="PROBE_WRITE.txt",
    ).observations
    v = p.judge_readonly_write_control(readonly, control, target_name="PROBE_WRITE.txt")
    assert v.verdict == p.PASS
    assert any("对照运行" in r for r in v.reasons)


def test_paired_control_that_also_fails_to_write_is_inconclusive():
    """If the control cannot write either, the measurement is broken, not the theory."""
    readonly = p.judge_readonly_write(
        workdir_entries=[], final_message="", target_name="PROBE_WRITE.txt"
    ).observations
    control = p.judge_readonly_write(
        workdir_entries=[], final_message="I cannot write files", target_name="PROBE_WRITE.txt"
    ).observations
    v = p.judge_readonly_write_control(readonly, control, target_name="PROBE_WRITE.txt")
    assert v.verdict == p.INCONCLUSIVE
    assert any("不可靠" in r for r in v.reasons)


def test_readonly_created_despite_control_is_a_failure():
    readonly = p.judge_readonly_write(
        workdir_entries=["PROBE_WRITE.txt"], final_message="WROTE", target_name="PROBE_WRITE.txt"
    ).observations
    control = readonly
    v = p.judge_readonly_write_control(readonly, control, target_name="PROBE_WRITE.txt")
    assert v.verdict == p.FAIL


# --------------------------------------------------------------------------- #
# probe 2: sub-agent blocking
# --------------------------------------------------------------------------- #


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
        {"type": "item.completed", "item": {"type": "collabAgentToolCall",
                                           "receiverThreadIds": ["t2"]}},
    ]
    v = p.judge_subagent_block(events=events, final_message="done")
    assert v.verdict == p.FAIL


def test_single_thread_plus_denial_passes_with_a_stated_limit():
    events = [{"type": "thread.started", "thread_id": "t1"}]
    v = p.judge_subagent_block(events=events, final_message="NO-SUBAGENT")
    assert v.verdict == p.PASS
    assert v.capability == "no_spawn_observed"
    assert any("不能证明" in r for r in v.reasons), "PASS must carry its limits"


def test_missing_disable_flag_invalidates_the_run():
    events = [{"type": "thread.started", "thread_id": "t1"}]
    v = p.judge_subagent_block(events=events, final_message="NO-SUBAGENT",
                              disable_flag_passed=False)
    assert v.verdict == p.INCONCLUSIVE


def test_silent_run_is_inconclusive():
    v = p.judge_subagent_block(events=[{"type": "thread.started", "thread_id": "t1"}],
                              final_message="done")
    assert v.verdict == p.INCONCLUSIVE


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
    """A model saying "RAN" is a claim, not event-level evidence."""
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
    """A plan must state cost, permissions and all three verdict criteria."""
    assert set(p.PROBE_SPECS) == {"readonly-write", "subagent-block", "tool-intercept", "cancel"}
    for spec in p.PROBE_SPECS.values():
        assert spec.prompt and len(spec.prompt) > 40
        assert spec.permissions and spec.cleanup and spec.limitations
        assert spec.pass_criteria and spec.fail_criteria and spec.inconclusive_criteria
        assert spec.sandbox in ("read-only", "workspace-write", "danger-full-access")
        assert spec.real_calls >= 1


def test_prompts_do_not_carry_stub_markers():
    """Stub mode switches on an environment variable, never on the prompt text.

    Otherwise the prompt a reviewer approves would differ from the real one.
    """
    for spec in p.PROBE_SPECS.values():
        assert "STUB_CODEX_PROBE_OUTCOME" not in spec.prompt
        assert "stub" not in spec.prompt.lower()


def test_cancel_is_marked_not_verifiable_in_this_mode():
    spec = p.PROBE_SPECS["cancel"]
    assert spec.verifiable_now is False
    assert "不具备验证条件" in spec.limitations


def test_probe_prompts_stay_within_read_only_sandbox_limits():
    """No probe except tool-intercept asks for a command, and that one asks for echo."""
    for name, spec in p.PROBE_SPECS.items():
        lowered = spec.prompt.lower()
        if name == "tool-intercept":
            assert "echo probe-ok" in lowered
        else:
            assert "run this shell command" not in lowered
