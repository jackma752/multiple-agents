"""Atomic token-budget admission and settlement (T-1c; rules in ``agenthub.budget``).

* :func:`admit` runs inside :meth:`PolicyService.consume`, after the slot
  reservation and before the approval is consumed. The reservation is one
  conditional UPDATE on ``tree_token_budget`` whose WHERE clause encodes the
  budget, so two calls cannot together cross the same remaining budget. A
  refusal is raised as a ``QuotaError`` and the caller rolls back: no slot,
  no reservation, approval untouched, adapter start count 0.
* :func:`settle_budget` runs wherever a call's ledger row is written (same
  transaction): the reservation leaves flight and the call charges what the
  ledger knows. A later, better row re-settles by the difference, so the
  counters always equal the sum of the per-call items.
* The counters of a tree are created lazily, the first time a budget applies,
  from the tree's existing ledger rows: spend before the budget was set
  counts too.
* Every budgeted admission first reconciles the tree (:func:`ensure_counters`):
  a call with no item yet - settled, or still running because it was admitted
  while the tree had no budget (first switch-on, or switched off and on
  again) - is brought onto the counters. A running one reserved nothing, so
  it counts as unbounded (state ``unreserved``) until it settles. Views
  compute the same reconciliation without writing (:func:`spend_preview`),
  so the approval card's preview and the admission see the same numbers.

Nothing here reads prompt or reply text; the estimate is the approved
snapshot's context-preview estimate (T-1d), or for older snapshots the
estimate of its input text, and is stored as a number.
"""

from __future__ import annotations

from typing import Any, Optional

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from agenthub.budget import STRICT, BudgetDecision, BudgetPolicy, TreeSpend, decide, settle
from agenthub.context import visible_estimate
from agenthub.contracts import Policy
from agenthub_kernel.db import (
    InvocationRow, TaskRow, TokenBudgetItemRow, TreeTokenBudgetRow, UsageLedgerRow,
)

#: Statuses after which a call's spend is settled into the counters.
SETTLED_STATUSES = ("succeeded", "failed", "canceled", "timed_out", "unknown")
#: Approval consumed, not settled yet: the call is spending now.
IN_FLIGHT_STATUSES = ("starting", "running")
#: Item state of a running call admitted without a budget (nothing reserved).
UNRESERVED = "unreserved"


def tree_policy(session: Session, root_task_id: str) -> Policy:
    root = session.get(TaskRow, root_task_id)
    if root is not None and root.policy:
        return Policy(**{k: v for k, v in root.policy.items() if k != "version"})
    return Policy()


def _usage(session: Session, invocation_id: str) -> Optional[dict[str, Any]]:
    row = session.get(UsageLedgerRow, invocation_id)
    if row is None:
        return None
    return {"completeness": row.completeness, "input_total": row.input_total,
            "output_total": row.output_total}


def _counters(session: Session, root_task_id: str) -> Optional[TreeSpend]:
    row = session.execute(select(
        TreeTokenBudgetRow.known, TreeTokenBudgetRow.unsettled,
        TreeTokenBudgetRow.reserved, TreeTokenBudgetRow.unbounded,
    ).where(TreeTokenBudgetRow.root_task_id == root_task_id)).first()
    if row is None:
        return None
    running = session.scalar(select(func.count()).select_from(TokenBudgetItemRow).where(
        TokenBudgetItemRow.root_task_id == root_task_id, TokenBudgetItemRow.state == UNRESERVED))
    return TreeSpend(*row, unreserved_in_flight=running or 0)


def _unrecorded(session: Session, root_task_id: str) -> list[InvocationRow]:
    """The tree's settled or running calls that have no budget item yet."""
    return session.scalars(
        select(InvocationRow).join(TaskRow, TaskRow.id == InvocationRow.task_id)
        .outerjoin(TokenBudgetItemRow, TokenBudgetItemRow.invocation_id == InvocationRow.id)
        .where(TaskRow.root_task_id == root_task_id, TokenBudgetItemRow.invocation_id.is_(None),
               InvocationRow.status.in_(SETTLED_STATUSES + IN_FLIGHT_STATUSES))
        .order_by(InvocationRow.created_at, InvocationRow.id)
    ).all()


