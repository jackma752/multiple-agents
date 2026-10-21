"""Tool approval cards: one user decision per provider tool request.

Approving a call never approves its tools (AGENTS.md). While a call runs, a
provider may ask to run a command or change files; the adapter opens a card
through :class:`ToolGate` and waits. Only a user, through the API
(:func:`decide`), can approve or decline it.

Rules enforced here, not in the adapter or the page:

* A card answers exactly one request: ``invocation_id`` + JSON-RPC
  ``request_key`` + thread/turn/item. Approving it can never answer another.
* ``approved`` is the only status that becomes ``accept``. Declined, expired
  (``expires_at`` passed) and void (the request, turn or transport ended
  first) cards are answered ``decline`` or not at all.
* A decision is accepted only while the card is pending, unexpired, the call is
  still starting/running, and - for approval - the caller presents the card's
  current ``card_hash`` (a stale page cannot approve something it never saw).
* Every transition happens under the writer lock with a fresh read, so an
  approval racing an expiry or a second approval yields exactly one outcome.

``details`` may quote user or model content; it is returned only by the
authenticated card endpoints. Events carry ids and statuses only.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from typing import Any, Optional

from sqlalchemy import select, text

from agenthub.contracts import (
    TOOL_APPROVAL_APPROVED, TOOL_APPROVAL_DECLINED, TOOL_APPROVAL_EXPIRED,
    TOOL_APPROVAL_PENDING, TOOL_APPROVAL_VOID, TOOL_DELIVERY_FAILED,
    TOOL_DELIVERY_NOT_SENT, TOOL_DELIVERY_SENT,
)
from agenthub_kernel.db import ChatMessageRow, InvocationRow, ToolApprovalRow
from agenthub_kernel.service import QuotaError, new_id, now

ACTIVE_CALL_STATUSES = ("starting", "running")
MAX_DETAIL_CHARS = 8000


def card_hash(row: ToolApprovalRow) -> str:
    """Fingerprint of everything the user is shown and asked to approve."""
    bound = {
        "id": row.id, "invocation_id": row.invocation_id, "method": row.method,
        "request_key": row.request_key, "thread_id": row.thread_id,
        "turn_id": row.turn_id, "item_id": row.item_id, "details": row.details,
        "available_decisions": row.available_decisions,
        "expires_at": row.expires_at.isoformat(),
    }
    return hashlib.sha256(
        json.dumps(bound, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def conversation_of(session: Any, invocation_id: str) -> Optional[str]:
    return session.scalar(select(ChatMessageRow.conversation_id).where(
        ChatMessageRow.invocation_id == invocation_id, ChatMessageRow.role == "user",
    ).limit(1))


def card_view(session: Any, row: ToolApprovalRow) -> dict[str, Any]:
    return {
        "tool_approval_id": row.id, "invocation_id": row.invocation_id,
        "conversation_id": conversation_of(session, row.invocation_id),
        "method": row.method, "thread_id": row.thread_id, "turn_id": row.turn_id,
        "item_id": row.item_id, "details": row.details,
        "available_decisions": row.available_decisions, "status": row.status,
        "expires_at": row.expires_at.isoformat() + "Z",
        "decided_by": row.decided_by,
        "decided_at": row.decided_at.isoformat() + "Z" if row.decided_at else None,
        "delivery": row.delivery, "decision_sent": row.decision_sent,
        "card_hash": card_hash(row),
    }


def _clean_details(details: Any) -> dict[str, Any]:
    """Keep only JSON-safe, size-bounded values; the card must stay readable."""
    out: dict[str, Any] = {}
    if not isinstance(details, dict):
        return out
    for key, value in details.items():
        if not isinstance(key, str):
            continue
        encoded = json.dumps(value, ensure_ascii=False, default=str)
        if len(encoded) > MAX_DETAIL_CHARS:
            value = encoded[:MAX_DETAIL_CHARS] + "…[truncated]"
        out[key] = value
    return out


def _locked(session: Any, card_id: str) -> Optional[ToolApprovalRow]:
    # BEGIN IMMEDIATE (SQLite) / FOR UPDATE (PostgreSQL): read the current
    # status only after taking the lock, so transitions serialize.
    session.execute(text("BEGIN IMMEDIATE"))
    return session.scalar(
        select(ToolApprovalRow).where(ToolApprovalRow.id == card_id).with_for_update()
    )


def decide(session: Any, card_id: str, *, decision: str, actor: str,
           presented_hash: Optional[str]) -> ToolApprovalRow:
    """User decision. Raises QuotaError (-> 404/409) unless it can apply."""
    if decision not in (TOOL_APPROVAL_APPROVED, TOOL_APPROVAL_DECLINED):
        raise QuotaError("bad_decision", "decision must be approved or declined")
    row = _locked(session, card_id)
    if row is None:
        raise QuotaError("no_tool_approval", "tool approval not found")
    if row.status != TOOL_APPROVAL_PENDING:
        raise QuotaError("not_pending", f"tool approval is already {row.status}")
    if now() >= row.expires_at:
        raise QuotaError("expired", "tool approval has expired; it will be declined")
    inv = session.get(InvocationRow, row.invocation_id)
    if inv is None or inv.status not in ACTIVE_CALL_STATUSES:
        raise QuotaError("call_not_active", "the call that asked is no longer running")
    if presented_hash is not None and presented_hash != card_hash(row):
        raise QuotaError("card_changed", "card_hash does not match this request")
    if decision == TOOL_APPROVAL_APPROVED:
        if presented_hash is None:
            raise QuotaError("card_hash_required", "approval must present the card_hash")
        offered = row.available_decisions
        if isinstance(offered, list) and "accept" not in offered:
            raise QuotaError("accept_not_offered", "the provider did not offer accept")
    row.status = decision
    row.decided_by = actor
    row.decided_at = now()
    session.flush()
    return row


class ToolGate:
    """The Worker's handle for one call; the adapter sees only these methods.

    Each method is its own short transaction. A database failure while opening
    returns None, which the adapter answers with ``decline``: an unrecorded
    request is never left waiting for an approval no one can give.
    """

    def __init__(self, db: Any, invocation_id: str, *, ttl_seconds: float,
                 before_open: Optional[Any] = None) -> None:
        self.db = db
        self.invocation_id = invocation_id
        self.ttl_seconds = ttl_seconds
        # Called before a card is recorded. The Worker passes the stream sink's
        # flush, so output that led up to a request precedes its card in the
        # event stream instead of sitting in a buffer behind it.
        self.before_open = before_open
        self.opened: list[str] = []

    def open(self, *, method: str, request_key: str, thread_id: str, turn_id: str,
             item_id: str, details: Any, available_decisions: Any,
             deadline: Optional[dt.datetime] = None) -> Optional[str]:
        if self.before_open is not None:
            try:
                self.before_open()
            except Exception:  # noqa: BLE001 - ordering aid only; never blocks the card
                pass
        expires = now() + dt.timedelta(seconds=self.ttl_seconds)
        if deadline is not None:
            expires = min(expires, deadline)
        offered = ([d for d in available_decisions if isinstance(d, str)]
                   if isinstance(available_decisions, list) else None)
        try:
            with self.db.session() as session:
                session.execute(text("BEGIN IMMEDIATE"))
                row = ToolApprovalRow(
                    id=new_id("tool"), invocation_id=self.invocation_id,
                    method=str(method)[:200], request_key=str(request_key)[:200],
                    thread_id=str(thread_id)[:200], turn_id=str(turn_id)[:200],
                    item_id=str(item_id or "")[:200], details=_clean_details(details),
                    available_decisions=offered, status=TOOL_APPROVAL_PENDING,
                    expires_at=expires,
                )
                session.add(row)
                session.flush()
                card_id = row.id
        except Exception:  # noqa: BLE001 - details may quote content; never logged
            return None
        self.opened.append(card_id)
        return card_id

    def poll(self, card_id: str) -> str:
        """Current status; a pending card past ``expires_at`` becomes expired."""
        try:
            with self.db.session() as session:
                row = session.get(ToolApprovalRow, card_id)
                if row is None:
                    return TOOL_APPROVAL_VOID
                if row.status != TOOL_APPROVAL_PENDING or now() < row.expires_at:
                    return row.status
            with self.db.session() as session:
                row = _locked(session, card_id)
                if row.status == TOOL_APPROVAL_PENDING and now() >= row.expires_at:
                    row.status = TOOL_APPROVAL_EXPIRED
                    session.flush()
                return row.status
        except Exception:  # noqa: BLE001
            return TOOL_APPROVAL_PENDING  # retry next pass; never guess "approved"

    def delivered(self, card_id: str, *, decision: Optional[str], ok: bool) -> None:
        try:
            with self.db.session() as session:
                row = _locked(session, card_id)
                if row is None:
                    return
                row.delivery = TOOL_DELIVERY_SENT if ok else TOOL_DELIVERY_FAILED
                row.decision_sent = decision
                session.flush()
        except Exception:  # noqa: BLE001
            pass

    def void_pending(self) -> int:
        """Close this call's unanswered cards; their requests are gone."""
        return void_pending(self.db, invocation_ids=[self.invocation_id])


def void_pending(db: Any, *, invocation_ids: Optional[list[str]] = None,
                 exclude: Optional[set[str]] = None) -> int:
    """Mark pending cards void (and undelivered cards not_sent)."""
    from sqlalchemy import inspect

    if invocation_ids is not None and not invocation_ids:
        return 0
    closed = 0
    with db.session() as session:
        if not inspect(session.connection()).has_table("tool_approvals"):
            return 0  # a database opened without the additive schema has no cards
        session.execute(text("BEGIN IMMEDIATE"))
        stmt = select(ToolApprovalRow).where(ToolApprovalRow.status == TOOL_APPROVAL_PENDING)
        if invocation_ids is not None:
            stmt = stmt.where(ToolApprovalRow.invocation_id.in_(invocation_ids))
        for row in session.scalars(stmt).all():
            if exclude and row.invocation_id in exclude:
                continue
            row.status = TOOL_APPROVAL_VOID
            if row.delivery == "pending":
                row.delivery = TOOL_DELIVERY_NOT_SENT
            closed += 1
        session.flush()
    return closed
