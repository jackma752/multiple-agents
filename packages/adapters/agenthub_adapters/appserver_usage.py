"""One turn's usage from app-server ``thread/tokenUsage/updated`` (T-1a, D-T0-1).

Each update carries ``last`` (ONE model request) and ``total`` (the thread's
running total, carried across a cold resume). A turn with several requests -
a tool call, a declined command and a second answer - sends several updates,
so the last ``last`` is not the turn: S3 run 3 spent 30,539 input / 79 output
and only 15,328 / 7 had been kept (docs/t0-usage-baseline.md §4).

The turn's usage is the thread total at its end minus the total before it:

* a new thread starts at zero (every new thread's first update has
  ``total == last``, 14 real threads);
* a resumed thread starts at the total of the one replay the server sends for
  the previous turn (recorded apart, never this turn's usage, D9).

The sum of the kept updates' ``last`` is an independent second derivation;
when both agree the record is complete. A disagreement, a missing baseline, a
late update or an unfinished turn is reported with a note so the record is
``partial`` - never silently rounded to whichever number looks right. An
update repeated with the same ``total`` is one update delivered twice.
"""

from __future__ import annotations

from typing import Any, Optional

from agenthub.usage import is_count

#: TokenUsageBreakdown fields (alpha.11 / alpha.11.2 schema).
FIELDS = ("inputTokens", "cachedInputTokens", "cacheWriteInputTokens",
          "outputTokens", "reasoningOutputTokens", "totalTokens")
REQUIRED = ("inputTokens", "cachedInputTokens", "outputTokens")


def breakdown(value: Any) -> Optional[dict]:
    """The numeric fields of a TokenUsageBreakdown, or None when malformed."""
    if not isinstance(value, dict) or not all(is_count(value.get(k)) for k in REQUIRED):
        return None
    return {k: value[k] for k in FIELDS if is_count(value.get(k))}


def update_record(token_usage: Any, *, late: bool = False) -> dict:
    """What is kept of one ``tokenUsage`` object: numbers only."""
    token_usage = token_usage if isinstance(token_usage, dict) else {}
    record = {"last": breakdown(token_usage.get("last")), "total": breakdown(token_usage.get("total"))}
    if late:
        record["late"] = True
    return record


def _combine(parts: list[dict], sign: int = 1, base: Optional[dict] = None) -> dict:
    """Field-wise sum (``sign=1``) of ``parts``, or ``parts[0] - base``; a field
    missing anywhere is left out rather than guessed."""
    keys = [k for k in FIELDS if all(k in p for p in parts) and (base is None or k in base)]
    if sign < 0:
        return {k: parts[0][k] - base[k] for k in keys}
    return {k: sum(p[k] for p in parts) for k in keys}


def turn_usage(updates: list[dict], *, resumed: bool,
               replay_total: Optional[dict]) -> tuple[dict, str, list[str]]:
    """``(usage, source, notes)`` for one turn.

    ``updates``: :func:`update_record` dicts in arrival order, this turn's only.
    ``replay_total``: the replay's ``total`` (resume), else ignored.
    """
    notes: list[str] = []
    kept: list[dict] = []
    for update in updates:
        if update.get("last") is None or update.get("total") is None:
            if "usage_update_malformed" not in notes:
                notes.append("usage_update_malformed")
            continue
        if kept and update["total"] == kept[-1]["total"]:
            if "duplicate_usage_update" not in notes:
                notes.append("duplicate_usage_update")
            continue  # the same update delivered twice
        if update.get("late") and "late_usage" not in notes:
            notes.append("late_usage")
        kept.append(update)
    if not kept:
        return {}, "none", notes + ["no_usage_reported"]
    summed = _combine([u["last"] for u in kept])
    if not resumed:
        baseline: Optional[dict] = {k: 0 for k in FIELDS}
    else:
        baseline = breakdown(replay_total)
        if baseline is None:
            return summed, "sum_of_last", notes + ["sum_of_last", "resume_baseline_unknown"]
    delta = _combine([kept[-1]["total"]], sign=-1, base=baseline)
    if any(v < 0 for v in delta.values()) or not all(k in delta for k in REQUIRED):
        return summed, "sum_of_last", notes + ["sum_of_last", "total_decreased"]
    if any(delta.get(k) != summed.get(k) for k in REQUIRED):
        # A request whose update never arrived shows up only in the total.
        notes.append("last_sum_mismatch")
    return delta, "thread_total_delta", notes
