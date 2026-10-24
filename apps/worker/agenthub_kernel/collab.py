"""Collaboration plans: save, item state, dispatch rules (P4, ``agenthub.collab``).

An item's state is derived from the calls of its sub-task, never stored. Every
call on the sub-task counts - the plan's own dispatches and a retry created by
the manual adjudication of an ``unknown`` call alike - and the latest decides:

* ``pending``: not dispatched yet;
* ``awaiting`` / ``running``: its latest call is pending approval or executing;
* ``unconfirmed``: its latest call is ``unknown`` - it may still be running,
  so it is neither a failure nor retryable, and keeps its quota slot. It
  stays so until recovery or the user's adjudication confirms how it ended;
* ``done``: succeeded, with a reply that can be handed on;
* ``no_result``: succeeded but without a reply in this conversation;
* ``failed``: confirmed to have ended otherwise (failed, canceled, timed out,
  rejected, invalid).

Dispatching is refused unless the collaboration switch is on, every item it
depends on is ``done``, the item is ``pending``, ``failed`` or ``no_result``,
and it has attempts left. The kernel then creates the depth-1 sub-task call
like any other turn (``conversations.submit_turn``); approval and limits apply
there.
"""

from __future__ import annotations

from typing import Any, Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from agenthub.collab import MAX_ATTEMPTS_PER_ITEM, PlanItem
from agenthub.contracts import Policy
from agenthub_kernel.db import (
    ChatMessageRow, CollabAttemptRow, CollabItemRow, CollabPlanRow, InvocationRow, TaskRow,
)
from agenthub_kernel.service import QuotaError
from agenthub_kernel.summaries import root_task_of

ACTIVE = {"draft": "awaiting", "awaiting_approval": "awaiting", "queued": "awaiting",
          "starting": "running", "running": "running"}


def collaboration_enabled(session: Session, cid: str) -> bool:
    root_id = root_task_of(session, cid)
    root = session.get(TaskRow, root_id) if root_id else None
    if root is None or not root.policy:
        return False
    return Policy(**{k: v for k, v in root.policy.items() if k != "version"}).collaboration_enabled


def save_plan(session: Session, cid: str, items: list[PlanItem], *, source: str = "manual") -> CollabPlanRow:
    if not collaboration_enabled(session, cid):
        raise QuotaError("collaboration_disabled", "协作扩展未开启：先在策略中开启，才能保存和分派协作计划")
    plan = CollabPlanRow(conversation_id=cid, source=source)
    session.add(plan)
    session.flush()
    for item in items:
        session.add(CollabItemRow(plan_id=plan.id, idx=item.index, agent_id=item.agent_id, goal=item.goal,
                                  depends_on=list(item.depends_on)))
    session.flush()
    return plan


def _items(session: Session, plan_id: int) -> list[CollabItemRow]:
    return session.scalars(select(CollabItemRow).where(CollabItemRow.plan_id == plan_id)
                           .order_by(CollabItemRow.idx)).all()


#: Retryable end states (confirmed over, nothing to hand on).
RETRYABLE = ("pending", "failed", "no_result")


def _latest(session: Session, item: CollabItemRow) -> tuple[int, Optional[InvocationRow]]:
    """(calls on the item's sub-task, the latest of them)."""
    if item.task_id is None:
        return 0, None
    calls = session.scalars(select(InvocationRow).where(InvocationRow.task_id == item.task_id)
                            .order_by(InvocationRow.created_at, InvocationRow.id)).all()
    return len(calls), (calls[-1] if calls else None)


def _reply(session: Session, invocation_id: str) -> Optional[ChatMessageRow]:
    return session.scalar(select(ChatMessageRow).where(ChatMessageRow.invocation_id == invocation_id,
                                                       ChatMessageRow.role == "assistant"))


def item_state(session: Session, item: CollabItemRow) -> dict[str, Any]:
    attempts, inv = _latest(session, item)
    reply = _reply(session, inv.id) if inv is not None else None
    if inv is None:
        state = "pending"
    elif inv.status in ACTIVE:
        state = ACTIVE[inv.status]
    elif inv.status == "unknown":
        state = "unconfirmed"  # may still be running: not a failure, not retryable
    elif inv.status == "succeeded":
        state = "done" if reply is not None else "no_result"
    else:
        state = "failed"
    return {"state": state, "attempts": attempts, "invocation_id": inv.id if inv else None,
            "reply_message_id": reply.id if reply else None}


