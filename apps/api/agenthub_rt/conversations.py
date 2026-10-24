"""Authenticated persistent chat routes, separate from the legacy console."""

import asyncio
import json
import time
from datetime import date
from types import SimpleNamespace
from typing import Literal, Optional

from fastapi import Depends, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select, text
from starlette.concurrency import run_in_threadpool

from agenthub.chat import MAX_MESSAGE_CHARS, ConversationTurn
from agenthub.files import MAX_REFS
from agenthub_kernel.adapters import adapter_binding, adapter_context_profile
from agenthub_kernel.budget import estimate_check
from agenthub_kernel.conversations import (
    compose_turn, conversation_view, owned_conversation, recent_file_refs, submit_turn, turn_view,
)
from agenthub_kernel.file_refs import FileRefError, list_files
from agenthub_kernel.file_suggest import suggest
from agenthub_kernel.summaries import (
    available_summaries, compress, link_of, link_view, root_task_of, save_summary, summary_view,
)
from agenthub.summary import SUMMARY_INSTRUCTION, StageSummary, parse_summary_text
from agenthub.collab import (
    COLLECT_INSTRUCTION, MAX_PLAN_ITEMS, PLAN_INSTRUCTION, parse_plan_text, validate_items,
)
from agenthub_kernel.collab import (
    collaboration_enabled, item_in, plan_view, plans_of, save_plan,
)
from agenthub_kernel.events import CursorAhead, attach_stream_text, read_events
from agenthub_kernel.usage_ledger import conversation_usage, instance_usage

SSE_RETRY_MS = 3000
SSE_HEARTBEAT_SECONDS = 15.0
SSE_BATCH = 200


def sse_frame(event: dict) -> str:
    data = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
    return f"id: {event['seq']}\nevent: {event['type']}\ndata: {data}\n\n"
from agenthub_kernel.db import ChatMessageRow, ConversationLinkRow, ConversationRow, InvocationRow
from agenthub_kernel.service import QuotaError, new_id
from agenthub_rt.auth import Principal, authenticate, require_approver


