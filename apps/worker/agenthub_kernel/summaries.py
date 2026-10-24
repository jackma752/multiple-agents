"""Stage summaries, history for drafting, and "compress and continue" (T-2, minimal).

* :func:`save_summary` stores a new immutable version; :func:`usable_summary`
  says which summaries a call in a conversation may attach: its own, or those
  of the conversation it was compressed from. Anything else is "not found".
* :func:`summary_segments` / :func:`history_segments` frame a summary, or the
  conversation's recent messages, into context-preview segments, so the
  approval binds exactly that text (``agenthub.context``).
* :func:`compress` opens a new conversation linked to its source and to the
  summary it continues from. The source conversation is not changed. Nothing
  here calls a model: a draft summary comes from an ordinary approved call.
"""

from __future__ import annotations

from typing import Any, Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from agenthub.context import HISTORY_CONTENT, HISTORY_FRAME, SEPARATOR, SUMMARY_CONTENT, SUMMARY_FRAME
from agenthub.summary import MAX_HISTORY_CHARS, SECTIONS, StageSummary, summary_frames
from agenthub_kernel.db import (
    ChatMessageRow, ConversationLinkRow, ConversationRow, ConversationSummaryRow, InvocationRow,
)
from agenthub_kernel.service import QuotaError, new_id


def summary_of(row: ConversationSummaryRow) -> StageSummary:
    return StageSummary(**{name: getattr(row, name) or "" for name, _ in SECTIONS},
                        file_refs=tuple(row.file_refs or ()))


def summary_view(row: ConversationSummaryRow) -> dict[str, Any]:
    summary = summary_of(row)
    return {"summary_id": row.id, "conversation_id": row.conversation_id, "version": row.version,
            **summary.to_dict(), "source": row.source, "chars": len(summary.body()),
            "created_at": row.created_at.isoformat() + "Z" if row.created_at else None}


def save_summary(session: Session, cid: str, summary: StageSummary, *, source: str = "manual") -> ConversationSummaryRow:
    """A new version (the caller holds the writer lock)."""
    latest = session.scalar(select(func.max(ConversationSummaryRow.version))
                            .where(ConversationSummaryRow.conversation_id == cid)) or 0
    row = ConversationSummaryRow(conversation_id=cid, version=latest + 1, source=source,
                                 **{k: v for k, v in summary.to_dict().items()})
    session.add(row)
    session.flush()
    return row


def link_of(session: Session, cid: str) -> Optional[ConversationLinkRow]:
    return session.get(ConversationLinkRow, cid)


def link_view(link: Optional[ConversationLinkRow]) -> dict[str, Any]:
    if link is None:
        return {"parent_conversation_id": None, "continued_from_summary_id": None}
    return {"parent_conversation_id": link.parent_conversation_id, "continued_from_summary_id": link.summary_id}


def available_summaries(session: Session, cid: str) -> list[ConversationSummaryRow]:
    """This conversation's versions and its source conversation's, newest first."""
    owners = [cid]
    link = link_of(session, cid)
    if link is not None:
        owners.append(link.parent_conversation_id)
    return session.scalars(select(ConversationSummaryRow)
                           .where(ConversationSummaryRow.conversation_id.in_(owners))
                           .order_by(ConversationSummaryRow.id.desc())).all()


def usable_summary(session: Session, cid: str, summary_id: int) -> ConversationSummaryRow:
    row = session.get(ConversationSummaryRow, summary_id)
    link = link_of(session, cid)
    owners = {cid} | ({link.parent_conversation_id} if link is not None else set())
    if row is None or row.conversation_id not in owners:
        raise QuotaError("summary_not_found", "summary must belong to this conversation or the one it continues")
    return row


def summary_segments(row: ConversationSummaryRow) -> list[tuple]:
    where = {"summary_id": row.id, "summary_version": row.version}
    head, tail = summary_frames(row.version, row.id)
    return [(SUMMARY_FRAME, head, where), (SUMMARY_CONTENT, summary_of(row).body(), where),
            (SUMMARY_FRAME, tail, where)]


def history_segments(session: Session, cid: str) -> list[tuple]:
    """The conversation's most recent messages that fit, oldest first, whole messages only."""
    rows = session.execute(
        select(ChatMessageRow.id, ChatMessageRow.role, ChatMessageRow.text, InvocationRow.snapshot)
        .outerjoin(InvocationRow, InvocationRow.id == ChatMessageRow.invocation_id)
        .where(ChatMessageRow.conversation_id == cid, ChatMessageRow.role.in_(("user", "assistant")))
        .order_by(ChatMessageRow.id.desc())
    ).all()
    chosen, total = [], 0
    for row in rows:
        if total + len(row.text) > MAX_HISTORY_CHARS:
            break
        chosen.append(row)
        total += len(row.text)
    chosen.reverse()
    segments = [(HISTORY_FRAME, f"--- Conversation history: the last {len(chosen)} of {len(rows)} messages ---")]
    for row in chosen:
        who = row.role if row.role == "user" else f"assistant {(row.snapshot or {}).get('agent_id', '')}".strip()
        where = {"message_id": row.id, "role": row.role}
        segments += [(SEPARATOR, "\n\n"), (HISTORY_FRAME, f"--- {who} (Hub message {row.id}) ---\n", where),
                     (HISTORY_CONTENT, row.text, where)]
    segments.append((HISTORY_FRAME, "\n--- End of history ---"))
    return segments


def compress(session: Session, cid: str, user_id: str, summary_id: int, title: str = "") -> ConversationRow:
    """Open a new conversation that continues ``cid`` from ``summary_id``."""
    source = session.get(ConversationRow, cid)
    summary = usable_summary(session, cid, summary_id)
    row = ConversationRow(id=new_id("conv"), user_id=user_id,
                          title=(title or f"{source.title or cid}（续，摘要 v{summary.version}）")[:200])
    session.add(row)
    session.flush()
    session.add(ConversationLinkRow(conversation_id=row.id, parent_conversation_id=cid, summary_id=summary.id))
    session.flush()
    return row
