"""Normalized token usage (T-1a; field semantics from docs/t0-usage-baseline.md).

Providers report usage under different meanings, so their numbers are never
shown side by side or added up raw:

* Codex (app-server ``TokenUsageBreakdown``, exec ``turn.completed.usage``):
  ``inputTokens`` already contains ``cachedInputTokens``, ``outputTokens``
  already contains ``reasoningOutputTokens``, and ``totalTokens`` is input +
  output. ``cacheWriteInputTokens`` has only ever been 0; whether it is part
  of the input is unverified, so a non-zero value marks the mapping
  unverified instead of being folded into any arithmetic.
* Claude Code (``-p --output-format json`` result ``usage``): ``input_tokens``
  excludes the cache. The total input is ``input_tokens +
  cache_creation_input_tokens + cache_read_input_tokens``.

A missing number is ``None`` (unknown), never 0. ``completeness`` says how far
a record can be trusted: ``complete``, ``partial`` (some numbers known, the
call may have spent more) or ``unknown``. Adapters explain themselves with
short machine codes (``notes``), never free text; the codes below decide the
completeness. ``raw`` keeps the provider's numbers (whitelisted keys, numbers
only) so a later mapping version can re-read them. Records keep the mapping
version they were made with and are never reinterpreted.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any, Optional

#: Bump when a mapping's meaning changes; stored records keep their version.
USAGE_MAPPING_VERSION = 2

MAPPING_CODEX_APPSERVER = "codex.app_server.token_usage_breakdown"
MAPPING_CODEX_EXEC = "codex.exec.turn_completed_usage"
MAPPING_CLAUDE_CODE = "claude_code.result_usage"
#: DeepSeek (OpenAI-compatible) ``usage`` object from the final stream chunk.
MAPPING_DEEPSEEK = "deepseek.chat_completions.usage"
MAPPING_NONE = "none"
MAPPINGS = frozenset({MAPPING_CODEX_APPSERVER, MAPPING_CODEX_EXEC, MAPPING_CLAUDE_CODE,
                      MAPPING_DEEPSEEK, MAPPING_NONE})

COMPLETE = "complete"
PARTIAL = "partial"
UNKNOWN = "unknown"
COMPLETENESS = (UNKNOWN, PARTIAL, COMPLETE)  # ascending trust

#: Any of these makes the record ``unknown``.
UNKNOWN_NOTES = frozenset({
    "no_usage_reported",        # the provider sent nothing for this call
    "usage_malformed",          # required numbers missing or not numbers
    "turn_id_reused",           # a resumed turn reused the previous id: not attributable
    "adapter_reports_no_usage",  # the adapter has no usage channel (e.g. Mock)
    "no_adapter_record",        # the adapter knows nothing about this call
    "adapter_start_raised",     # the launch raised; nothing is known
})
#: Any of these (and none of the above) makes the record ``partial``.
PARTIAL_NOTES = frozenset({
    "sum_of_last",              # per-request sums used: a missed request is not seen
    "usage_update_malformed",   # one of the updates could not be read
    "resume_baseline_unknown",  # resumed without a usable replay total
    "last_sum_mismatch",        # per-request sum and thread-total delta disagree
    "total_decreased",          # the thread total went down
    "late_usage",               # usage arrived after the terminal; more may follow
    "turn_not_terminal",        # the turn never ended (timeout, disconnect)
    "native_children_not_metered",  # native child threads spend outside this record
    "legacy_last",              # recorded before T-1a: the last request only
    "mapping_unverified",       # a field whose meaning is unverified is non-zero
    "mapping_mismatch",         # the provider's own numbers contradict each other
    "cache_fields_missing",     # Claude Code result without its cache fields
    "exec_usage_semantics_unverified",  # exec: per-turn vs cumulative unverified
    "multi_model_usage_unverified",     # several models: aggregation unverified
})

_NOTE = re.compile(r"^[a-z0-9_.:-]{1,64}$")
_KEY = re.compile(r"^[A-Za-z0-9_.:/-]{1,80}$")
_MODEL = re.compile(r"^[A-Za-z0-9._:/@-]{1,80}$")
_ENUM = re.compile(r"^[a-z0-9_]{1,32}$")
#: String values kept in ``raw`` (short enums only); every other string is dropped.
RAW_STRING_KEYS = frozenset({"service_tier"})


@dataclass(frozen=True)
class UsageRecord:
    """One call's usage in Hub terms. ``None`` means unknown, never zero."""

    mapping: str
    completeness: str
    source: str
    input_total: Optional[int] = None
    input_uncached: Optional[int] = None
    cache_read: Optional[int] = None
    cache_write: Optional[int] = None
    output_total: Optional[int] = None
    reasoning: Optional[int] = None
    notes: tuple = ()
    raw: dict = field(default_factory=dict)
    model: Optional[str] = None
    mapping_version: int = USAGE_MAPPING_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "mapping": self.mapping, "mapping_version": self.mapping_version,
            "completeness": self.completeness, "source": self.source,
            "input_total": self.input_total, "input_uncached": self.input_uncached,
            "cache_read": self.cache_read, "cache_write": self.cache_write,
            "output_total": self.output_total, "reasoning": self.reasoning,
            "notes": list(self.notes), "model": self.model,
        }


