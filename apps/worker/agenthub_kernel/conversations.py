"""Durable local chat history. No provider calls or implicit approvals here."""

import hashlib
import re
from typing import Any, Optional

from sqlalchemy import exists, select
from sqlalchemy.orm import Session, aliased

from agenthub.chat import CONTEXT_MODE, MAX_HANDOFF_CHARS, MAX_MESSAGE_CHARS, ConversationTurn
from agenthub.context import (
    FILE_SNIPPET, HANDOFF_CONTENT, HANDOFF_FRAME, OUTPUT_HINT, SEPARATOR, USER_INPUT, compose,
)
from agenthub.contracts import NATIVE_RESUME, InvocationSnapshot
from agenthub_kernel.model_usage import provider_of
from agenthub.collab import MAX_COLLECT_CHARS
from agenthub.output import OUTPUT_HINTS

#: Statuses in which a provider thread may still be executing a turn.
THREAD_BUSY_STATUSES = ("starting", "running", "unknown")
from agenthub_kernel.adapters import adapter_for
from agenthub_kernel.autopilot import grant_view
from agenthub_kernel.db import (
    ApprovalRow, ChatMessageRow, CollabItemRow, ConversationRow, InvocationRow, TaskRow,
)
from agenthub_kernel.service import PolicyService, QuotaError
from agenthub_kernel.budget import budget_view
from agenthub_kernel.file_refs import FileRefError, file_blocks
from agenthub_kernel.collab import (
    attempt_of, check_dispatch, collected_replies, dependency_replies, plan_in, record_attempt,
)
from agenthub_kernel.summaries import (
    history_segments, link_view, summary_segments, tree_owner, usable_summary,
)
from agenthub_kernel.usage_ledger import usage_view


#: Request-shaped openings that say nothing about the topic (stripped from titles).
_TITLE_OPENINGS = ("请你", "请帮我", "帮我", "帮忙", "麻烦你", "麻烦", "请", "根据以下", "根据下面", "根据",
                   "我想要", "我想", "我要", "能不能", "可以")
_CJK = re.compile(r"[㐀-鿿豈-﫿]")


def short_title(text: str, limit: int = 5) -> str:
    """A local title for a new conversation (P4.5): no model call.

    The first clause, without request-shaped openings and punctuation: at
    most ``limit`` characters when it starts with Chinese, else at most 3
    words. Empty when nothing is left (the page shows "未命名").
    """
    for piece in (re.split(r"[\n。！？!?；;：:，,、]", text.strip(), maxsplit=1)[0], text):
        s = piece.strip()
        changed = True
        while changed:
            changed = False
            for opening in _TITLE_OPENINGS:
                if s.startswith(opening) and len(s) > len(opening):
                    s, changed = s[len(opening):].lstrip(), True
        first = next((ch for ch in s if ch.isalnum()), "")
        if not first:
            continue
        if _CJK.match(first):
            return "".join(ch for ch in s if ch.isalnum())[:limit]
        return " ".join(re.findall(r"[^\W_][\w'-]*", s)[:3])[:40]
    return ""


def owned_conversation(session: Session, cid: str, user_id: str) -> ConversationRow:
    row = session.get(ConversationRow, cid)
    if row is None or row.user_id != user_id:
        raise QuotaError("no_conversation", "conversation not found")
    return row


