"""Per-call token usage ledger (T-1a; mapping rules in ``agenthub.usage``).

Every call that reached its adapter gets ONE ``usage_ledger`` row, written in
the transaction that settles it - success, failure, interrupt and unknown
alike (D-T0-3): spending is not tied to having a reply. The row is keyed by
the invocation, so SSE replays, restart backfills and repeated settlements
can only rewrite it, never add a second one. A later report may raise a row's
completeness (unknown -> partial -> complete) but never lower it.

Totals for a conversation, a root task (sub-tasks included), an agent, a day or
the whole instance are queries over the rows (T-1b); nothing is booked twice.
They add only known numbers and say how many calls are partial or unknown:
an unknown is never counted as zero.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from agenthub.usage import (
    COMPLETE, PARTIAL, UNKNOWN, UsageRecord, completeness_rank, normalize_usage,
    not_launched_usage, unknown_usage,
)
from agenthub_kernel.adapters import adapter_for
from agenthub_kernel.budget import budget_view, settle_budget
from agenthub_kernel.db import ChatMessageRow, ConversationRow, InvocationRow, TaskRow, UsageLedgerRow

#: Invocation statuses that mean the call was handed to its adapter and ended
#: (or became unknown). Calls in any other status have spent nothing yet.
SETTLED_STATUSES = frozenset({"succeeded", "failed", "canceled", "timed_out", "unknown"})
NUMBER_FIELDS = ("input_total", "input_uncached", "cache_read", "cache_write",
                 "output_total", "reasoning")


def adapter_usage(adapter: Any, inv: InvocationRow, *, fallback_note: str = "no_adapter_record",
                  refused_before_launch: bool = False) -> UsageRecord:
    """What this call's own adapter reports, normalized. Never raises.

    ``refused_before_launch``: the adapter just returned a definitive refusal
    in this process. An adapter with ``usage_report`` records every call before
    launching, so having no record then proves nothing was launched (zero).
    After a restart no such inference is made.
    """
    target = adapter_for(adapter, (inv.snapshot or {}).get("agent_id"))
    report = getattr(target, "usage_report", None) if target is not None else None
    if not callable(report):
        return unknown_usage("adapter_reports_no_usage")
    try:
        result = report(inv.id)
    except Exception:  # noqa: BLE001 - not logged: adapters may quote content
        return unknown_usage("usage_malformed")
    if result is None:
        return not_launched_usage() if refused_before_launch else unknown_usage(fallback_note)
    return normalize_usage(result)


def _conversation_of(session: Session, invocation_id: str) -> Optional[str]:
    return session.scalar(select(ChatMessageRow.conversation_id).where(
        ChatMessageRow.invocation_id == invocation_id, ChatMessageRow.role == "user").limit(1))


def _same(row: UsageLedgerRow, rec: UsageRecord) -> bool:
    return (row.mapping == rec.mapping and row.mapping_version == rec.mapping_version
            and row.completeness == rec.completeness and row.source == rec.source
            and all(getattr(row, f) == getattr(rec, f) for f in NUMBER_FIELDS)
            and list(row.notes or []) == list(rec.notes) and (row.raw or {}) == rec.raw
            and row.model == rec.model)


def record_usage(session: Session, adapter: Any, invocation_id: str, *,
                 fallback_note: str = "no_adapter_record", refused_before_launch: bool = False,
                 record: Optional[UsageRecord] = None) -> Optional[UsageLedgerRow]:
    """Write (or improve) this call's ledger row inside the caller's transaction."""
    inv = session.get(InvocationRow, invocation_id)
    if inv is None:
        return None
    rec = record or adapter_usage(adapter, inv, fallback_note=fallback_note,
                                  refused_before_launch=refused_before_launch)
    row = session.get(UsageLedgerRow, invocation_id)
    if row is None:
        task = session.get(TaskRow, inv.task_id)
        row = UsageLedgerRow(
            invocation_id=invocation_id, task_id=inv.task_id,
            root_task_id=task.root_task_id if task is not None else inv.task_id,
            agent_id=str((inv.snapshot or {}).get("agent_id") or "unknown"),
            conversation_id=_conversation_of(session, invocation_id),
        )
        session.add(row)
    elif completeness_rank(rec.completeness) < completeness_rank(row.completeness) or _same(row, rec):
        settle_budget(session, invocation_id)  # idempotent: the counters follow the row
        return row  # what is known is never replaced by less
    row.mapping, row.mapping_version = rec.mapping, rec.mapping_version
    row.completeness, row.source = rec.completeness, rec.source
    for name in NUMBER_FIELDS:
        setattr(row, name, getattr(rec, name))
    row.notes, row.raw, row.model = list(rec.notes), dict(rec.raw), rec.model
    session.flush()
    # Same transaction: the call's budget reservation is settled against this row (T-1c).
    settle_budget(session, invocation_id)
    return row


#: The Hub has no project entity yet (AGENT_HUB_PLAN §5 ``projects`` is not
#: implemented), so the widest view is one user's conversations in this Hub
#: instance - each instance runs with its own data directory.
INSTANCE_SCOPE_NOTE = "未区分项目：方案 §5 的 projects 尚未实现，本视图为当前用户在本 Hub 实例中的全部会话"
DAY_BASIS = "UTC，按结算记录时间"


def _iso(value: Any) -> Optional[str]:
    return value.isoformat() + "Z" if value is not None else None


def ledger_view(row: UsageLedgerRow) -> dict[str, Any]:
    """The public shape: numbers and machine codes; ``raw`` stays internal."""
    view = {"invocation_id": row.invocation_id, "agent_id": row.agent_id,
            "mapping": row.mapping, "mapping_version": row.mapping_version,
            "completeness": row.completeness, "source": row.source,
            "notes": list(row.notes or []), "model": row.model,
            "recorded_at": _iso(row.created_at)}
    view.update({name: getattr(row, name) for name in NUMBER_FIELDS})
    return view


#: Shown for a settled call that has no row (settled before T-1a, or by a path
#: that never reached a usage report): unknown, not zero.
def not_recorded(invocation_id: str, agent_id: str) -> dict[str, Any]:
    view = {"invocation_id": invocation_id, "agent_id": agent_id, "mapping": "none",
            "mapping_version": None, "completeness": UNKNOWN, "source": "none",
            "notes": ["not_recorded"], "model": None, "recorded_at": None}
    view.update({name: None for name in NUMBER_FIELDS})
    return view


def _view_of(inv: InvocationRow, row: Optional[UsageLedgerRow]) -> Optional[dict[str, Any]]:
    if row is not None:
        return ledger_view(row)
    if inv.status in SETTLED_STATUSES:
        return not_recorded(inv.id, str((inv.snapshot or {}).get("agent_id") or "unknown"))
    return None


def usage_view(session: Session, inv: InvocationRow) -> Optional[dict[str, Any]]:
    """This call's usage, ``not_recorded`` when settled without a row, else None."""
    return _view_of(inv, session.get(UsageLedgerRow, inv.id))


