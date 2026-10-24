"""T-1c: token budget rules (pure functions, offline)."""

import pytest

from agenthub.budget import (
    SOFT, STRICT, BudgetPolicy, TreeSpend, decide, estimate_visible_tokens, settle,
)
from agenthub.contracts import Policy


def budget(**kw):
    return BudgetPolicy(**{"mode": STRICT, **kw})


# ------------------------------------------------------------------ estimate


@pytest.mark.parametrize("text, tokens", [
    ("", 0), ("hello world", 3), ("用户原文", 4), ("ab用户", 3), ("x" * 400, 100),
])
def test_estimate_counts_only_the_text_the_hub_sends(text, tokens):
    assert estimate_visible_tokens(text) == tokens


# ------------------------------------------------------------------ decision


def test_no_budget_changes_nothing():
    d = decide(BudgetPolicy(), TreeSpend(known=10**9), estimate=10**6, metered=False)
    assert d.allowed and d.code == "no_budget" and d.reserve == 0


@pytest.mark.parametrize("mode", [STRICT, SOFT])
def test_an_estimate_over_the_per_call_budget_is_refused_in_both_modes(mode):
    d = decide(budget(per_call=100, mode=mode), TreeSpend(), estimate=101, metered=True)
    assert not d.allowed and d.code == "estimate_exceeds_per_call_budget"


@pytest.mark.parametrize("mode", [STRICT, SOFT])
def test_a_known_exhausted_budget_is_refused_in_both_modes(mode):
    d = decide(budget(total=1000, per_call=100, mode=mode), TreeSpend(known=1000), estimate=1, metered=True)
    assert not d.allowed and d.code == "token_budget_exhausted"


@pytest.mark.parametrize("spend, metered, per_call, code", [
    (TreeSpend(), False, 100, "budget_unmeterable"),
    (TreeSpend(unbounded=1), True, 100, "budget_unknown_unbounded"),
    (TreeSpend(), True, None, "budget_needs_per_call"),
    (TreeSpend(known=500, unsettled=200, reserved=250), True, 100, "token_budget_exceeded"),
])
def test_strict_refusals(spend, metered, per_call, code):
    d = decide(budget(total=1000, per_call=per_call), spend, estimate=1, metered=metered)
    assert not d.allowed and d.code == code and d.message


def test_strict_reserves_the_per_call_budget_and_warns_at_the_threshold():
    d = decide(budget(total=1000, per_call=100, warn_percent=80), TreeSpend(known=650, reserved=50),
               estimate=5, metered=True)
    assert d.allowed and d.reserve == 100 and "warn_threshold" in d.warnings  # 800 >= 80 %
    d = decide(budget(total=1000, per_call=100), TreeSpend(known=100), estimate=5, metered=True)
    assert d.allowed and d.warnings == ()


def test_strict_per_call_only_needs_metering_not_a_total():
    assert decide(budget(per_call=100), TreeSpend(known=10**6), estimate=5, metered=True).allowed
    assert not decide(budget(per_call=100), TreeSpend(), estimate=5, metered=False).allowed


def test_soft_admits_with_warnings():
    d = decide(budget(total=1000, mode=SOFT), TreeSpend(known=990, unbounded=1), estimate=50, metered=False)
    assert d.allowed and d.reserve == 50
    assert set(d.warnings) >= {"unmeterable", "unknown_unbounded", "reservation_is_estimate",
                               "projected_over_budget", "warn_threshold"}
    assert "unreserved_in_flight" not in d.warnings


def test_a_running_call_without_reservation_is_unbounded_risk():
    # Admitted before the budget was switched on: still running, nothing reserved.
    running = TreeSpend(unbounded=1, unreserved_in_flight=1)
    d = decide(budget(total=1000, per_call=100), running, estimate=1, metered=True)
    assert not d.allowed and d.code == "budget_unknown_unbounded"
    assert "仍在执行" in d.message and "已结束" not in d.message
    d = decide(budget(total=1000, per_call=100, mode=SOFT), running, estimate=1, metered=True)
    assert d.allowed and d.warnings == ("unreserved_in_flight",)
    both = TreeSpend(unbounded=3, unreserved_in_flight=1)
    d = decide(budget(total=1000, per_call=100), both, estimate=1, metered=True)
    assert "2 次已结束" in d.message and "1 次在预算开启前" in d.message
    d = decide(budget(total=1000, per_call=100, mode=SOFT), both, estimate=1, metered=True)
    assert d.warnings == ("unknown_unbounded", "unreserved_in_flight")


# ---------------------------------------------------------------- settlement


@pytest.mark.parametrize("reservation, usage, expected", [
    (100, {"completeness": "complete", "input_total": 60, "output_total": 10}, (70, 0, 0, False, "settled")),
    (100, {"completeness": "complete", "input_total": 150, "output_total": 10}, (160, 0, 0, True, "settled")),
    (100, {"completeness": "partial", "input_total": 60, "output_total": None}, (60, 40, 0, False, "unsettled")),
    (0, {"completeness": "partial", "input_total": 60, "output_total": 1}, (61, 0, 1, False, "unsettled")),
    (100, {"completeness": "unknown", "input_total": None, "output_total": None}, (0, 100, 0, False, "unsettled")),
    (100, None, (0, 100, 0, False, "unsettled")),
    (0, None, (0, 0, 1, False, "unsettled")),
    (0, {"completeness": "complete", "input_total": 0, "output_total": 0}, (0, 0, 0, False, "settled")),
])
def test_settlement_never_turns_unknown_into_zero(reservation, usage, expected):
    s = settle(reservation, usage)
    assert (s.known, s.unsettled, s.unbounded, s.overrun, s.state) == expected


# ---------------------------------------------------------------- policy


@pytest.mark.parametrize("kw", [
    {"token_budget_total": 0}, {"token_budget_per_call": -5}, {"token_budget_total": True},
    {"token_budget_total": 100, "token_budget_per_call": 101}, {"token_budget_mode": "hard"},
    {"token_budget_warn_percent": 0}, {"token_budget_warn_percent": 101},
])
def test_invalid_budget_policies_are_rejected(kw):
    with pytest.raises(ValueError):
        Policy(**kw)


def test_stored_policies_without_budget_keys_have_no_budget():
    old = {"max_subagents_total": 3, "max_subagents_concurrent": 2, "max_depth": 1,
           "max_agent_invocations": 10, "run_time_limit_seconds": 600, "root_task_ttl_seconds": 3600,
           "collaboration_enabled": False}
    assert BudgetPolicy.from_policy(Policy(**old)).active is False
    assert Policy(token_budget_total=5).as_dict()["token_budget_total"] == 5