def ensure_counters(session: Session, root_task_id: str) -> None:
    """Create the tree's counters if needed and bring every unrecorded call onto them.

    Settled calls charge what the ledger knows. Running calls were admitted
    while the tree had no budget and reserved nothing: each is recorded as
    ``unreserved`` and counts as unbounded until it settles, when
    :func:`settle_budget` replaces that with its real charge (once). Runs in
    the claim's ``BEGIN IMMEDIATE`` transaction, so no settlement can slip
    between this reconciliation and the reservation CAS.
    """
    if session.get(TreeTokenBudgetRow, root_task_id) is None:
        session.add(TreeTokenBudgetRow(root_task_id=root_task_id, known=0, unsettled=0,
                                       reserved=0, unbounded=0))
        session.flush()
    for inv in _unrecorded(session, root_task_id):
        if inv.status in SETTLED_STATUSES:
            _settle_item(session, root_task_id, inv.id)
        else:
            _record_unreserved(session, root_task_id, inv.id)


def _record_unreserved(session: Session, root_task_id: str, invocation_id: str) -> None:
    # Accounting only: the call keeps running and its approval is not touched.
    session.add(TokenBudgetItemRow(invocation_id=invocation_id, root_task_id=root_task_id,
                                   estimate=0, reservation=0, reserved=0, known=0, unsettled=0,
                                   unbounded=1, overrun=False, state=UNRESERVED, warnings=[]))
    session.execute(
        update(TreeTokenBudgetRow).where(TreeTokenBudgetRow.root_task_id == root_task_id)
        .values(unbounded=TreeTokenBudgetRow.unbounded + 1)
        .execution_options(synchronize_session=False))
    session.flush()


def spend_preview(session: Session, root_task_id: str) -> TreeSpend:
    """The tree's spend after :func:`ensure_counters`, without writing anything (for views)."""
    base = _counters(session, root_task_id) or TreeSpend()
    known, unbounded, running = base.known, base.unbounded, base.unreserved_in_flight
    for inv in _unrecorded(session, root_task_id):
        if inv.status in SETTLED_STATUSES:
            s = settle(0, _usage(session, inv.id))
            known, unbounded = known + s.known, unbounded + s.unbounded
        else:
            unbounded, running = unbounded + 1, running + 1
    return TreeSpend(known=known, unsettled=base.unsettled, reserved=base.reserved,
                     unbounded=unbounded, unreserved_in_flight=running)


def admit(session: Session, inv: InvocationRow, root_task_id: str, policy: Policy, *,
          metered: Optional[bool]) -> Optional[BudgetDecision]:
    """Decide and reserve atomically; None when the tree has no budget."""
    budget = BudgetPolicy.from_policy(policy)
    if not budget.active:
        return None
    ensure_counters(session, root_task_id)
    estimate = visible_estimate(inv.snapshot)
    decision = decide(budget, _counters(session, root_task_id), estimate=estimate, metered=metered)
    if not decision.allowed:
        return decision
    reserve = decision.reserve
    conditions = [TreeTokenBudgetRow.root_task_id == root_task_id]
    if budget.total is not None:
        # Both modes: a budget known to be used up never admits.
        conditions.append(TreeTokenBudgetRow.known < budget.total)
        if budget.mode == STRICT:
            conditions.append(TreeTokenBudgetRow.known + TreeTokenBudgetRow.unsettled
                              + TreeTokenBudgetRow.reserved + reserve <= budget.total)
    if budget.mode == STRICT:
        conditions.append(TreeTokenBudgetRow.unbounded == 0)
    result = session.execute(
        update(TreeTokenBudgetRow).where(*conditions)
        .values(reserved=TreeTokenBudgetRow.reserved + reserve)
        .execution_options(synchronize_session=False))
    if result.rowcount != 1:
        # Another call took the remaining budget between the read and the write.
        return BudgetDecision(False, "token_budget_exceeded", reserve=reserve,
                              message="预算余量已被并发调用占用")
    session.add(TokenBudgetItemRow(
        invocation_id=inv.id, root_task_id=root_task_id, estimate=estimate,
        reservation=reserve, reserved=reserve, state="reserved", mode=budget.mode,
        warnings=list(decision.warnings)))
    session.flush()
    return decision