def completeness_rank(value: str) -> int:
    return COMPLETENESS.index(value) if value in COMPLETENESS else 0


def is_count(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def clean_notes(notes: Any) -> list[str]:
    """Machine codes only; anything else is replaced by one marker code."""
    out: list[str] = []
    for note in notes if isinstance(notes, (list, tuple)) else ():
        code = note if isinstance(note, str) and _NOTE.match(note) else "invalid_note_dropped"
        if code not in out:
            out.append(code)
    return out


def numbers_only(value: Any, depth: int = 0) -> Any:
    """Copy keeping whitelisted-shape keys and numbers (and short enum strings)."""
    if not isinstance(value, dict) or depth > 3:
        return {}
    out: dict[str, Any] = {}
    for key, item in value.items():
        if not isinstance(key, str) or not _KEY.match(key):
            continue
        if isinstance(item, bool):
            continue
        if isinstance(item, int) or (isinstance(item, float) and math.isfinite(item)):
            out[key] = item
        elif isinstance(item, dict):
            nested = numbers_only(item, depth + 1)
            if nested:
                out[key] = nested
        elif key in RAW_STRING_KEYS and isinstance(item, str) and _ENUM.match(item):
            out[key] = item
    return out


def _codex(usage: dict, names: dict[str, str], notes: list[str]) -> dict[str, Optional[int]]:
    get = {k: usage.get(v) for k, v in names.items()}
    if not all(is_count(get[k]) for k in ("input", "cached", "output")):
        notes.append("usage_malformed")
        return {}
    inp, cached, out = get["input"], get["cached"], get["output"]
    write = get["write"] if is_count(get["write"]) else None
    reasoning = get["reasoning"] if is_count(get["reasoning"]) else None
    total = get["total"] if is_count(get["total"]) else None
    if cached > inp or (reasoning is not None and reasoning > out) \
            or (total is not None and total != inp + out):
        notes.append("mapping_mismatch")
    if write:
        notes.append("mapping_unverified")
    return {
        "input_total": inp,
        # Only when nothing of unverified meaning is non-zero.
        "input_uncached": inp - cached if not write and cached <= inp else None,
        "cache_read": cached, "cache_write": write,
        "output_total": out, "reasoning": reasoning,
    }


CODEX_APPSERVER_NAMES = {"input": "inputTokens", "cached": "cachedInputTokens",
                         "write": "cacheWriteInputTokens", "output": "outputTokens",
                         "reasoning": "reasoningOutputTokens", "total": "totalTokens"}
CODEX_EXEC_NAMES = {"input": "input_tokens", "cached": "cached_input_tokens",
                    "write": "cache_write_input_tokens", "output": "output_tokens",
                    "reasoning": "reasoning_output_tokens", "total": "total_tokens"}


def _claude_code(usage: dict, notes: list[str]) -> dict[str, Optional[int]]:
    inp, out = usage.get("input_tokens"), usage.get("output_tokens")
    if not (is_count(inp) and is_count(out)):
        notes.append("usage_malformed")
        return {}
    write, read = usage.get("cache_creation_input_tokens"), usage.get("cache_read_input_tokens")
    write = write if is_count(write) else None
    read = read if is_count(read) else None
    if write is None or read is None:
        notes.append("cache_fields_missing")
    details = usage.get('output_tokens_details')
    thinking = details.get('thinking_tokens') if isinstance(details, dict) else None
    if thinking is not None and (not is_count(thinking) or thinking > out):
        notes.append('mapping_mismatch')
        thinking = None
    return {
        "input_total": inp + write + read if write is not None and read is not None else None,
        "input_uncached": inp, "cache_read": read, "cache_write": write,
        "output_total": out, "reasoning": thinking,
    }


def _deepseek(usage: dict, notes: list[str]) -> dict[str, Optional[int]]:
    """DeepSeek ``usage``: prompt/completion totals, cache hit and reasoning.

    ``prompt_tokens`` already includes the cache-hit tokens (the provider bills
    them at two rates, it does not send them twice), so ``input_uncached`` is
    the miss count. There is no cache-*write* charge in this API, so that field
    stays unknown rather than being claimed as a zero.
    """
    prompt, completion = usage.get("prompt_tokens"), usage.get("completion_tokens")
    if not (is_count(prompt) and is_count(completion)):
        notes.append("usage_malformed")
        return {}
    hit, miss = usage.get("prompt_cache_hit_tokens"), usage.get("prompt_cache_miss_tokens")
    hit = hit if is_count(hit) else None
    miss = miss if is_count(miss) else None
    if hit is None:
        notes.append("cache_fields_missing")
    elif hit > prompt:
        notes.append("mapping_mismatch")
    uncached = miss if miss is not None else (prompt - hit if hit is not None else None)
    if uncached is not None and hit is not None and uncached + hit != prompt:
        notes.append("mapping_mismatch")
    total = usage.get("total_tokens")
    if is_count(total) and total != prompt + completion:
        notes.append("mapping_mismatch")
    reasoning = None
    details = usage.get("completion_tokens_details")
    if isinstance(details, dict):
        value = details.get("reasoning_tokens")
        reasoning = value if is_count(value) else None
    return {
        "input_total": prompt,
        "input_uncached": uncached,
        "cache_read": hit,
        "cache_write": None,
        "output_total": completion,
        "reasoning": reasoning,
    }


def judge_completeness(notes: list[str]) -> str:
    if any(n in UNKNOWN_NOTES for n in notes):
        return UNKNOWN
    if any(n in PARTIAL_NOTES for n in notes):
        return PARTIAL
    return COMPLETE


def unknown_usage(note: str, *, source: str = "none") -> UsageRecord:
    return UsageRecord(mapping=MAPPING_NONE, completeness=UNKNOWN, source=source, notes=(note,))


def not_launched_usage() -> UsageRecord:
    """A call its adapter refused before launching anything: known zero.

    Only valid where the adapter's own record proves it (it records every call
    before launching, and has none) - never inferred across a restart.
    """
    return UsageRecord(mapping=MAPPING_NONE, completeness=COMPLETE, source="not_launched",
                       input_total=0, input_uncached=0, cache_read=0, cache_write=0,
                       output_total=0, reasoning=0)


def normalize_usage(report: Optional[dict]) -> UsageRecord:
    """Map an adapter's ``usage_report`` to a :class:`UsageRecord`.

    ``report``: ``{"mapping", "usage", "source", "notes", "model"}``. ``None``
    means the adapter has no record of the call.
    """
    if report is None:
        return unknown_usage("no_adapter_record")
    if not isinstance(report, dict):
        return unknown_usage("usage_malformed")
    mapping = report.get("mapping") if report.get("mapping") in MAPPINGS else MAPPING_NONE
    source = report.get("source")
    source = source if isinstance(source, str) and _NOTE.match(source) else "none"
    notes = clean_notes(report.get("notes"))
    usage = report.get("usage")
    usage = usage if isinstance(usage, dict) else {}
    fields: dict[str, Optional[int]] = {}
    if mapping == MAPPING_NONE:
        if not notes:
            notes.append("adapter_reports_no_usage")
    elif not usage:
        if "no_usage_reported" not in notes:
            notes.append("no_usage_reported")
    elif mapping == MAPPING_CLAUDE_CODE:
        fields = _claude_code(usage, notes)
    elif mapping == MAPPING_DEEPSEEK:
        fields = _deepseek(usage, notes)
    else:
        names = CODEX_APPSERVER_NAMES if mapping == MAPPING_CODEX_APPSERVER else CODEX_EXEC_NAMES
        fields = _codex(usage, names, notes)
    model = report.get("model")
    model = model if isinstance(model, str) and _MODEL.match(model) else None
    notes = clean_notes(notes)
    completeness = judge_completeness(notes)
    if completeness == UNKNOWN:
        fields = {}  # an unattributable or malformed number is not shown as if it were known
    return UsageRecord(mapping=mapping, completeness=completeness, source=source,
                       notes=tuple(notes), raw=numbers_only(usage), model=model, **fields)
