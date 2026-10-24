"""Token budgets: estimate, admission decision, settlement (T-1c).

Pure rules; the kernel applies them atomically (``agenthub_kernel.budget``).
Tokens are ledger tokens: ``input_total`` (cache read/write included) plus
``output_total`` (``agenthub.usage``).

What a budget can and cannot do here:

* **No hard per-call limit.** No adapter can stop a provider turn at N tokens,
  so the per-call budget is the amount each call RESERVES against the tree and
  the ceiling for the pre-send estimate. A call that spends more is recorded
  as an overrun, after the fact.
* **The estimate covers only what the Hub sends** (the call's input text).
  Provider-side context - instructions, tool definitions, thread history - is
  invisible and reported as unknown; it is never folded into the estimate.
* **Unknown is never zero.** A settled call whose usage is unknown keeps its
  reservation as an unsettled charge; one with no reservation behind it is
  "unbounded". Strict mode refuses new calls while anything is unbounded.
* **A budget switched on mid-flight.** A call admitted while the tree had no
  budget (never set, or switched off) reserved nothing. While it still runs,
  its spend is unknown with no reservation behind it, so it counts as
  unbounded too (``unreserved_in_flight``) until it settles; it keeps running
  and its approval is untouched.

Modes (``Policy.token_budget_mode``):

* ``strict`` - refuse a call whose agent does not report usage, a root budget
  without a per-call reservation amount, anything unbounded in the tree, and
  any reservation that would cross the total. Concurrent calls cannot
  together cross the remaining budget (the kernel reserves with a CAS).
* ``soft`` - only a known exhausted budget (known spend >= total) or an
  estimate above the per-call budget refuses; everything else is admitted
  with warnings, and the reservation falls back to the estimate.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Optional

STRICT = "strict"
SOFT = "soft"


def estimate_visible_tokens(text: str) -> int:
    """A rough upper-leaning estimate of the tokens the Hub itself sends.

    ASCII: about four characters per token; any other character: one token
    each (CJK text is often one or more tokens per character). An estimate,
    never a count; no tokenizer is installed (T-1d may add one per provider).
    """
    if not isinstance(text, str) or not text:
        return 0
    ascii_chars = sum(1 for ch in text if ord(ch) < 128)
    return math.ceil(ascii_chars / 4) + (len(text) - ascii_chars)


@dataclass(frozen=True)
class BudgetPolicy:
    total: Optional[int] = None
    per_call: Optional[int] = None
    warn_percent: int = 80
    mode: str = STRICT

    @property
    def active(self) -> bool:
        return self.total is not None or self.per_call is not None

    @classmethod
    def from_policy(cls, policy: Any) -> "BudgetPolicy":
        return cls(total=getattr(policy, "token_budget_total", None),
                   per_call=getattr(policy, "token_budget_per_call", None),
                   warn_percent=getattr(policy, "token_budget_warn_percent", 80),
                   mode=getattr(policy, "token_budget_mode", STRICT))

    def to_dict(self) -> dict[str, Any]:
        return {"total": self.total, "per_call": self.per_call,
                "warn_percent": self.warn_percent, "mode": self.mode}


@dataclass(frozen=True)
class TreeSpend:
    """Where a tree stands. ``known`` is settled known spend; ``unsettled`` the
    reservations kept by calls whose spend is not (fully) known; ``reserved``
    the reservations of calls still in flight; ``unbounded`` how many calls
    have unknown spend with no reservation behind it - settled ones, plus the
    ``unreserved_in_flight`` ones still running that were admitted before the
    tree had a budget."""

    known: int = 0
    unsettled: int = 0
    reserved: int = 0
    unbounded: int = 0
    unreserved_in_flight: int = 0

    @property
    def charged(self) -> int:
        return self.known + self.unsettled + self.reserved


@dataclass(frozen=True)
class BudgetDecision:
    allowed: bool
    code: str
    reserve: int = 0
    warnings: tuple = ()
    message: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"allowed": self.allowed, "code": self.code, "reserve": self.reserve,
                "warnings": list(self.warnings), "message": self.message}


def decide(budget: BudgetPolicy, spend: TreeSpend, *, estimate: int,
           metered: Optional[bool]) -> BudgetDecision:
    """May one more call start, and how much does it reserve?

    ``metered``: the serving adapter reports usage (``usage_report``); None
    when not known (treated as not metered).
    """
    if not budget.active:
        return BudgetDecision(True, "no_budget")
    warnings: list[str] = []
    if budget.per_call is not None and estimate > budget.per_call:
        return BudgetDecision(False, "estimate_exceeds_per_call_budget",
                              message=f"仅 Hub 可见部分的估算就有 {estimate} token，超过单次预算 {budget.per_call}")
    if budget.total is not None and spend.known >= budget.total:
        return BudgetDecision(False, "token_budget_exhausted",
                              message=f"已知用量 {spend.known} 已达到总预算 {budget.total}")
    if budget.mode == STRICT:
        if not metered:
            return BudgetDecision(False, "budget_unmeterable",
                                  message="该 Agent 不报告用量：严格预算模式下不启动")
        if spend.unbounded:
            return BudgetDecision(False, "budget_unknown_unbounded", message=_unbounded_message(spend))
        if budget.per_call is None:
            return BudgetDecision(False, "budget_needs_per_call",
                                  message="严格模式需要单次预算作为每次调用的预留量")
        reserve = budget.per_call
        if budget.total is not None and spend.charged + reserve > budget.total:
            return BudgetDecision(False, "token_budget_exceeded", reserve=reserve,
                                  message=(f"已知 {spend.known} + 未结算 {spend.unsettled} + 在途 {spend.reserved}"
                                           f" + 本次预留 {reserve} 超过总预算 {budget.total}"))
    else:
        if not metered:
            warnings.append("unmeterable")
        if spend.unbounded > spend.unreserved_in_flight:
            warnings.append("unknown_unbounded")
        if spend.unreserved_in_flight:
            warnings.append("unreserved_in_flight")
        reserve = budget.per_call if budget.per_call is not None else estimate
        if budget.per_call is None:
            warnings.append("reservation_is_estimate")
        if budget.total is not None and spend.charged + reserve > budget.total:
            warnings.append("projected_over_budget")
    if budget.total is not None and (spend.charged + reserve) * 100 >= budget.total * budget.warn_percent:
        warnings.append("warn_threshold")
    return BudgetDecision(True, "admitted", reserve=reserve, warnings=tuple(warnings))


def _unbounded_message(spend: TreeSpend) -> str:
    running = spend.unreserved_in_flight
    settled = spend.unbounded - running
    parts = []
    if settled:
        parts.append(f"{settled} 次已结束的调用用量未知且没有预留")
    if running:
        parts.append(f"{running} 次在预算开启前已获准的调用仍在执行、没有预留（结算后解除）")
    return f"本任务有{'，'.join(parts)}：严格模式下先处理或切换为软预算"


@dataclass(frozen=True)
class Settlement:
    known: int
    unsettled: int
    unbounded: int
    overrun: bool
    state: str  # settled | unsettled


def settle(reservation: int, usage: Optional[dict]) -> Settlement:
    """What one finished call charges, from its ledger view (or None).

    complete: its known tokens. partial: the known part, plus whatever of its
    reservation the known part has not used (it may have spent that much). unknown /
    no row: its whole reservation; with no reservation it is unbounded.
    """
    completeness = (usage or {}).get("completeness", "unknown")
    parts = [(usage or {}).get(k) for k in ("input_total", "output_total")]
    known = sum(p for p in parts if isinstance(p, int) and not isinstance(p, bool))
    overrun = bool(reservation) and known > reservation
    if completeness == "complete":
        return Settlement(known, 0, 0, overrun, "settled")
    if completeness == "partial":
        if reservation:
            return Settlement(known, max(reservation - known, 0), 0, overrun, "unsettled")
        return Settlement(known, 0, 1, overrun, "unsettled")
    if reservation:
        return Settlement(0, reservation, 0, False, "unsettled")
    return Settlement(0, 0, 1, False, "unsettled")