def _settle_item(session: Session, root_task_id: str, invocation_id: str) -> None:
    item = session.get(TokenBudgetItemRow, invocation_id)
    if item is None:
        # Admitted before the tree had a budget: nothing was reserved.
        item = TokenBudgetItemRow(invocation_id=invocation_id, root_task_id=root_task_id,
                                  estimate=0, reservation=0, reserved=0, known=0, unsettled=0,
                                  unbounded=0, overrun=False, state="reserved", warnings=[])
        session.add(item)
        session.flush()
    s = settle(item.reservation, _usage(session, invocation_id))
    deltas = {"reserved": -item.reserved, "known": s.known - item.known,
              "unsettled": s.unsettled - item.unsettled, "unbounded": s.unbounded - item.unbounded}
    if any(deltas.values()):
        session.execute(
            update(TreeTokenBudgetRow).where(TreeTokenBudgetRow.root_task_id == root_task_id)
            .values(**{name: getattr(TreeTokenBudgetRow, name) + delta
                       for name, delta in deltas.items() if delta})
            .execution_options(synchronize_session=False))
    item.reserved, item.known, item.unsettled, item.unbounded = 0, s.known, s.unsettled, s.unbounded
    item.overrun, item.state = s.overrun, s.state
    session.flush()


def settle_budget(session: Session, invocation_id: str) -> None:
    """Move a call's reservation out of flight and charge what the ledger knows.

    Idempotent; a no-op for trees without counters (they are built from the
    ledger when a budget first applies).
    """
    inv = session.get(InvocationRow, invocation_id)
    task = session.get(TaskRow, inv.task_id) if inv is not None else None
    if task is None or session.get(TreeTokenBudgetRow, task.root_task_id) is None:
        return
    _settle_item(session, task.root_task_id, invocation_id)


def estimate_check(session: Session, root_task_id: Optional[str], estimate: int) -> Optional[dict[str, Any]]:
    """What the tree's budget would say about a call with this estimate (composer
    preview; assumes the agent reports usage). None without a budget. Nothing is
    reserved, trimmed or widened here: an over-budget preview asks the user to
    narrow the references."""
    if root_task_id is None:
        return None
    budget = BudgetPolicy.from_policy(tree_policy(session, root_task_id))
    if not budget.active:
        return None
    decision = decide(budget, spend_preview(session, root_task_id), estimate=estimate, metered=True)
    return {**budget.to_dict(), "estimate": estimate, "decision": decision.to_dict()}


def item_view(item: Optional[TokenBudgetItemRow]) -> Optional[dict[str, Any]]:
    if item is None:
        return None
    return {"estimate_visible": item.estimate, "reservation": item.reservation,
            "reserved": item.reserved, "known": item.known, "unsettled": item.unsettled,
            "unbounded": item.unbounded, "overrun": item.overrun, "state": item.state,
            "mode": item.mode, "warnings": list(item.warnings or [])}


def budget_view(session: Session, root_task_id: Optional[str], *,
                inv: Optional[InvocationRow] = None) -> dict[str, Any]:
    """What an approval card or a usage bar shows: policy, estimate, spend.

    ``preview`` is the admission decision this call WOULD get now, assuming its
    agent reports usage (``assumes_usage_reporting``); the Worker decides again,
    atomically, when the call runs.
    """
    policy = tree_policy(session, root_task_id) if root_task_id else Policy()
    budget = BudgetPolicy.from_policy(policy)
    estimate = visible_estimate(inv.snapshot) if inv is not None else None
    view: dict[str, Any] = {
        "active": budget.active, **budget.to_dict(),
        "estimate_visible": estimate,
        # Instructions, tool definitions and thread history the provider adds.
        "provider_context": "unknown",
        "policy": {k: v for k, v in policy.as_dict().items() if k != "version"},
    }
    if root_task_id is None or (inv is not None and not budget.active):
        return view  # a card without a budget needs no tree arithmetic
    spend = spend_preview(session, root_task_id)
    view.update({"known": spend.known, "unsettled": spend.unsettled, "reserved": spend.reserved,
                 "unbounded": spend.unbounded, "unreserved_in_flight": spend.unreserved_in_flight,
                 "remaining": budget.total - spend.charged if budget.total is not None else None})
    if inv is not None:
        item = session.get(TokenBudgetItemRow, inv.id)
        view["this_call"] = item_view(item)
        if item is None and inv.status in IN_FLIGHT_STATUSES:
            # What ensure_counters will record for it (spend_preview counts it already).
            view["this_call"] = {"estimate_visible": 0, "reservation": 0, "reserved": 0, "known": 0,
                                 "unsettled": 0, "unbounded": 1, "overrun": False,
                                 "state": UNRESERVED, "mode": None, "warnings": []}
        if item is None and inv.status in ("awaiting_approval", "queued") and budget.active:
            view["preview"] = decide(budget, spend, estimate=estimate or 0, metered=True).to_dict()
            view["assumes_usage_reporting"] = True
    return view