def plan_view(session: Session, plan: CollabPlanRow) -> dict[str, Any]:
    items = _items(session, plan.id)
    states = {item.idx: item_state(session, item) for item in items}
    out = []
    for item in items:
        s = states[item.idx]
        deps_done = all(states[d]["state"] == "done" for d in item.depends_on or [])
        out.append({"item_id": item.id, "index": item.idx, "agent_id": item.agent_id, "goal": item.goal,
                    "depends_on": list(item.depends_on or []), **s,
                    "ready": s["state"] in RETRYABLE and deps_done
                    and s["attempts"] < MAX_ATTEMPTS_PER_ITEM})
    return {"plan_id": plan.id, "conversation_id": plan.conversation_id, "source": plan.source,
            "items": out, "all_done": bool(out) and all(i["state"] == "done" for i in out)}


def plans_of(session: Session, cid: str) -> list[CollabPlanRow]:
    return session.scalars(select(CollabPlanRow).where(CollabPlanRow.conversation_id == cid)
                           .order_by(CollabPlanRow.id.desc())).all()


def plan_in(session: Session, cid: str, plan_id: int) -> CollabPlanRow:
    plan = session.get(CollabPlanRow, plan_id)
    if plan is None or plan.conversation_id != cid:
        raise QuotaError("plan_not_found", "plan not found in this conversation")
    return plan


def item_in(session: Session, cid: str, plan_id: int, index: int) -> CollabItemRow:
    plan_in(session, cid, plan_id)
    item = session.scalar(select(CollabItemRow).where(CollabItemRow.plan_id == plan_id, CollabItemRow.idx == index))
    if item is None:
        raise QuotaError("plan_not_found", "plan item not found")
    return item


def check_dispatch(session: Session, cid: str, item: CollabItemRow) -> None:
    """Raise unless this item may be dispatched now (see module docstring)."""
    if not collaboration_enabled(session, cid):
        raise QuotaError("collaboration_disabled", "协作扩展未开启")
    states = {i.idx: item_state(session, i) for i in _items(session, item.plan_id)}
    mine = states[item.idx]
    if mine["state"] in ("awaiting", "running"):
        raise QuotaError("item_in_progress", "this sub-task already has a call in progress")
    if mine["state"] == "unconfirmed":
        raise QuotaError("item_unconfirmed",
                         "this sub-task's last call is unknown and may still be running: confirm how it "
                         "ended (recovery or adjudication) before dispatching it again")
    if mine["state"] == "done":
        raise QuotaError("item_done", "this sub-task is already done")
    if mine["attempts"] >= MAX_ATTEMPTS_PER_ITEM:
        raise QuotaError("retry_limit", f"this sub-task was dispatched {mine['attempts']} times (the limit)")
    waiting = [d for d in item.depends_on or [] if states[d]["state"] != "done"]
    if waiting:
        raise QuotaError("dependency_pending", f"sub-task waits for item(s) {waiting}")


def dependency_replies(session: Session, item: CollabItemRow) -> list[tuple[ChatMessageRow, str]]:
    """The replies of the items it depends on (done, checked by check_dispatch)."""
    if not item.depends_on:
        return []
    deps = session.scalars(select(CollabItemRow).where(CollabItemRow.plan_id == item.plan_id,
                                                       CollabItemRow.idx.in_(item.depends_on))).all()
    out = [(session.get(ChatMessageRow, item_state(session, dep)["reply_message_id"]), dep.agent_id)
           for dep in deps]
    return sorted(out, key=lambda pair: pair[0].id)


def collected_replies(session: Session, cid: str, plan_id: int) -> list[tuple[ChatMessageRow, str]]:
    plan = plan_in(session, cid, plan_id)
    view = plan_view(session, plan)
    if not view["all_done"]:
        raise QuotaError("plan_not_finished", "every sub-task must be done before collecting")
    return [(session.get(ChatMessageRow, i["reply_message_id"]), i["agent_id"]) for i in view["items"]]


def record_attempt(session: Session, item: CollabItemRow, invocation_id: str) -> int:
    """Number this dispatch by the calls on the item's sub-task (adjudication retries included)."""
    attempt = session.scalar(select(func.count()).select_from(InvocationRow)
                             .where(InvocationRow.task_id == item.task_id)) or 1
    session.add(CollabAttemptRow(invocation_id=invocation_id, item_id=item.id, attempt=attempt))
    session.flush()
    return attempt


def attempt_of(session: Session, invocation_id: str) -> Optional[dict[str, Any]]:
    """For the call's card: which plan item and attempt it is, if any."""
    row = session.get(CollabAttemptRow, invocation_id)
    if row is None:
        return None
    item = session.get(CollabItemRow, row.item_id)
    return {"plan_id": item.plan_id, "index": item.idx, "attempt": row.attempt}