def summarize(views: list[dict[str, Any]]) -> dict[str, Any]:
    """Known sums plus how many calls are partial / unknown (never guessed as 0)."""
    known = {name: 0 for name in NUMBER_FIELDS}
    missing = {name: 0 for name in NUMBER_FIELDS}
    counts = {COMPLETE: 0, PARTIAL: 0, UNKNOWN: 0}
    for view in views:
        counts[view["completeness"] if view["completeness"] in counts else UNKNOWN] += 1
        for name in NUMBER_FIELDS:
            if isinstance(view.get(name), int):
                known[name] += view[name]
            else:
                missing[name] += 1
    return {"calls": len(views), "complete_calls": counts[COMPLETE],
            "partial_calls": counts[PARTIAL], "unknown_calls": counts[UNKNOWN],
            "known": known, "calls_missing": missing}


def by_agent(views: list[dict[str, Any]]) -> dict[str, Any]:
    agents: dict[str, list] = {}
    for view in views:
        agents.setdefault(view["agent_id"], []).append(view)
    return {agent: summarize(group) for agent, group in sorted(agents.items())}


MAPPING_NOTE = "input_total 已含缓存读写；未知的调用不计为 0"


def conversation_usage(session: Session, conversation_id: str) -> dict[str, Any]:
    """This conversation's chat calls, plus its whole root task (sub-tasks included)."""
    invs = session.scalars(
        select(InvocationRow).join(ChatMessageRow, ChatMessageRow.invocation_id == InvocationRow.id)
        .where(ChatMessageRow.conversation_id == conversation_id, ChatMessageRow.role == "user")
        .order_by(ChatMessageRow.id)
    ).all()
    calls = [v for v in (usage_view(session, inv) for inv in invs) if v is not None]
    conversation = session.get(ConversationRow, conversation_id)
    root = conversation.root_task_id if conversation is not None else None
    return {"conversation_id": conversation_id, "mapping_note": MAPPING_NOTE,
            "calls": calls, "total": summarize(calls), "by_agent": by_agent(calls),
            "root_task": root_task_usage(session, root) if root else None,
            # The tree's token budget and spend (T-1c); its policy for editing.
            "budget": budget_view(session, root)}


