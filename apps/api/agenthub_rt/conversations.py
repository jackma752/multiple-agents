"""Authenticated persistent chat routes, separate from the legacy console."""

from typing import Literal

from fastapi import Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select, text

from agenthub.chat import MAX_MESSAGE_CHARS, ConversationTurn
from agenthub_kernel.conversations import (
    conversation_view, owned_conversation, submit_turn, turn_view,
)
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
    def run_invocation(invocation_id: str, principal: Principal = Depends(authenticate)):
        require_approver(principal)
        with db.session() as session:
            if session.get(InvocationRow, invocation_id) is None:
                raise HTTPException(404, detail="invocation not found")
            link = session.scalar(select(ChatMessageRow).where(
                ChatMessageRow.invocation_id == invocation_id, ChatMessageRow.role == "user",
            ))
            if link:
                require_conversation(session, link.conversation_id, principal)
        result = app.state.worker.run_one(invocation_id)
        if not result.get("adapter_start_attempted"):
            raise HTTPException(409, detail=result)
        with db.session() as session:
            result["status"] = session.get(InvocationRow, invocation_id).status
        return result