def conversation_view(row: ConversationRow, link: Any = None) -> dict[str, Any]:
    """``link``: its ``ConversationLinkRow`` when it continues another conversation (T-2)."""
    return {
        "conversation_id": row.id, "title": row.title,
        "root_task_id": row.root_task_id, "context_mode": CONTEXT_MODE,
        "created_at": row.created_at.isoformat() + "Z",
        **link_view(link),
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
        "error": inv.error,
        "execution_mode": inv.snapshot.get("execution_mode", "strict"),
        "workspace": (inv.snapshot.get("permissions") or {}).get("workspace"),
        "workspace_access": (inv.snapshot.get("permissions") or {}).get("workspace_access"),
        "agent_id": inv.snapshot["agent_id"],
        "model": inv.snapshot.get("model"), "reasoning_effort": inv.snapshot.get("reasoning_effort"),
        "model_provider": inv.snapshot.get("model_provider"),
        "context_mode": inv.snapshot.get("context_mode", CONTEXT_MODE),
        "resume_thread_id": inv.snapshot.get("resume_thread_id"),
        "handoff": inv.snapshot.get("handoff") or [],
        # With a handoff or files, what is sent differs from the user's own text: show it all.
        "sent_text": inv.snapshot["task_input"] if _adds_context(inv.snapshot) else None,
        # What this call sends, part by part, as approved (T-1d); None before T-1d.
        "context_preview": inv.snapshot.get("context_preview"),
        # P4: the plan item and attempt this sub-task call belongs to, if any.
        "plan_item": attempt_of(session, inv.id), "depth": inv.snapshot.get("depth", 0),
        # Normalized ledger row (T-1a); None until the call is settled.
        "usage": usage_view(session, inv),
        # Token budget of its tree as the approval card shows it (T-1c).
        "budget": budget_view(session, _root_of_task(session, inv.task_id), inv=inv),
        # P4.6: the stage rule that approved it, when the user did not click.
        "authorized_by": grant_view(session, approval.id if approval else None),
    }


def _adds_context(snapshot: dict) -> bool:
    """Does the call send more than the user's own text (handoff, files, summary, history)?"""
    parts = (snapshot.get("context_preview") or {}).get("parts") or []
    return bool(snapshot.get("handoff")) or any(p.get("kind") != USER_INPUT for p in parts)


def _root_of_task(session: Session, task_id: str) -> Optional[str]:
    task = session.get(TaskRow, task_id)
    return task.root_task_id if task is not None else None


def handoff_segments(source: ChatMessageRow, agent_id: str) -> list[tuple]:
    """How one handed-over reply is framed in the next call's input (fixed format),
    as context-preview segments: the Hub's frame lines around the reply's text."""
    where = {"message_id": source.id, "agent_id": agent_id}
    return [(HANDOFF_FRAME, f"--- Handoff: reply of {agent_id} (Hub message {source.id}) ---\n", where),
            (HANDOFF_CONTENT, source.text, where),
            (HANDOFF_FRAME, "\n--- End of handoff ---", where)]


def handoff_block(source: ChatMessageRow, agent_id: str) -> str:
    return "".join(segment[1] for segment in handoff_segments(source, agent_id))


def build_handoff(session: Session, cid: str, message_ids: tuple) -> tuple[list[list[tuple]], tuple]:
    """Segments and source records for the chosen replies, or QuotaError.

    Only assistant replies of this same conversation can be handed on - a
    message id from another conversation is "not found", never read.
    """
    blocks, records, total = [], [], 0
    for message_id in message_ids:
        source = session.get(ChatMessageRow, message_id)
        if source is None or source.conversation_id != cid or source.role != "assistant":
            raise QuotaError("handoff_not_found", "handoff source must be a reply in this conversation")
        inv = session.get(InvocationRow, source.invocation_id)
        source_agent = (inv.snapshot or {}).get("agent_id", "unknown") if inv is not None else "unknown"
        total += len(source.text)
        if total > MAX_HANDOFF_CHARS:
            raise QuotaError("handoff_too_large",
                             f"handed-over text exceeds {MAX_HANDOFF_CHARS} characters")
        blocks.append(handoff_segments(source, source_agent))
        records.append(_handoff_record(source, source_agent))
    return blocks, tuple(records)


def _handoff_record(source: ChatMessageRow, agent_id: str) -> dict[str, Any]:
    return {"message_id": source.id, "agent_id": agent_id, "chars": len(source.text),
            "sha256": hashlib.sha256(source.text.encode("utf-8")).hexdigest()}


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


