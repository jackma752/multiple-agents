"""Durable local chat history. No provider calls or implicit approvals here."""

from typing import Any, Optional

from sqlalchemy import exists, select
from sqlalchemy.orm import Session, aliased

from agenthub.chat import CONTEXT_MODE, ConversationTurn
from agenthub.contracts import NATIVE_RESUME, InvocationSnapshot

#: Statuses in which a provider thread may still be executing a turn.
THREAD_BUSY_STATUSES = ("starting", "running", "unknown")
from agenthub_kernel.db import (
    ApprovalRow, ChatMessageRow, ConversationRow, InvocationRow, TaskRow,
)
from agenthub_kernel.service import PolicyService, QuotaError


def owned_conversation(session: Session, cid: str, user_id: str) -> ConversationRow:
    row = session.get(ConversationRow, cid)
    if row is None or row.user_id != user_id:
        raise QuotaError("no_conversation", "conversation not found")
    return row


def conversation_view(row: ConversationRow) -> dict[str, Any]:
    return {
        "conversation_id": row.id, "title": row.title,
        "root_task_id": row.root_task_id, "context_mode": CONTEXT_MODE,
        "created_at": row.created_at.isoformat() + "Z",
    }


def turn_view(session: Session, message: ChatMessageRow) -> dict[str, Any]:
    inv = session.get(InvocationRow, message.invocation_id)
    approval = session.scalar(
        select(ApprovalRow).where(ApprovalRow.invocation_id == inv.id)
        .order_by(ApprovalRow.created_at.desc(), ApprovalRow.id.desc()).limit(1)
    )
    return {
        "conversation_id": message.conversation_id, "message_id": message.id,
        "task_id": inv.task_id, "invocation_id": inv.id,
        "approval_id": approval.id if approval else None,
        "approval_status": approval.status if approval else None,
        "payload_hash": inv.payload_hash, "status": inv.status,
        "execution_mode": inv.snapshot.get("execution_mode", "strict"),
        "agent_id": inv.snapshot["agent_id"],
        "context_mode": inv.snapshot.get("context_mode", CONTEXT_MODE),
        "resume_thread_id": inv.snapshot.get("resume_thread_id"),
    }


def native_thread_for(session: Session, cid: str, agent_id: str) -> Optional[str]:
    """Provider thread recorded by this agent's latest reply in the conversation.

    Only the newest reply counts: resuming an older thread would silently skip
    the exchange after it. ``None`` when that reply carries no native session.
    """
    reply = aliased(ChatMessageRow)
    rows = session.execute(
        select(reply.result_metadata, InvocationRow.snapshot)
        .join(InvocationRow, InvocationRow.id == reply.invocation_id)
        .where(reply.conversation_id == cid, reply.role == "assistant")
        .order_by(reply.id.desc())
    ).all()
    for metadata, snapshot in rows:
        if (snapshot or {}).get("agent_id") != agent_id:
            continue
        native = (metadata or {}).get("native_session")
        if isinstance(native, dict) and isinstance(native.get("thread_id"), str) and native["thread_id"]:
            return native["thread_id"]
        return None
    return None


def native_resume_blocker(session: Session, inv: InvocationRow) -> Optional[tuple[str, str]]:
    """Why a native_resume call must not start now, or None.

    Another call of the same tree that is starting, running or unknown may
    still be using the provider thread; a second turn on top of it is refused
    rather than queued blindly. Checked before any approval is consumed.
    """
    if inv.snapshot.get("context_mode") != NATIVE_RESUME:
        return None
    root = session.get(TaskRow, inv.task_id)
    busy = session.scalar(
        select(InvocationRow.id)
        .join(TaskRow, TaskRow.id == InvocationRow.task_id)
        .where(
            TaskRow.root_task_id == root.root_task_id,
            InvocationRow.id != inv.id,
            InvocationRow.status.in_(THREAD_BUSY_STATUSES),
        ).limit(1)
    )
    if busy is not None:
        return ("native_session_busy",
                "another call in this conversation is active or unknown; "
                "the provider thread may still be in use")
    return None


