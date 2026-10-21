"""Authenticated tool approval cards (one per provider tool request).

Only a user principal may read or answer a card; an agent or worker token is
refused like any approval endpoint. Cards of a conversation call are visible
only to that conversation's owner (others get 404). ``details`` is returned
here and nowhere else.
"""

from typing import Literal, Optional

from fastapi import Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from agenthub_kernel.conversations import owned_conversation
from agenthub_kernel.db import InvocationRow, ToolApprovalRow
from agenthub_kernel.service import QuotaError
from agenthub_kernel.tool_approvals import card_view, conversation_of, decide
from agenthub_rt.auth import Principal, authenticate, require_approver


class DecisionIn(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    # Required to approve; optional to decline (declining is always safe).
    card_hash: Optional[str] = Field(default=None, min_length=64, max_length=64)


def register_tool_approvals(app, db) -> None:
    def visible(session, row: Optional[ToolApprovalRow], principal: Principal) -> ToolApprovalRow:
        if row is None:
            raise HTTPException(404, detail="tool approval not found")
        cid = conversation_of(session, row.invocation_id)
        if cid is not None:
            try:
                owned_conversation(session, cid, principal.identity)
            except QuotaError:
                raise HTTPException(404, detail="tool approval not found")
        return row

    @app.get("/api/conversations/{cid}/tool-approvals")
    def list_tool_approvals(
        cid: str,
        status: Optional[Literal["pending", "approved", "declined", "expired", "void"]] = Query(None),
        principal: Principal = Depends(authenticate),
    ):
        require_approver(principal)
        from agenthub_kernel.db import ChatMessageRow

        with db.session() as session:
            try:
                owned_conversation(session, cid, principal.identity)
            except QuotaError:
                raise HTTPException(404, detail="conversation not found")
            stmt = (
                select(ToolApprovalRow)
                .join(InvocationRow, InvocationRow.id == ToolApprovalRow.invocation_id)
                .join(ChatMessageRow, ChatMessageRow.invocation_id == InvocationRow.id)
                .where(ChatMessageRow.conversation_id == cid, ChatMessageRow.role == "user")
                .order_by(ToolApprovalRow.created_at, ToolApprovalRow.id)
            )
            if status is not None:
                stmt = stmt.where(ToolApprovalRow.status == status)
            return {"conversation_id": cid,
                    "tool_approvals": [card_view(session, r) for r in session.scalars(stmt).all()]}

    @app.get("/api/tool-approvals/{card_id}")
    def get_tool_approval(card_id: str, principal: Principal = Depends(authenticate)):
        require_approver(principal)
        with db.session() as session:
            row = visible(session, session.get(ToolApprovalRow, card_id), principal)
            return card_view(session, row)

    def answer(card_id: str, decision: str, body: Optional[DecisionIn], principal: Principal):
        require_approver(principal)
        with db.session() as session:
            visible(session, session.get(ToolApprovalRow, card_id), principal)
        with db.session() as session:
            try:
                row = decide(session, card_id, decision=decision, actor=principal.identity,
                             presented_hash=body.card_hash if body else None)
            except QuotaError as exc:
                raise HTTPException(404 if exc.code == "no_tool_approval" else 409,
                                    detail={"code": exc.code, "message": exc.message})
            return card_view(session, row)

    @app.post("/api/tool-approvals/{card_id}/approve")
    def approve_tool(card_id: str, body: Optional[DecisionIn] = None,
                     principal: Principal = Depends(authenticate)):
        """Approve this one request. Not a session-wide or future approval."""
        return answer(card_id, "approved", body, principal)

    @app.post("/api/tool-approvals/{card_id}/decline")
    def decline_tool(card_id: str, body: Optional[DecisionIn] = None,
                     principal: Principal = Depends(authenticate)):
        return answer(card_id, "declined", body, principal)