def recent_file_refs(session: Session, cid: str, limit: int = 3) -> list[str]:
    """This conversation's latest file references as specs, newest first, distinct."""
    rows = session.scalars(
        select(InvocationRow.snapshot).join(ChatMessageRow, ChatMessageRow.invocation_id == InvocationRow.id)
        .where(ChatMessageRow.conversation_id == cid, ChatMessageRow.role == "user")
        .order_by(ChatMessageRow.id.desc()).limit(20)
    ).all()
    specs: list[str] = []
    for snapshot in rows:
        for part in ((snapshot or {}).get("context_preview") or {}).get("parts") or []:
            if part.get("kind") == FILE_SNIPPET:
                spec = f"{part['path']}:L{part['start_line']}-{part['end_line']}"
                if spec not in specs:
                    specs.append(spec)
        if len(specs) >= limit:
            break
    return specs[:limit]


def compose_turn(session: Session, cid: str, request: Any, *, context_profile: Optional[dict] = None,
                 files_root: Optional[str] = None) -> tuple[str, dict, tuple]:
    """The text a turn sends, its context preview and its handoff records; writes nothing.

    The user's text, then the chosen replies (handoff) and the referenced files,
    each in a fixed frame. The composed text is what the agent receives and what
    the approval binds; the user's message keeps only what the user typed. The
    preview is built from the same segments, so its parts are an exact split of
    that text (T-1d). File contents are read now, once: the approved text is
    what runs, whatever happens to the files afterwards (T-1f).

    T-2: ``include_history`` adds this conversation's recent messages (for
    drafting a summary) and ``summary_id`` a stage summary, whose file
    references are read with the call's own.

    T-3, context first: the blocks go out in a fixed order - stage summary,
    file snippets, output hint, handoffs, history - and the user's message
    LAST. What repeats from call to call (summary, files) forms a stable
    prefix a provider's prompt cache can reuse; nothing time- or
    random-dependent is put in front of it. Whether a provider actually hits
    its cache is only observed afterwards (``usage.cache_read``).
    """
    blocks, handoff = [], ()
    refs = list(getattr(request, "file_refs", ()) or ())
    summary_id = getattr(request, "summary_id", None)
    if summary_id is not None:
        if cid is None:
            raise QuotaError("summary_not_found", "a summary needs its conversation")
        summary = usable_summary(session, cid, summary_id)
        blocks.append(summary_segments(summary))
        refs = list(dict.fromkeys([*(summary.file_refs or []), *refs]))
    if refs:
        try:
            blocks += file_blocks(files_root, refs)
        except FileRefError as exc:
            raise QuotaError(exc.code, exc.message) from None
        except ValueError as exc:
            raise QuotaError("invalid_file_ref", str(exc)) from None
    style = getattr(request, "output_style", "") or ""
    if style:
        blocks.append([(OUTPUT_HINT, OUTPUT_HINTS[style])])
    if request.handoff_message_ids:
        handoff_blocks, handoff = build_handoff(session, cid, request.handoff_message_ids)
        blocks += handoff_blocks
    # P4: a sub-task gets the results it depends on; collecting gets every result.
    plan_sources = []
    if getattr(request, "plan_item_id", None):
        plan_sources = dependency_replies(session, session.get(CollabItemRow, request.plan_item_id))
    elif getattr(request, "collect_plan_id", None):
        plan_sources = collected_replies(session, cid, request.collect_plan_id)
    if plan_sources:
        if sum(len(source.text) for source, _ in plan_sources) > MAX_COLLECT_CHARS:
            raise QuotaError("handoff_too_large", f"sub-task results exceed {MAX_COLLECT_CHARS} characters")
        blocks += [handoff_segments(source, agent) for source, agent in plan_sources]
        handoff = tuple(handoff) + tuple(_handoff_record(source, agent) for source, agent in plan_sources)
    if getattr(request, "include_history", False) and cid is not None:
        blocks.append(history_segments(session, cid))
    if request.text:
        blocks.append([(USER_INPUT, request.text)])
    segments = []
    for i, block in enumerate(blocks):
        segments += ([(SEPARATOR, "\n\n")] if i else []) + list(block)
    task_input, preview = compose(segments, context_mode=request.context_mode, profile=context_profile)
    if len(task_input) > MAX_MESSAGE_CHARS:
        raise QuotaError("handoff_too_large" if not (refs or summary_id or request.include_history)
                         else "context_too_large",
                         "message with its handoff, summary, history and files exceeds the size limit")
    return task_input, preview, handoff


