"""Archive: put a conversation or research task out of the way, and bring it back.

Only display state. Archiving refuses while something of the item could still
start or is running (an active automatic stage, a queued or running call), so a
hidden item never keeps working unseen; a paused stage may be archived, since it
can only continue after the user resumes it. Everything is kept and the
archive can be undone.
"""

from __future__ import annotations

from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from agenthub_kernel.db import ArchivedItemRow, InvocationRow, StageRunRow, TaskRow
from agenthub_kernel.service import QuotaError

KINDS = ("conversation", "research")
#: States in which an item keeps working on its own (paused is not one of them).
RUNNING_STAGE = ("active", "repairing", "waiting_quota")
RUNNING_CALL = ("queued", "starting", "running")


def busy(session: Session, root_task_id: Optional[str]) -> Optional[str]:
    """Why the tree cannot be archived now, or None."""
    if not root_task_id:
        return None
    if session.scalar(select(StageRunRow.id).where(StageRunRow.root_task_id == root_task_id,
                                                   StageRunRow.status.in_(RUNNING_STAGE)).limit(1)):
        return "自动执行仍在进行：先暂停或停止，再归档"
    if session.scalar(select(InvocationRow.id).join(TaskRow, TaskRow.id == InvocationRow.task_id).where(
            TaskRow.root_task_id == root_task_id, InvocationRow.status.in_(RUNNING_CALL)).limit(1)):
        return "还有正在排队或执行的调用：等它结束后再归档"
    return None


def archive(session: Session, user_id: str, kind: str, item_id: str) -> bool:
    if kind not in KINDS:
        raise ValueError("unknown archive kind")
    if session.get(ArchivedItemRow, (kind, item_id)) is not None:
        return False
    session.add(ArchivedItemRow(kind=kind, item_id=item_id, user_id=user_id))
    session.flush()
    return True


def unarchive(session: Session, kind: str, item_id: str) -> bool:
    row = session.get(ArchivedItemRow, (kind, item_id))
    if row is None:
        return False
    session.delete(row)
    session.flush()
    return True


def archived_ids(session: Session, user_id: str, kind: str) -> set:
    return set(session.scalars(select(ArchivedItemRow.item_id).where(
        ArchivedItemRow.user_id == user_id, ArchivedItemRow.kind == kind)))


def require_idle(session: Session, root_task_id: Optional[str]) -> None:
    reason = busy(session, root_task_id)
    if reason:
        raise QuotaError("item_busy", reason)
