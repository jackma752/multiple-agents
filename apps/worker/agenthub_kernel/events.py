"""Durable, ordered conversation events for cursor replay and SSE.

Events are captured from one choke point, the ORM ``after_flush`` hook, rather
than sprinkled through the approval/quota code. They are written with Core
statements on the flush's own connection, so an event commits or rolls back
together with the state change it describes. Nothing here approves, starts,
retries or settles anything; it only records what the kernel already did.

Payloads contain identifiers and statuses only. Chat text stays in
``chat_messages``; a client reads it with the message cursor. Provisional
model output from :class:`StreamSink` follows the same rule: its text goes to
``stream_chunks`` (chat content) in the same transaction as a ``stream.delta``
event that holds only the chunk id. The authenticated readers attach the
text server-side (:func:`attach_stream_text`), so a client needs no extra
request per event. A delta is not a reply - the assistant ``message.created``
written at settlement is.

Limits (by design):

* Only ORM writes are observed. The approval consume CAS is a Core UPDATE, so
  ``approved -> consumed`` has no own event; every ``invocation.status`` event
  carries the approval's *current* stored status instead of inventing one.
* A status changed twice inside one flush yields one event (first -> last).
* Calls that are not linked to a conversation (legacy ``/api/invocations``)
  produce no conversation events; there is no guessing of history.
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict
from typing import Any, Iterable

from sqlalchemy import event, inspect, insert, select, text, update
from sqlalchemy.engine import Connection
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.orm.attributes import get_history

from agenthub.contracts import (
    STREAM_DELTA, STREAM_REQUEST_DECLINED, STREAM_TURN_ENDED, STREAM_TURN_STARTED,
)

EVENT_TABLES = ("conversation_events", "conversation_event_cursors")

# Tree-level kernel events mirrored into the conversation that owns the tree.
# ``start`` is omitted: it duplicates invocation.status -> starting.
TREE_EVENT_TYPES = {
    "tree_stop", "tree_pause", "tree_resume", "local_stop_requested",
    "unknown_settled", "retry_requested", "policy_updated",
}
# Only these payload keys are copied; free-text fields (stop evidence, policy
# blobs, errors) never enter the event stream.
TREE_PAYLOAD_KEYS = (
    "invocation_id", "superseded_invocation_id", "retry_invocation_id",
    "retry_approval_id", "outcome", "version", "revoked_approvals", "repeat",
    "requested_by", "actor",
)


def install_event_capture(maker: sessionmaker) -> None:
    event.listen(maker, "after_flush", _after_flush)


def _ready(session: Session, conn: Connection) -> bool:
    db = session.info.get("agenthub_db")
    if db is None:
        return False
    if not db.events_ready:
        # A database opened without create_all (e.g. an old file before the
        # additive schema runs) must keep working exactly as before.
        names = set(inspect(conn).get_table_names())
        if not set(EVENT_TABLES) <= names or "conversations" not in names:
            return False
        db.events_ready = True
    return True


def _changed_status(obj: Any) -> tuple[str | None, str | None] | None:
    hist = get_history(obj, "status")
    if not hist.added:
        return None
    before = hist.deleted[0] if hist.deleted else None
    after = hist.added[-1]
    if before is None or before == after:
        return None  # Inserts are described by message.created.
    return before, after


def _conversation_of_invocation(conn: Connection, invocation_id: str) -> str | None:
    from agenthub_kernel.db import ChatMessageRow

    return conn.execute(
        select(ChatMessageRow.conversation_id).where(
            ChatMessageRow.invocation_id == invocation_id,
            ChatMessageRow.role == "user",
        ).limit(1)
    ).scalar()


def _latest_approval(conn: Connection, invocation_id: str) -> tuple[str | None, str | None]:
    from agenthub_kernel.db import ApprovalRow

    row = conn.execute(
        select(ApprovalRow.id, ApprovalRow.status)
        .where(ApprovalRow.invocation_id == invocation_id)
        .order_by(ApprovalRow.created_at.desc(), ApprovalRow.id.desc()).limit(1)
    ).first()
    return (row[0], row[1]) if row else (None, None)


def _invocation_status(conn: Connection, invocation_id: str) -> str | None:
    from agenthub_kernel.db import InvocationRow

    return conn.execute(
        select(InvocationRow.status).where(InvocationRow.id == invocation_id)
    ).scalar()


def _sorted(objs: Iterable[Any], key: str) -> list[Any]:
    return sorted(objs, key=lambda o: getattr(o, key) or 0)


def _after_flush(session: Session, _ctx: Any) -> None:
    from agenthub_kernel.db import (
        ApprovalRow, ChatMessageRow, ConversationRow, EventRow, InvocationRow,
    )

    new = list(session.new)
    dirty = list(session.dirty)
    if not new and not dirty:
        return
    interesting = (ConversationRow, ChatMessageRow, ApprovalRow, InvocationRow, EventRow)
    if not any(isinstance(o, interesting) for o in new + dirty):
        return
    conn = session.connection()
    if not _ready(session, conn):
        return

    out: list[tuple[str, str, dict[str, Any]]] = []
    created = [o for o in new if isinstance(o, ConversationRow)]
    for conv in _sorted(created, "id"):
        out.append((conv.id, "conversation.created", {"conversation_id": conv.id}))

    msgs = _sorted([o for o in new if isinstance(o, ChatMessageRow)], "id")
    for msg in (m for m in msgs if m.role == "user"):
        payload: dict[str, Any] = {
            "message_id": msg.id, "role": msg.role, "invocation_id": msg.invocation_id,
        }
        if msg.invocation_id:
            approval_id, approval_status = _latest_approval(conn, msg.invocation_id)
            payload.update(
                invocation_status=_invocation_status(conn, msg.invocation_id),
                approval_id=approval_id, approval_status=approval_status,
            )
        out.append((msg.conversation_id, "message.created", payload))

    for appr in _sorted([o for o in dirty if isinstance(o, ApprovalRow)], "id"):
        change = _changed_status(appr)
        cid = change and _conversation_of_invocation(conn, appr.invocation_id)
        if cid:
            out.append((cid, "approval.status", {
                "approval_id": appr.id, "invocation_id": appr.invocation_id,
                "from": change[0], "to": change[1],
            }))

    for inv in _sorted([o for o in dirty if isinstance(o, InvocationRow)], "id"):
        change = _changed_status(inv)
        cid = change and _conversation_of_invocation(conn, inv.id)
        if cid:
            approval_id, approval_status = _latest_approval(conn, inv.id)
            out.append((cid, "invocation.status", {
                "invocation_id": inv.id, "task_id": inv.task_id,
                "from": change[0], "to": change[1],
                "approval_id": approval_id, "approval_status": approval_status,
            }))

    tree_events = [o for o in new if isinstance(o, EventRow) and o.type in TREE_EVENT_TYPES]
    for ev in sorted(tree_events, key=lambda e: (e.root_task_id, e.seq or 0, e.id or 0)):
        cid = conn.execute(
            select(ConversationRow.id).where(ConversationRow.root_task_id == ev.root_task_id)
        ).scalar()
        if cid:
            source = ev.payload or {}
            payload = {k: source[k] for k in TREE_PAYLOAD_KEYS if k in source}
            out.append((cid, f"task.{ev.type}", {"root_task_id": ev.root_task_id, **payload}))

    for msg in (m for m in msgs if m.role != "user"):
        out.append((msg.conversation_id, "message.created", {
            "message_id": msg.id, "role": msg.role, "invocation_id": msg.invocation_id,
        }))

    if out:
        _append(conn, out)


def _append(conn: Connection, out: list[tuple[str, str, dict[str, Any]]]) -> None:
    from agenthub_kernel.db import ConversationEventCursorRow as Cur, ConversationEventRow

    grouped: dict[str, list[tuple[str, dict[str, Any]]]] = defaultdict(list)
    order: list[str] = []
    for cid, etype, payload in out:
        if cid not in grouped:
            order.append(cid)
        grouped[cid].append((etype, payload))
    for cid in order:
        items = grouped[cid]
        n = len(items)
        bumped = conn.execute(
            update(Cur).where(Cur.conversation_id == cid)
            .values(last_seq=Cur.last_seq + n)
        ).rowcount
        if bumped != 1:
            # First event of this conversation (or one created before the
            # events schema existed). The PK makes a racing insert fail loudly.
            conn.execute(insert(Cur).values(conversation_id=cid, last_seq=n))
        last = conn.execute(select(Cur.last_seq).where(Cur.conversation_id == cid)).scalar_one()
        base = last - n
        conn.execute(insert(ConversationEventRow), [
            {"conversation_id": cid, "seq": base + i + 1, "type": etype, "payload": payload}
            for i, (etype, payload) in enumerate(items)
        ])


# --------------------------------------------------------------------------- #
# Provisional adapter output
# --------------------------------------------------------------------------- #

#: Allowed keys and value types per stream event; anything else is dropped.
#: A delta's text is not a payload field: it is stored as a chunk.
STREAM_FIELDS: dict[str, dict[str, tuple[type, ...]]] = {
    STREAM_TURN_STARTED: {"thread_id": (str,), "turn_id": (str,), "resumed": (bool,)},
    STREAM_DELTA: {"item_id": (str,)},
    STREAM_REQUEST_DECLINED: {"method": (str,), "item_id": (str,), "decision": (str,)},
    STREAM_TURN_ENDED: {"turn_status": (str,), "verdict": (str,), "terminal": (bool,)},
}
MAX_ID_CHARS = 200
MAX_DELTA_CHARS = 4000
_TEXT = "_text"  # buffered chunk text; never part of a stored payload


class StreamSink:
    """Buffers one invocation's stream output and appends it in order.

    Each flush is one short transaction: delta text into ``stream_chunks``,
    then the events (seq allocated by :func:`_append`), committed or rolled
    back together. A page following the SSE stream therefore sees output while
    the adapter is still running. Consecutive deltas of one item are coalesced
    (flushed every ``flush_seconds`` or ``flush_chars``). A write failure drops
    the buffered output and is counted - never logged, since it is model
    text - and never touches the call's execution state or settlement.
    """

    def __init__(self, db: Any, invocation_id: str, *,
                 flush_seconds: float = 0.2, flush_chars: int = 2000) -> None:
        self.db = db
        self.invocation_id = invocation_id
        self.flush_seconds = flush_seconds
        self.flush_chars = flush_chars
        self.written = 0
        self.dropped = 0
        self.rejected = 0
        self._pending: list[tuple[str, dict[str, Any]]] = []
        self._chars = 0
        self._last_flush = time.monotonic()
        self._lock = threading.Lock()
        self._conversation_id = self._resolve()

    def _resolve(self) -> str | None:
        try:
            with self.db.session() as session:
                conn = session.connection()
                if not _ready(session, conn) or not inspect(conn).has_table("stream_chunks"):
                    return None
                return _conversation_of_invocation(conn, self.invocation_id)
        except Exception:  # noqa: BLE001 - no stream is better than a failed call
            return None

    @property
    def enabled(self) -> bool:
        return self._conversation_id is not None

    def emit(self, etype: str, payload: dict[str, Any]) -> None:
        fields = STREAM_FIELDS.get(etype)
        if fields is None or not isinstance(payload, dict):
            self.rejected += 1
            return
        clean: dict[str, Any] = {}
        for key, kinds in fields.items():
            value = payload.get(key)
            if isinstance(value, kinds) and not (isinstance(value, str) and len(value) > MAX_ID_CHARS):
                clean[key] = value
        piece = payload.get("text")
        if etype == STREAM_DELTA and not (isinstance(piece, str) and piece):
            self.rejected += 1
            return
        if not self.enabled:
            return
        with self._lock:
            if etype == STREAM_DELTA:
                self._add_delta(clean.get("item_id"), piece)
                due = (self._chars >= self.flush_chars
                       or time.monotonic() - self._last_flush >= self.flush_seconds)
            else:
                self._pending.append((etype, clean))
                due = True
            if due:
                self._flush_locked()

    def _add_delta(self, item_id: str | None, piece: str) -> None:
        for start in range(0, len(piece), MAX_DELTA_CHARS):
            chunk = piece[start:start + MAX_DELTA_CHARS]
            last = self._pending[-1] if self._pending else None
            if (last and last[0] == STREAM_DELTA and last[1].get("item_id") == item_id
                    and len(last[1][_TEXT]) + len(chunk) <= MAX_DELTA_CHARS):
                last[1][_TEXT] += chunk
            else:
                entry: dict[str, Any] = {_TEXT: chunk}
                if item_id is not None:
                    entry["item_id"] = item_id
                self._pending.append((STREAM_DELTA, entry))
            self._chars += len(chunk)

    def flush(self) -> None:
        with self._lock:
            self._flush_locked()

    def _flush_locked(self) -> None:
        from agenthub_kernel.db import StreamChunkRow

        batch, self._pending, self._chars = self._pending, [], 0
        self._last_flush = time.monotonic()
        if not batch or not self.enabled:
            return
        try:
            with self.db.session() as session:
                session.execute(text("BEGIN IMMEDIATE"))
                conn = session.connection()
                out = []
                for etype, entry in batch:
                    payload = {"invocation_id": self.invocation_id,
                               **{k: v for k, v in entry.items() if k != _TEXT}}
                    if etype == STREAM_DELTA:
                        payload["chunk_id"] = conn.execute(insert(StreamChunkRow).values(
                            conversation_id=self._conversation_id,
                            invocation_id=self.invocation_id,
                            item_id=entry.get("item_id"), text=entry[_TEXT],
                        )).inserted_primary_key[0]
                    out.append((self._conversation_id, etype, payload))
                _append(conn, out)
        except Exception:  # noqa: BLE001 - may concern model text; never logged
            self.dropped += len(batch)
            return
        self.written += len(batch)

    def close(self) -> dict[str, int]:
        self.flush()
        return {"written": self.written, "dropped": self.dropped, "rejected": self.rejected}


# --------------------------------------------------------------------------- #
# Read side
# --------------------------------------------------------------------------- #

class CursorAhead(Exception):
    """Client cursor is beyond the stored stream (e.g. database was replaced)."""

    def __init__(self, last_seq: int) -> None:
        super().__init__(last_seq)
        self.last_seq = last_seq


def last_seq(session: Session, conversation_id: str) -> int:
    from agenthub_kernel.db import ConversationEventCursorRow as Cur

    return int(session.scalar(
        select(Cur.last_seq).where(Cur.conversation_id == conversation_id)
    ) or 0)


def event_view(row: Any) -> dict[str, Any]:
    return {
        "seq": row.seq, "type": row.type, "payload": row.payload,
        "created_at": row.created_at.isoformat() + "Z",
    }


def attach_stream_text(
    session: Session, conversation_id: str, events: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Return copies of ``events`` with each delta's chunk text attached.

    Only for authenticated readers that already checked conversation
    ownership. Chunks are looked up within ``conversation_id`` only, so a
    chunk id from another conversation is never resolved. Stored rows are not
    modified; a missing chunk leaves the event without ``text``.
    """
    from agenthub_kernel.db import StreamChunkRow

    ids = {e["payload"].get("chunk_id") for e in events
           if e["type"] == STREAM_DELTA and isinstance(e["payload"].get("chunk_id"), int)}
    if not ids:
        return events
    texts = dict(session.execute(
        select(StreamChunkRow.id, StreamChunkRow.text).where(
            StreamChunkRow.conversation_id == conversation_id, StreamChunkRow.id.in_(ids),
        )
    ).all())
    out = []
    for e in events:
        chunk_id = e["payload"].get("chunk_id") if e["type"] == STREAM_DELTA else None
        if chunk_id in texts:
            e = {**e, "payload": {**e["payload"], "text": texts[chunk_id]}}
        out.append(e)
    return out


def read_events(
    session: Session, conversation_id: str, after_seq: int, limit: int,
) -> dict[str, Any]:
    """Events with ``seq > after_seq`` in order; raises CursorAhead."""
    from agenthub_kernel.db import ConversationEventRow

    current = last_seq(session, conversation_id)
    if after_seq > current:
        raise CursorAhead(current)
    rows = session.scalars(
        select(ConversationEventRow).where(
            ConversationEventRow.conversation_id == conversation_id,
            ConversationEventRow.seq > after_seq,
        ).order_by(ConversationEventRow.seq).limit(limit + 1)
    ).all()
    events = [event_view(r) for r in rows[:limit]]
    return {
        "events": events,
        "next_after_seq": events[-1]["seq"] if events else after_seq,
        "has_more": len(rows) > limit,
        "last_seq": current,
    }
