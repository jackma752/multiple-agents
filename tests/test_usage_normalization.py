"""T-1a: usage normalization and per-turn derivation (pure functions, offline).

Fixtures are the real numbers of docs/t0-usage-baseline.md §4 (numbers only).
"""

import pytest

from agenthub.usage import (
    COMPLETE, MAPPING_CLAUDE_CODE, MAPPING_CODEX_APPSERVER, MAPPING_CODEX_EXEC, MAPPING_NONE,
    PARTIAL, UNKNOWN, clean_notes, normalize_usage, not_launched_usage, numbers_only,
)
from agenthub_adapters.appserver_usage import turn_usage, update_record


def bd(inp, cached, out, reasoning=0, write=0):
    """A TokenUsageBreakdown with totalTokens = input + output (as observed)."""
    return {"inputTokens": inp, "cachedInputTokens": cached, "cacheWriteInputTokens": write,
            "outputTokens": out, "reasoningOutputTokens": reasoning, "totalTokens": inp + out}


def codex(usage, **extra):
    return normalize_usage({"mapping": MAPPING_CODEX_APPSERVER, "usage": usage,
                            "source": "thread_total_delta", **extra})


def claude(usage, **extra):
    return normalize_usage({"mapping": MAPPING_CLAUDE_CODE, "usage": usage, "source": "cli_result", **extra})


# S3 run 3: one turn, two requests (rollout lines 15 and 19).
S3_A, S3_B = bd(15211, 12416, 72), bd(15328, 12416, 7)
S3_TURN = bd(30539, 24832, 79)


# ------------------------------------------------------------- Codex mapping


def test_codex_input_already_contains_the_cache():
    rec = codex(bd(15043, 12544, 5))  # S2
    assert rec.completeness == COMPLETE
    assert (rec.input_total, rec.cache_read, rec.input_uncached, rec.output_total) == (15043, 12544, 2499, 5)
    assert rec.cache_write == 0 and rec.reasoning == 0


def test_codex_reasoning_is_part_of_the_output():
    rec = codex(bd(15661, 12544, 24, reasoning=16))  # 10-14: total 15,685 = 15,661 + 24
    assert rec.completeness == COMPLETE and rec.output_total == 24 and rec.reasoning == 16
    assert "mapping_mismatch" not in rec.notes


@pytest.mark.parametrize("usage", [
    {**bd(100, 10, 5), "totalTokens": 999},          # total is not input + output
    bd(100, 200, 5),                                 # cache larger than the input
    bd(100, 10, 5, reasoning=9),                     # reasoning larger than the output
])
def test_self_contradicting_numbers_are_partial(usage):
    rec = codex(usage)
    assert rec.completeness == PARTIAL and "mapping_mismatch" in rec.notes


def test_a_non_zero_cache_write_marks_the_mapping_unverified():
    rec = codex(bd(100, 10, 5, write=7))
    assert rec.completeness == PARTIAL and "mapping_unverified" in rec.notes
    assert rec.cache_write == 7 and rec.input_uncached is None  # not folded into any arithmetic
    assert rec.input_total == 100


def test_exec_usage_is_at_best_partial():
    rec = normalize_usage({"mapping": MAPPING_CODEX_EXEC, "source": "exec_turn_completed",
                           "notes": ["exec_usage_semantics_unverified"],
                           "usage": {"input_tokens": 14481, "cached_input_tokens": 12544,
                                     "cache_write_input_tokens": 0, "output_tokens": 5,
                                     "reasoning_output_tokens": 0}})
    assert rec.completeness == PARTIAL and rec.input_total == 14481 and rec.cache_read == 12544


# ------------------------------------------------------- Claude Code mapping


@pytest.mark.parametrize("usage, total", [
    ({"input_tokens": 2, "cache_creation_input_tokens": 635, "cache_read_input_tokens": 1161,
      "output_tokens": 15}, 1798),   # two-agent run 2: the page showed "2"
    ({"input_tokens": 2, "cache_creation_input_tokens": 1750, "cache_read_input_tokens": 0,
      "output_tokens": 4}, 1752),    # single turn run 1
])
def test_claude_code_total_input_adds_both_cache_fields(usage, total):
    rec = claude(usage)
    assert rec.completeness == COMPLETE and rec.input_total == total
    assert rec.input_uncached == 2 and rec.reasoning is None
    assert (rec.cache_write, rec.cache_read) == (usage["cache_creation_input_tokens"],
                                                 usage["cache_read_input_tokens"])


def test_claude_code_without_cache_fields_has_no_total():
    rec = claude({"input_tokens": 2, "output_tokens": 3})
    assert rec.completeness == PARTIAL and rec.input_total is None and rec.output_total == 3


# ------------------------------------------------------------ unknown, never 0