def root_task_usage(session: Session, root_task_id: str) -> dict[str, Any]:
    """Every call of one root task tree - chat calls and sub-tasks alike.

    A parent's figure is this query over its tree; nothing is booked twice.
    Task goals are user text and are not returned.
    """
    tasks = {t.id: t for t in session.scalars(select(TaskRow).where(TaskRow.root_task_id == root_task_id))}
    invs = session.scalars(
        select(InvocationRow).where(InvocationRow.task_id.in_(list(tasks)))
        .order_by(InvocationRow.created_at, InvocationRow.id)
    ).all() if tasks else []
    rows = {r.invocation_id: r for r in session.scalars(select(UsageLedgerRow).where(
        UsageLedgerRow.invocation_id.in_([i.id for i in invs])))} if invs else {}
    views, per_task = [], {}
    for inv in invs:
        view = _view_of(inv, rows.get(inv.id))
        if view is not None:
            views.append(view)
            per_task.setdefault(inv.task_id, []).append(view)
    ordered = sorted(per_task, key=lambda tid: (tasks[tid].depth, tid))
    return {"root_task_id": root_task_id, "total": summarize(views), "by_agent": by_agent(views),
            "by_task": [{"task_id": tid, "parent_task_id": tasks[tid].parent_task_id,
                         "depth": tasks[tid].depth, "agent_id": tasks[tid].agent_id,
                         "kind": tasks[tid].kind, "total": summarize(per_task[tid])}
                        for tid in ordered]}


def instance_usage(session: Session, user_id: str, *, since: Optional[date] = None,
                   until: Optional[date] = None) -> dict[str, Any]:
    """One user's calls across all their conversations in this Hub instance.

    The project-level view until a project entity exists (see
    :data:`INSTANCE_SCOPE_NOTE`). Only root tasks of the user's own
    conversations are read; calls of other users or of tasks created outside
    a conversation (the legacy console) are not included. A call's day is its
    settlement record's UTC date (a settled call without a record: its finish
    or creation time).
    """
    conversations = session.scalars(select(ConversationRow).where(
        ConversationRow.user_id == user_id, ConversationRow.root_task_id.is_not(None))).all()
    by_root = {c.root_task_id: c for c in conversations}
    pairs = session.execute(
        select(InvocationRow, TaskRow.root_task_id).join(TaskRow, TaskRow.id == InvocationRow.task_id)
        .where(TaskRow.root_task_id.in_(list(by_root)))
        .order_by(InvocationRow.created_at, InvocationRow.id)
    ).all() if by_root else []
    rows = {r.invocation_id: r for r in session.scalars(select(UsageLedgerRow).where(
        UsageLedgerRow.root_task_id.in_(list(by_root))))} if by_root else {}
    views, days, convs = [], {}, {}
    for inv, root in pairs:
        row = rows.get(inv.id)
        view = _view_of(inv, row)
        if view is None:
            continue
        when = row.created_at if row is not None else (inv.finished_at or inv.created_at)
        day = when.date() if when is not None else None
        if day is None or (since and day < since) or (until and day > until):
            continue
        views.append(view)
        days.setdefault(day.isoformat(), []).append(view)
        convs.setdefault(by_root[root].id, []).append(view)
    by_id = {c.id: c for c in conversations}
    order = sorted(convs, key=lambda cid: (by_id[cid].created_at, cid), reverse=True)
    return {
        "scope": "instance", "scope_note": INSTANCE_SCOPE_NOTE, "mapping_note": MAPPING_NOTE,
        "day_basis": DAY_BASIS,
        "since": since.isoformat() if since else None, "until": until.isoformat() if until else None,
        "total": summarize(views), "by_agent": by_agent(views),
        "by_day": [{"day": day, **summarize(group)} for day, group in sorted(days.items())],
        "by_conversation": [{"conversation_id": cid, "title": by_id[cid].title,
                             "total": summarize(convs[cid])} for cid in order],
    }


def restore_usage(db: Any, adapter: Any) -> int:
    """Backfill rows for settled calls that have none (e.g. after a crash)."""
    with db.session() as session:
        ids = session.scalars(
            select(InvocationRow.id)
            .outerjoin(UsageLedgerRow, UsageLedgerRow.invocation_id == InvocationRow.id)
            .where(InvocationRow.status.in_(sorted(SETTLED_STATUSES)),
                   UsageLedgerRow.invocation_id.is_(None))
        ).all()
    restored = 0
    for invocation_id in ids:
        with db.session() as session:
            restored += int(record_usage(session, adapter, invocation_id) is not None)
    return restored