def submit_turn(
    session: Session, cid: str, user_id: str, request: ConversationTurn,
    *, adapter_version: str = "unknown", context_profile: Optional[dict] = None,
    files_root: Optional[str] = None, stage_dispatch_limit: Optional[int] = None,
    stage_resume_thread_id: Optional[str] = None,
) -> dict[str, Any]:
    """Caller holds SQLite's writer lock; message/draft/approval commit together.

    ``adapter_version`` is the serving adapter's binding identity; it enters
    the snapshot, so the approval binds it (D11). ``context_profile`` is what
    the serving adapter declares for the context preview (T-1d); the preview
    enters the snapshot too. ``files_root`` bounds which files ``file_refs``
    may read.
    """
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
    # A continued conversation runs in its source's task tree (T-2): same
    # policy, budget counters, reservations and ledger; only the conversation
    # and the provider threads are new.
    owner = tree_owner(session, cid)
    if owner.root_task_id is None:
        root = svc.create_task(agent_id=request.agent_id, goal=request.text)
        owner.root_task_id = root.id
    else:
        root = session.get(TaskRow, owner.root_task_id)
        if svc._tree_stopped(root.id):
            raise QuotaError("tree_stopped", "conversation task tree is stopped")

    resume_thread_id = None
    if request.context_mode == NATIVE_RESUME:
        resume_thread_id = stage_resume_thread_id or native_thread_for(session, cid, request.agent_id)
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

    # P4: a plan item runs as its own depth-1 sub-task (created on its first
    # dispatch, which checks the collaboration switch; retries reuse it).
    task, item = root, None
    if request.plan_item_id is not None:
        item = session.get(CollabItemRow, request.plan_item_id)
        plan = plan_in(session, cid, item.plan_id) if item is not None else None
        if plan is None:
            raise QuotaError("plan_not_found", "plan item not found")
        check_dispatch(session, cid, item, **({"attempt_limit": stage_dispatch_limit}
                                             if stage_dispatch_limit is not None else {}))
        if item.task_id is None:
            child = svc.create_task(agent_id=item.agent_id, goal=item.goal, parent_task_id=root.id,
                                    kind="auto" if plan.source.startswith("reply:") else "user")
            item.task_id = child.id
        task = session.get(TaskRow, item.task_id)

    task_input, preview, handoff = compose_turn(session, cid, request, context_profile=context_profile,
                                                files_root=files_root)
    snapshot = InvocationSnapshot(
        agent_id=request.agent_id, task_input=task_input, parent_task_id=task.parent_task_id,
        depth=task.depth, policy_version=root.policy_version, execution_mode=request.execution_mode,
        context_mode=request.context_mode, resume_thread_id=resume_thread_id,
        adapter_version=adapter_version, handoff=handoff, context_preview=preview,
        model=request.model, reasoning_effort=request.reasoning_effort,
        model_provider=provider_of(request.agent_id) if request.model else None,
    )
    inv = svc.create_invocation(task_id=task.id, snapshot=snapshot)
    svc.create_approval(invocation_id=inv.id, user_id=user_id)
    if item is not None:
        record_attempt(session, item, inv.id)
    message = ChatMessageRow(
        conversation_id=cid, role="user", text=request.text, invocation_id=inv.id,
        client_request_id=request.client_request_id, request_hash=request.request_hash(),
    )
    session.add(message)
    if not (conversation.title or "").strip():
        conversation.title = short_title(request.text)  # an untitled conversation is named by its first message
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
    # Behind a router, the reply lives in the store of the call's own agent.
    adapter = adapter_for(adapter, inv.snapshot.get("agent_id"))
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