def submit_turn(
    session: Session, cid: str, user_id: str, request: ConversationTurn,
) -> dict[str, Any]:
    """Caller holds SQLite's writer lock; message/draft/approval commit together."""
    conversation = owned_conversation(session, cid, user_id)
    previous = session.scalar(select(ChatMessageRow).where(
        ChatMessageRow.conversation_id == cid,
        ChatMessageRow.client_request_id == request.client_request_id,
    ))
    if previous is not None:
        if previous.request_hash != request.request_hash():
            raise QuotaError("idempotency_conflict", "request key already used with different content")
        return {**turn_view(session, previous), "replayed": True}

    svc = PolicyService(session)
    if conversation.root_task_id is None:
        root = svc.create_task(agent_id=request.agent_id, goal=request.text)
        conversation.root_task_id = root.id
    else:
        root = session.get(TaskRow, conversation.root_task_id)
        if svc._tree_stopped(root.id):
            raise QuotaError("tree_stopped", "conversation task tree is stopped")

    resume_thread_id = None
    if request.context_mode == NATIVE_RESUME:
        resume_thread_id = native_thread_for(session, cid, request.agent_id)
        if resume_thread_id is None:
            raise QuotaError("no_native_session",
                             "no provider thread recorded by this agent's latest reply")
        busy = session.scalar(select(InvocationRow.id).where(
            InvocationRow.task_id == root.id,
            InvocationRow.status.in_(THREAD_BUSY_STATUSES),
        ).limit(1))
        if busy is not None:
            raise QuotaError("native_session_busy",
                             "another call in this conversation is active or unknown")

    snapshot = InvocationSnapshot(
        agent_id=request.agent_id, task_input=request.text, parent_task_id=None,
        depth=0, policy_version=root.policy_version, execution_mode=request.execution_mode,
        context_mode=request.context_mode, resume_thread_id=resume_thread_id,
    )
    inv = svc.create_invocation(task_id=root.id, snapshot=snapshot)
    svc.create_approval(invocation_id=inv.id, user_id=user_id)
    message = ChatMessageRow(
        conversation_id=cid, role="user", text=request.text, invocation_id=inv.id,
        client_request_id=request.client_request_id, request_hash=request.request_hash(),
    )
    session.add(message)
    session.flush()
    return {**turn_view(session, message), "replayed": False}


def capture_reply(session: Session, adapter: Any, invocation_id: str) -> bool:
    """Save only actual adapter text for a confirmed terminal call, once.

    Adapter-store outages cannot undo quota settlement. Recovery retries this
    projection from stored results, never by starting another model call.
    The caller serializes this write with settlement/stop using BEGIN IMMEDIATE.
    """
    inv = session.get(InvocationRow, invocation_id)
    if inv is None or inv.status not in {"succeeded", "failed", "canceled"}:
        return False
    source = session.scalar(select(ChatMessageRow).where(
        ChatMessageRow.invocation_id == invocation_id, ChatMessageRow.role == "user",
    ))
    if source is None or session.scalar(select(ChatMessageRow.id).where(
        ChatMessageRow.invocation_id == invocation_id, ChatMessageRow.role == "assistant",
    )) is not None:
        return False
    last = getattr(adapter, "last_result", None)
    if not callable(last):
        return False  # An inert Mock's state is not a model reply.
    try:
        result = last(invocation_id)
    except Exception:
        return False  # Do not log adapter exceptions: they can quote user content.
    if not isinstance(result, dict):
        return False
    reply = result.get("final_message")
    if not isinstance(reply, str) or not reply.strip():
        return False
    thread_id = result.get("thread_id")
    if thread_id and thread_id != inv.adapter_invocation_id:
        return False  # A result from another execution is not this call's reply.
    usage = result.get("usage")
    metadata = {"thread_id": thread_id, "usage": usage if isinstance(usage, dict) else None}
    native = result.get("native_session")
    if (isinstance(native, dict) and native.get("thread_id") == thread_id and thread_id
            and isinstance(native.get("transport"), str)):
        # Only an adapter that can actually resume reports this; it is what a
        # later native_resume turn binds into its snapshot.
        metadata["native_session"] = {"transport": native["transport"], "thread_id": thread_id}
    session.add(ChatMessageRow(
        conversation_id=source.conversation_id, role="assistant", text=reply,
        invocation_id=invocation_id, result_metadata=metadata,
    ))
    session.flush()
    return True


def restore_replies(db: Any, adapter: Any) -> int:
    """Backfill terminal replies after restart; no changes to execution status."""
    reply = aliased(ChatMessageRow)
    with db.session() as session:
        ids = session.scalars(
            select(ChatMessageRow.invocation_id)
            .join(InvocationRow, InvocationRow.id == ChatMessageRow.invocation_id)
            .where(
                ChatMessageRow.role == "user",
                InvocationRow.status.in_(("succeeded", "failed", "canceled")),
                ~exists(select(reply.id).where(
                    reply.invocation_id == ChatMessageRow.invocation_id,
                    reply.role == "assistant",
                )),
            ).order_by(ChatMessageRow.id)
        ).all()
    from sqlalchemy import text
    restored = 0
    for inv_id in ids:
        with db.session() as session:
            session.execute(text("BEGIN IMMEDIATE"))
            restored += int(capture_reply(session, adapter, inv_id))
    return restored
