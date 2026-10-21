"""Authenticated persistent chat routes, separate from the legacy console."""

import asyncio
import json
import time
from typing import Literal, Optional

from fastapi import Depends, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select, text
from starlette.concurrency import run_in_threadpool

from agenthub.chat import MAX_MESSAGE_CHARS, ConversationTurn
from agenthub_kernel.conversations import (
    conversation_view, owned_conversation, submit_turn, turn_view,
)
from agenthub_kernel.events import CursorAhead, attach_stream_text, read_events

SSE_RETRY_MS = 3000
SSE_HEARTBEAT_SECONDS = 15.0
SSE_BATCH = 200


def sse_frame(event: dict) -> str:
    data = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
    return f"id: {event['seq']}\nevent: {event['type']}\ndata: {data}\n\n"
from agenthub_kernel.db import ChatMessageRow, ConversationRow, InvocationRow
from agenthub_kernel.service import QuotaError, new_id
from agenthub_rt.auth import Principal, authenticate, require_approver


class ConversationIn(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    title: str = Field(default="", max_length=200)


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

    @field_validator("agent_id", "client_request_id")
    @classmethod
    def nonblank_identifier(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("identifier cannot be blank")
        return value


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
            return {
                "conversations": [conversation_view(row) for row in rows[:limit]],
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
                **conversation_view(conversation), "messages": messages,
                "has_more": len(rows) > limit,
                "next_after_id": messages[-1]["message_id"] if messages else after_id,
            }

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
        request = ConversationTurn(**body.model_dump())
        with db.session() as session:
            session.execute(text("BEGIN IMMEDIATE"))
            try:
                return submit_turn(session, cid, principal.identity, request)
            except QuotaError as exc:
                raise HTTPException(
                    404 if exc.code == "no_conversation" else 409,
                    detail={"code": exc.code, "message": exc.message},
                )

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