@pytest.mark.parametrize("report, note", [
    (None, "no_adapter_record"),
    ({"mapping": MAPPING_NONE}, "adapter_reports_no_usage"),
    ({"mapping": MAPPING_CODEX_APPSERVER, "usage": {}}, "no_usage_reported"),
    ({"mapping": MAPPING_CODEX_APPSERVER, "usage": {"inputTokens": "many"}}, "usage_malformed"),
    ({"mapping": MAPPING_CLAUDE_CODE, "usage": {"input_tokens": True, "output_tokens": 1}}, "usage_malformed"),
    ({"mapping": MAPPING_CODEX_APPSERVER, "usage": bd(5, 0, 1), "notes": ["turn_id_reused"]}, "turn_id_reused"),
    ("not a dict", "usage_malformed"),
])
def test_missing_or_unattributable_usage_is_unknown_not_zero(report, note):
    rec = normalize_usage(report)
    assert rec.completeness == UNKNOWN and note in rec.notes
    assert all(getattr(rec, f) is None for f in
               ("input_total", "input_uncached", "cache_read", "cache_write", "output_total", "reasoning"))


def test_not_launched_is_a_known_zero():
    rec = not_launched_usage()
    assert rec.completeness == COMPLETE and rec.input_total == 0 and rec.output_total == 0


def test_notes_and_raw_never_carry_text():
    assert clean_notes(["late_usage", "the model said: hello", "late_usage", 5]) == \
        ["late_usage", "invalid_note_dropped"]
    raw = numbers_only({"input_tokens": 2, "service_tier": "standard", "note": "free text",
                        "server_tool_use": {"web_search_requests": 0}, "flag": True,
                        "modelUsage": {"claude-opus-5-5": {"inputTokens": 2, "label": "x"}}})
    assert raw == {"input_tokens": 2, "service_tier": "standard",
                   "server_tool_use": {"web_search_requests": 0},
                   "modelUsage": {"claude-opus-5-5": {"inputTokens": 2}}}
    assert numbers_only({"service_tier": "Not An Enum!"}) == {}


# ------------------------------------------- one turn from tokenUsage/updated


def updates(*pairs, late_from=None):
    out = []
    for i, (last, total) in enumerate(pairs):
        out.append(update_record({"last": last, "total": total}, late=late_from is not None and i >= late_from))
    return out


def test_s3_two_requests_count_both_d_t0_1():
    usage, source, notes = turn_usage(updates((S3_A, S3_A), (S3_B, S3_TURN)), resumed=False, replay_total=None)
    assert usage == S3_TURN and source == "thread_total_delta" and notes == []
    assert usage["inputTokens"] != S3_B["inputTokens"]  # the old rule kept the last request only
    assert codex(usage).completeness == COMPLETE


def test_a_missed_update_still_counts_the_turn_but_is_partial():
    usage, source, notes = turn_usage(updates((S3_B, S3_TURN)), resumed=False, replay_total=None)
    assert usage == S3_TURN and "last_sum_mismatch" in notes
    assert codex(usage, notes=notes).completeness == PARTIAL


@pytest.mark.parametrize("replay_total, last, total, expected", [
    # S6-3 call 2: replay total 15,785; thread total 32,089 after the resumed turn.
    (bd(15785, 12416, 7), bd(16304, 15616, 13), bd(32089, 28032, 20), bd(16304, 15616, 13)),
    # S5 run 2 recall: replay total 15,751; thread total 32,021.
    (bd(15751, 12416, 7), bd(16270, 12416, 11), bd(32021, 24832, 18), bd(16270, 12416, 11)),
])
def test_a_resumed_turn_starts_at_the_replay_total(replay_total, last, total, expected):
    usage, source, notes = turn_usage(updates((last, total)), resumed=True, replay_total=replay_total)
    assert usage == expected and source == "thread_total_delta" and notes == []


def test_a_resume_without_a_replay_falls_back_to_the_request_sum():
    last, total = bd(16304, 15616, 13), bd(32089, 28032, 20)
    usage, source, notes = turn_usage(updates((last, total)), resumed=True, replay_total=None)
    assert usage == last and source == "sum_of_last" and "resume_baseline_unknown" in notes
    assert codex(usage, notes=notes).completeness == PARTIAL


def test_a_duplicate_delivery_counts_once():
    one = (bd(120, 64, 3), bd(120, 64, 3))
    usage, _source, notes = turn_usage(updates(one, one), resumed=False, replay_total=None)
    assert usage == bd(120, 64, 3) and notes == ["duplicate_usage_update"]
    assert codex(usage, notes=notes).completeness == COMPLETE


def test_no_update_is_unknown():
    usage, source, notes = turn_usage([], resumed=False, replay_total=None)
    assert usage == {} and source == "none" and "no_usage_reported" in notes


def test_late_and_malformed_updates_make_it_partial():
    usage, _s, notes = turn_usage(updates((S3_A, S3_A), (S3_B, S3_TURN), late_from=1),
                                  resumed=False, replay_total=None)
    assert usage == S3_TURN and "late_usage" in notes
    bad = [update_record({"last": {"inputTokens": 1}, "total": None})] + updates((S3_A, S3_A))
    usage, _s, notes = turn_usage(bad, resumed=False, replay_total=None)
    assert usage == S3_A and "usage_update_malformed" in notes
    assert codex(usage, notes=notes).completeness == PARTIAL


def test_a_decreasing_total_is_not_trusted():
    usage, source, notes = turn_usage(updates((bd(10, 0, 1), bd(10, 0, 1))), resumed=True,
                                      replay_total=bd(500, 0, 5))
    assert source == "sum_of_last" and "total_decreased" in notes and usage == bd(10, 0, 1)