class ConversationIn(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    title: str = Field(default="", max_length=200)


class RenameIn(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    title: str = Field(min_length=1, max_length=200)


class MessageIn(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    text: str = Field(min_length=1, max_length=MAX_MESSAGE_CHARS)

    @field_validator("text")
    @classmethod
    def nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("text cannot be blank")
        return value


class TurnIn(MessageIn):
    agent_id: str = Field(min_length=1, max_length=200)
    client_request_id: str = Field(min_length=1, max_length=128)
    execution_mode: Literal["strict", "supervised"] = "strict"
    # native_resume continues the provider thread of this agent's latest reply;
    # the thread id is bound into the approved snapshot.
    context_mode: Literal["independent", "native_resume"] = "independent"
    # Manual handoff: replies of this conversation the user chose to pass on.
    handoff_message_ids: list[int] = Field(default_factory=list, max_length=3)
    # Files or line ranges the Hub reads and sends (agenthub.files syntax).
    file_refs: list[str] = Field(default_factory=list, max_length=MAX_REFS)
    # A stage summary sent instead of history; recent history (summary drafting).
    summary_id: Optional[int] = None
    include_history: bool = False
    # Output preference added as a visible, approved hint (agenthub.output).
    output_style: Literal["", "concise", "detailed"] = ""

    @field_validator("agent_id", "client_request_id")
    @classmethod
    def nonblank_identifier(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("identifier cannot be blank")
        return value


class PreviewIn(BaseModel):
    """What the composer would send: nothing is created, nothing is approved."""
    model_config = ConfigDict(extra="forbid", strict=True)
    conversation_id: Optional[str] = None  # needed only to preview a handoff
    text: str = Field(default="", max_length=MAX_MESSAGE_CHARS)
    agent_id: str = Field(min_length=1, max_length=200)
    context_mode: Literal["independent", "native_resume"] = "independent"
    handoff_message_ids: list[int] = Field(default_factory=list, max_length=3)
    file_refs: list[str] = Field(default_factory=list, max_length=MAX_REFS)
    summary_id: Optional[int] = None
    include_history: bool = False
    output_style: Literal["", "concise", "detailed"] = ""


class SummaryIn(BaseModel):
    """A new version of a conversation's stage summary (agenthub.summary)."""
    model_config = ConfigDict(extra="forbid", strict=True)
    goal: str
    constraints: str = ""
    done: str = ""
    open_issues: str = ""
    next_steps: str = ""
    file_refs: list[str] = Field(default_factory=list, max_length=MAX_REFS)
    # The drafting call's reply it was edited from, if any (recorded as its source).
    from_message_id: Optional[int] = None


class PlanItemIn(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    agent_id: str = Field(min_length=1, max_length=200)
    goal: str = Field(min_length=1)
    depends_on: list[int] = Field(default_factory=list)


class PlanIn(BaseModel):
    """A collaboration plan (agenthub.collab); saving starts nothing."""
    model_config = ConfigDict(extra="forbid", strict=True)
    items: list[PlanItemIn]
    # The main agent's reply it was edited from, if any (its items become "auto" tasks).
    from_message_id: Optional[int] = None


class DispatchIn(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    client_request_id: str = Field(min_length=1, max_length=128)
    execution_mode: Literal["strict", "supervised"] = "strict"


class CollectIn(DispatchIn):
    agent_id: str = Field(min_length=1, max_length=200)
    text: str = Field(default="", max_length=MAX_MESSAGE_CHARS)


class CompressIn(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    summary_id: int
    title: str = Field(default="", max_length=200)


class SuggestIn(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    conversation_id: Optional[str] = None  # for its recent references
    text: str = Field(default="", max_length=MAX_MESSAGE_CHARS)
    file_refs: list[str] = Field(default_factory=list, max_length=MAX_REFS)


def _turn_of(data: dict) -> ConversationTurn:
    try:
        return ConversationTurn(**{**data, "handoff_message_ids": tuple(data["handoff_message_ids"]),
                                   "file_refs": tuple(data["file_refs"])})
    except ValueError as exc:
        raise HTTPException(422, detail={"code": "invalid_turn", "message": str(exc)})


def _http_error(exc: QuotaError) -> HTTPException:
    status = 404 if exc.code in ("no_conversation", "handoff_not_found", "summary_not_found") else (
        422 if exc.code in FILE_REF_INPUT_ERRORS else 409)
    return HTTPException(status, detail={"code": exc.code, "message": exc.message})


#: File-reference refusals caused by what was asked for (not by server state).
FILE_REF_INPUT_ERRORS = frozenset({
    "invalid_file_ref", "file_outside_root", "file_excluded", "file_not_found", "file_too_large",
    "file_not_text", "range_out_of_bounds", "snippet_too_large", "files_too_large", "context_too_large",
})


def register_conversations(app, db) -> None:
    def require_conversation(session, cid, principal):
        require_approver(principal)
        try:
            return owned_conversation(session, cid, principal.identity)
        except QuotaError:
            raise HTTPException(404, detail="conversation not found")

    @app.post("/api/conversations")
    def create_conversation(
        body: ConversationIn | None = None, principal: Principal = Depends(authenticate),
    ):
        require_approver(principal)
        with db.session() as session:
            row = ConversationRow(
                id=new_id("conv"), user_id=principal.identity, title=body.title if body else "",
            )
            session.add(row)
            session.flush()
            return conversation_view(row)

    @app.patch("/api/conversations/{cid}")
    def rename_conversation(cid: str, body: RenameIn, principal: Principal = Depends(authenticate)):
        """A user-chosen title; it is never replaced by the first-message title."""
        title = body.title.strip()
        if not title:
            raise HTTPException(422, detail={"code": "invalid_title", "message": "title is empty"})
        with db.session() as session:
            row = require_conversation(session, cid, principal)
            row.title = title
            session.flush()
            link = session.get(ConversationLinkRow, cid)
            return conversation_view(row, link)

    @app.get("/api/conversations")
    def list_conversations(
        offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=100),
        principal: Principal = Depends(authenticate),
    ):
        require_approver(principal)
        with db.session() as session:
            rows = session.scalars(select(ConversationRow).where(
                ConversationRow.user_id == principal.identity,
            ).order_by(ConversationRow.created_at.desc(), ConversationRow.id.desc())
                .offset(offset).limit(limit + 1)).all()
            links = {link.conversation_id: link for link in session.scalars(select(ConversationLinkRow).where(
                ConversationLinkRow.conversation_id.in_([row.id for row in rows])))}
            return {
                "conversations": [conversation_view(row, links.get(row.id)) for row in rows[:limit]],
                "has_more": len(rows) > limit, "next_offset": offset + min(len(rows), limit),
            }

    @app.post("/api/conversations/{cid}/messages")
    def save_message(cid: str, body: MessageIn, principal: Principal = Depends(authenticate)):
        with db.session() as session:
            require_conversation(session, cid, principal)
            row = ChatMessageRow(conversation_id=cid, role="user", text=body.text)
            session.add(row)
            session.flush()
            return {"conversation_id": cid, "message_id": row.id, "acked": True, "text": row.text}

    @app.get("/api/conversations/{cid}/messages")
    def list_messages(
        cid: str, after_id: int = Query(0, ge=0), limit: int = Query(100, ge=1, le=200),
        principal: Principal = Depends(authenticate),
    ):
        with db.session() as session:
            conversation = require_conversation(session, cid, principal)
            rows = session.scalars(select(ChatMessageRow).where(
                ChatMessageRow.conversation_id == cid, ChatMessageRow.id > after_id,
            ).order_by(ChatMessageRow.id).limit(limit + 1)).all()
            messages = []
            for row in rows[:limit]:
                messages.append({
                    "message_id": row.id, "role": row.role, "text": row.text,
                    "created_at": row.created_at.isoformat() + "Z",
                    "invocation": turn_view(session, row) if row.invocation_id else None,
                    "result": row.result_metadata,
                })
            return {
                **conversation_view(conversation, link_of(session, cid)),
                "root_task_id": root_task_of(session, cid),  # a continuation's is its source's tree
                "messages": messages,
                "has_more": len(rows) > limit,
                "next_after_id": messages[-1]["message_id"] if messages else after_id,
            }

    @app.get("/api/conversations/{cid}/usage")
    def get_conversation_usage(cid: str, principal: Principal = Depends(authenticate)):
        """Ledger rows of this conversation's calls, per agent and in total.

        Totals add known numbers only and count partial / unknown calls; an
        unknown call is never shown as zero (T-1a).
        """
        with db.session() as session:
            require_conversation(session, cid, principal)
            return conversation_usage(session, cid)

    @app.get("/api/usage/summary")
    def get_usage_summary(
        since: Optional[date] = Query(None), until: Optional[date] = Query(None),
        principal: Principal = Depends(authenticate),
    ):
        """This user's calls across all their conversations here, by agent / day / conversation.

        The project-level view until projects exist (T-1b): only the caller's
        own conversations are read. ``since`` / ``until`` are inclusive UTC days.
        """
        require_approver(principal)
        if since and until and since > until:
            raise HTTPException(422, detail="since must not be after until")
        with db.session() as session:
            return instance_usage(session, principal.identity, since=since, until=until)

    def read_page(session, cid, after: int, limit: int):
        """Stored events (IDs/statuses only) plus server-attached chunk text.

        Callers must have checked ownership of ``cid`` already.
        """
        page = read_events(session, cid, after, limit)
        page["events"] = attach_stream_text(session, cid, page["events"])
        return page

    def cursor_ahead(exc: CursorAhead):
        return HTTPException(409, detail={
            "code": "cursor_ahead", "last_seq": exc.last_seq,
            "message": "cursor is beyond this conversation's stored events",
        })

    @app.get("/api/conversations/{cid}/events")
    def list_conversation_events(
        cid: str, after_seq: int = Query(0, ge=0), limit: int = Query(100, ge=1, le=500),
        principal: Principal = Depends(authenticate),
    ):
        with db.session() as session:
            require_conversation(session, cid, principal)
            try:
                return {"conversation_id": cid, **read_page(session, cid, after_seq, limit)}
            except CursorAhead as exc:
                raise cursor_ahead(exc)

    @app.get("/api/conversations/{cid}/events/stream")
    async def stream_conversation_events(
        cid: str, request: Request,
        after_seq: int = Query(0, ge=0),
        timeout: float = Query(30.0, ge=0, le=300),
        last_event_id: Optional[str] = Header(None),
        principal: Principal = Depends(authenticate),
    ):
        """SSE replay-then-follow. Reconnect with ``Last-Event-ID`` to resume.

        The stream closes after ``timeout`` seconds; clients reconnect from the
        last ``id`` they received. Polling the database (not an in-process
        signal) keeps events written by a separate Worker process visible.
        """
        cursor = after_seq
        if last_event_id is not None:
            if not last_event_id.isdigit():
                raise HTTPException(400, detail="Last-Event-ID must be a non-negative integer")
            cursor = int(last_event_id)  # The browser's resume point wins.

        def first_page():
            with db.session() as session:
                require_conversation(session, cid, principal)
                return read_page(session, cid, cursor, SSE_BATCH)

        try:
            page = await run_in_threadpool(first_page)
        except CursorAhead as exc:
            raise cursor_ahead(exc)

        def next_page(after: int):
            # Ownership was checked by first_page for this same principal/cid.
            with db.session() as session:
                return read_page(session, cid, after, SSE_BATCH)

        poll = float(getattr(app.state, "sse_poll_seconds", 0.5))

        async def frames():
            nonlocal page
            deadline = time.monotonic() + timeout
            beat = time.monotonic() + SSE_HEARTBEAT_SECONDS
            after = cursor
            yield f"retry: {SSE_RETRY_MS}\n\n"
            while True:
                for event in page["events"]:
                    yield sse_frame(event)
                after = page["next_after_seq"]
                if page["has_more"]:
                    page = await run_in_threadpool(next_page, after)
                    continue
                now = time.monotonic()
                if now >= deadline or await request.is_disconnected():
                    return
                if now >= beat:
                    beat = now + SSE_HEARTBEAT_SECONDS
                    yield ": keep-alive\n\n"
                await asyncio.sleep(min(poll, max(deadline - now, 0)))
                try:
                    page = await run_in_threadpool(next_page, after)
                except CursorAhead:
                    return  # Stream was replaced underneath us; client re-syncs.

        return StreamingResponse(frames(), media_type="text/event-stream", headers={
            "Cache-Control": "no-cache", "X-Accel-Buffering": "no",
        })

    @app.post("/api/conversations/{cid}/invocations")
    def create_turn(cid: str, body: TurnIn, principal: Principal = Depends(authenticate)):
        require_approver(principal)
        request = _turn_of(body.model_dump())
        with db.session() as session:
            session.execute(text("BEGIN IMMEDIATE"))
            try:
                # The identity of the adapter serving THIS agent (behind a router,
                # each agent has its own); "unavailable" for an unserved agent.
                # Its context profile feeds the bound pre-send preview (T-1d).
                adapter = app.state.worker.adapter
                return submit_turn(session, cid, principal.identity, request,
                                   adapter_version=adapter_binding(adapter, request.agent_id),
                                   context_profile=adapter_context_profile(adapter, request.agent_id),
                                   files_root=app.state.files_root)
            except QuotaError as exc:
                raise _http_error(exc)

    @app.post("/api/context-preview")
    def preview_turn(body: PreviewIn, principal: Principal = Depends(authenticate)):
        """The text a message would send and its context preview; creates nothing.

        The submitted call composes again from the files as they are then; the
        approval card shows (and binds) that result.
        """
        require_approver(principal)
        request = SimpleNamespace(text=body.text, context_mode=body.context_mode,
                                  handoff_message_ids=tuple(body.handoff_message_ids),
                                  file_refs=tuple(body.file_refs), summary_id=body.summary_id,
                                  include_history=body.include_history, output_style=body.output_style)
        with db.session() as session:
            conversation = None
            if body.conversation_id is not None:
                conversation = require_conversation(session, body.conversation_id, principal)
            elif request.handoff_message_ids:
                raise HTTPException(422, detail={"code": "invalid_turn",
                                                  "message": "a handoff needs its conversation_id"})
            try:
                sent, preview, _handoff = compose_turn(
                    session, body.conversation_id, request, files_root=app.state.files_root,
                    context_profile=adapter_context_profile(app.state.worker.adapter, body.agent_id))
            except QuotaError as exc:
                raise _http_error(exc)
            # Over the budget: the page asks to narrow the references (never trims).
            budget = estimate_check(session, root_task_of(session, conversation.id) if conversation else None,
                                    preview["estimate"])
        return {"sent_text": sent, "context_preview": preview, "budget": budget}

    # ---------------------------------------------------- stage summaries (T-2)

    @app.get("/api/conversations/{cid}/summaries")
    def list_summaries(cid: str, principal: Principal = Depends(authenticate)):
        """Summaries a message here may attach: this conversation's versions and those
        of the conversation it continues, newest first."""
        with db.session() as session:
            require_conversation(session, cid, principal)
            return {"summaries": [summary_view(r) for r in available_summaries(session, cid)],
                    **link_view(link_of(session, cid)), "instruction": SUMMARY_INSTRUCTION}

    @app.post("/api/conversations/{cid}/summaries")
    def create_summary(cid: str, body: SummaryIn, principal: Principal = Depends(authenticate)):
        """Save a new version. Nothing is sent or approved by saving."""
        data = body.model_dump()
        source = data.pop("from_message_id")
        try:
            summary = StageSummary(**data)
        except ValueError as exc:
            raise HTTPException(422, detail={"code": "invalid_summary", "message": str(exc)})
        with db.session() as session:
            session.execute(text("BEGIN IMMEDIATE"))
            require_conversation(session, cid, principal)
            if source is not None:
                draft = session.get(ChatMessageRow, source)
                if draft is None or draft.conversation_id != cid or draft.role != "assistant":
                    raise HTTPException(404, detail={"code": "draft_not_found",
                                                     "message": "draft must be a reply in this conversation"})
            row = save_summary(session, cid, summary, source=f"draft:{source}" if source else "manual")
            return summary_view(row)

    @app.get("/api/conversations/{cid}/summary-draft")
    def summary_draft(cid: str, message_id: int = Query(..., ge=1), principal: Principal = Depends(authenticate)):
        """A reply of this conversation read back into summary fields, for editing (not saved)."""
        with db.session() as session:
            require_conversation(session, cid, principal)
            reply = session.get(ChatMessageRow, message_id)
            if reply is None or reply.conversation_id != cid or reply.role != "assistant":
                raise HTTPException(404, detail={"code": "draft_not_found",
                                                 "message": "draft must be a reply in this conversation"})
            return {"from_message_id": reply.id, **parse_summary_text(reply.text)}

    @app.post("/api/conversations/{cid}/compress")
    def compress_conversation(cid: str, body: CompressIn, principal: Principal = Depends(authenticate)):
        """Continue in a new conversation from a summary; this one stays as it is."""
        with db.session() as session:
            session.execute(text("BEGIN IMMEDIATE"))
            require_conversation(session, cid, principal)
            try:
                row = compress(session, cid, principal.identity, body.summary_id, body.title)
            except QuotaError as exc:
                raise _http_error(exc)
            return conversation_view(row, link_of(session, row.id))

    # ------------------------------------------------- collaboration plans (P4)

    def agent_ids() -> tuple:
        """The agents this Hub serves (names only; no identity checks)."""
        adapter = app.state.worker.adapter
        agents = getattr(adapter, "agents", None)
        if callable(getattr(adapter, "for_agent", None)) and callable(agents):
            return tuple(sorted(agents()))
        return tuple(sorted(getattr(adapter, "AGENT_IDS", ()) or ()))

    def submit_hub_turn(session, cid, principal, request):
        adapter = app.state.worker.adapter
        try:
            return submit_turn(session, cid, principal.identity, request,
                               adapter_version=adapter_binding(adapter, request.agent_id),
                               context_profile=adapter_context_profile(adapter, request.agent_id),
                               files_root=app.state.files_root)
        except QuotaError as exc:
            raise HTTPException(404 if exc.code in ("plan_not_found",) else 409,
                                detail={"code": exc.code, "message": exc.message})

    @app.get("/api/conversations/{cid}/plans")
    def list_plans(cid: str, principal: Principal = Depends(authenticate)):
        with db.session() as session:
            require_conversation(session, cid, principal)
            return {"plans": [plan_view(session, p) for p in plans_of(session, cid)],
                    "collaboration_enabled": collaboration_enabled(session, cid),
                    "instruction": PLAN_INSTRUCTION.format(max_items=MAX_PLAN_ITEMS,
                                                           agents=", ".join(agent_ids()) or "any"),
                    "collect_instruction": COLLECT_INSTRUCTION}

    @app.post("/api/conversations/{cid}/plans")
    def create_plan(cid: str, body: PlanIn, principal: Principal = Depends(authenticate)):
        """Save a plan (needs the collaboration switch). Nothing is dispatched."""
        try:
            items = validate_items([i.model_dump() for i in body.items])
        except ValueError as exc:
            raise HTTPException(422, detail={"code": "invalid_plan", "message": str(exc)})
        known = agent_ids()
        unknown = [i.agent_id for i in items if known and i.agent_id not in known]
        if unknown:
            raise HTTPException(422, detail={"code": "invalid_plan", "message": f"unknown agent(s): {unknown}"})
        with db.session() as session:
            session.execute(text("BEGIN IMMEDIATE"))
            require_conversation(session, cid, principal)
            source = "manual"
            if body.from_message_id is not None:
                reply = session.get(ChatMessageRow, body.from_message_id)
                if reply is None or reply.conversation_id != cid or reply.role != "assistant":
                    raise HTTPException(404, detail={"code": "draft_not_found",
                                                     "message": "draft must be a reply in this conversation"})
                source = f"reply:{reply.id}"
            try:
                plan = save_plan(session, cid, items, source=source)
            except QuotaError as exc:
                raise HTTPException(409, detail={"code": exc.code, "message": exc.message})
            return plan_view(session, plan)

    @app.get("/api/conversations/{cid}/plan-draft")
    def plan_draft(cid: str, message_id: int = Query(..., ge=1), principal: Principal = Depends(authenticate)):
        """A reply read back into plan items, for editing (not saved)."""
        with db.session() as session:
            require_conversation(session, cid, principal)
            reply = session.get(ChatMessageRow, message_id)
            if reply is None or reply.conversation_id != cid or reply.role != "assistant":
                raise HTTPException(404, detail={"code": "draft_not_found",
                                                 "message": "draft must be a reply in this conversation"})
            return {"from_message_id": reply.id, "items": parse_plan_text(reply.text, agent_ids())}

    @app.post("/api/conversations/{cid}/plans/{plan_id}/items/{index}/dispatch")
    def dispatch_item(cid: str, plan_id: int, index: int, body: DispatchIn,
                      principal: Principal = Depends(authenticate)):
        """Create the item's sub-task call (pending approval); never runs it."""
        with db.session() as session:
            session.execute(text("BEGIN IMMEDIATE"))
            require_conversation(session, cid, principal)
            try:
                item = item_in(session, cid, plan_id, index)
            except QuotaError as exc:
                raise HTTPException(404, detail={"code": exc.code, "message": exc.message})
            request = _turn_of({"text": item.goal, "agent_id": item.agent_id,
                                "client_request_id": body.client_request_id, "execution_mode": body.execution_mode,
                                "handoff_message_ids": [], "file_refs": [], "plan_item_id": item.id})
            return submit_hub_turn(session, cid, principal, request)

    @app.post("/api/conversations/{cid}/plans/{plan_id}/collect")
    def collect_plan(cid: str, plan_id: int, body: CollectIn, principal: Principal = Depends(authenticate)):
        """One call that hands every sub-task result to an agent (pending approval)."""
        with db.session() as session:
            session.execute(text("BEGIN IMMEDIATE"))
            require_conversation(session, cid, principal)
            request = _turn_of({"text": body.text.strip() or COLLECT_INSTRUCTION, "agent_id": body.agent_id,
                                "client_request_id": body.client_request_id, "execution_mode": body.execution_mode,
                                "handoff_message_ids": [], "file_refs": [], "collect_plan_id": plan_id})
            return submit_hub_turn(session, cid, principal, request)

    @app.post("/api/file-suggestions")
    def file_suggestions(body: SuggestIn, principal: Principal = Depends(authenticate)):
        """Files or ranges the user may add, from the message, the chosen files and
        this conversation's recent references. Suggests only; adds nothing."""
        require_approver(principal)
        with db.session() as session:
            recent = []
            if body.conversation_id is not None:
                require_conversation(session, body.conversation_id, principal)
                recent = recent_file_refs(session, body.conversation_id)
            try:
                found = suggest(app.state.files_root, body.text, body.file_refs, recent)
            except FileRefError as exc:
                raise HTTPException(409, detail={"code": exc.code, "message": exc.message})
        return {"suggestions": found}

    @app.get("/api/files")
    def files(q: str = Query("", max_length=200), limit: int = Query(50, ge=1, le=200),
              principal: Principal = Depends(authenticate)):
        """Paths under the files root that a message may reference (for the picker)."""
        require_approver(principal)
        try:
            return list_files(app.state.files_root, q, limit)
        except FileRefError as exc:
            raise HTTPException(409, detail={"code": exc.code, "message": exc.message})

    @app.post("/api/invocations/{invocation_id}/run")
    def run_invocation(
        invocation_id: str, wait: bool = Query(False),
        principal: Principal = Depends(authenticate),
    ):
        """Claim the approved call now; execute it on the Worker's pool.

        Every refusal (not approved, admission, quota, stopped, native resume)
        is decided before this returns and answered 409 with the approval left
        as it was. An accepted call answers 202 at once with ``status`` as
        stored (``starting``); follow the conversation events for progress.
        ``wait=true`` blocks until the same background execution settles and
        answers 200 with its outcome - for scripts and tests, not for pages.
        """
        require_approver(principal)
        with db.session() as session:
            if session.get(InvocationRow, invocation_id) is None:
                raise HTTPException(404, detail="invocation not found")
            link = session.scalar(select(ChatMessageRow).where(
                ChatMessageRow.invocation_id == invocation_id, ChatMessageRow.role == "user",
            ))
            if link:
                require_conversation(session, link.conversation_id, principal)
        result = app.state.worker.submit(invocation_id)
        future = result.pop("future", None)
        if not result.get("accepted"):
            raise HTTPException(409, detail=result)
        code = 202
        if wait:
            result = {**future.result(), "accepted": True}
            code = 200
        with db.session() as session:
            result["status"] = session.get(InvocationRow, invocation_id).status
        return JSONResponse(status_code=code, content=result)

    @app.post("/api/invocations/{invocation_id}/interrupt")
    def interrupt_invocation(invocation_id: str, principal: Principal = Depends(authenticate)):
        """Ask the provider to interrupt this running call (not a tree stop).

        200 always describes what is known: ``interrupt_requested``,
        ``interrupt_accepted`` (server acknowledged) and ``terminal_received``
        (the turn's own terminal event settled the call). Without the last one
        nothing is claimed stopped and the quota slot stays held.
        """
        require_approver(principal)
        with db.session() as session:
            if session.get(InvocationRow, invocation_id) is None:
                raise HTTPException(404, detail="invocation not found")
            link = session.scalar(select(ChatMessageRow).where(
                ChatMessageRow.invocation_id == invocation_id, ChatMessageRow.role == "user",
            ))
            if link:
                require_conversation(session, link.conversation_id, principal)
        try:
            return app.state.worker.interrupt(invocation_id, requested_by=principal.identity)
        except QuotaError as exc:
            raise HTTPException(404 if exc.code == "no_invocation" else 409,
                                detail={"code": exc.code, "message": exc.message})
