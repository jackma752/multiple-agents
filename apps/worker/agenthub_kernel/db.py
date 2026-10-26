"""SQLAlchemy persistence for the Agent Hub kernel.

Uses SQLite in the P2 Mock deployment (per the confirmed stack). The schema is
kept intentionally small and mirrors AGENT_HUB_PLAN §5's table list:
``agents, projects, conversations, tasks, invocations, approvals,
policy_versions, quota_reservations, events, audit_logs``.

The approval & quota kernel and persistent local chat use these tables:
``tasks`` (the root-task tree), ``invocations``, ``approvals``,
``policy_versions``, ``events``, ``quota_reservations``, ``tree_quota``,
``conversations``, ``chat_messages``, ``conversation_events``,
``conversation_event_cursors``, ``stream_chunks``, ``tool_approvals``,
``tool_gate_closures``, ``usage_ledger``, ``tree_token_budget``,
``token_budget_items``, ``conversation_summaries``, ``conversation_links``,
``collab_plans``, ``collab_items`` and ``collab_attempts``.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Iterator

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    create_engine,
)
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.pool import StaticPool
from sqlalchemy.sql import func
from sqlalchemy.types import JSON


class Base(DeclarativeBase):
    pass


class TaskRow(Base):
    """A node in the root-task tree.

    ``root_task_id`` identifies the whole tree; ``parent_task_id`` the direct
    parent. ``depth`` is 0 for the main agent. ``kind`` is USER or AUTO.
    """

    __tablename__ = "tasks"

    id = Column(String, primary_key=True)
    root_task_id = Column(String, index=True, nullable=False)
    parent_task_id = Column(String, nullable=True)
    depth = Column(Integer, nullable=False, default=0)
    kind = Column(String, nullable=False, default="user")  # DelegationKind value
    goal = Column(Text, nullable=False)
    agent_id = Column(String, nullable=False)
    status = Column(String, nullable=False, default="draft")
    policy_version = Column(Integer, nullable=False, default=1)
    # Effective policy for the whole tree, stored on the ROOT task node.
    policy = Column(JSON, nullable=True)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)


class InvocationRow(Base):
    """One actual adapter call.

    ``payload_hash`` is the immutable snapshot hash the approval is bound to.
    """

    __tablename__ = "invocations"

    id = Column(String, primary_key=True)
    task_id = Column(
        ForeignKey("tasks.id", ondelete="CASCADE"), index=True, nullable=False
    )
    payload_hash = Column(String, nullable=False)
    snapshot = Column(JSON, nullable=False, default=dict)
    status = Column(String, nullable=False, default="draft")
    adapter_invocation_id = Column(String, nullable=True)
    error = Column(Text, nullable=True)
    started_at = Column(DateTime, nullable=True)
    finished_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)


class ApprovalRow(Base):
    """A user approval, one-shot and bound to an invocation's payload_hash."""

    __tablename__ = "approvals"

    id = Column(String, primary_key=True)  # approval_id
    invocation_id = Column(
        ForeignKey("invocations.id", ondelete="CASCADE"), index=True, nullable=False
    )
    payload_hash = Column(String, nullable=False)
    user_id = Column(String, nullable=False)
    status = Column(String, nullable=False, default="pending")
    policy_version = Column(Integer, nullable=False, default=1)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    approved_at = Column(DateTime, nullable=True)
    expires_at = Column(DateTime, nullable=True)
    consumed_at = Column(DateTime, nullable=True)


class ConversationRow(Base):
    __tablename__ = "conversations"

    id = Column(String, primary_key=True)
    user_id = Column(String, index=True, nullable=False)
    title = Column(String, nullable=False, default="")
    root_task_id = Column(ForeignKey("tasks.id"), nullable=True, unique=True)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)


class ChatMessageRow(Base):
    __tablename__ = "chat_messages"
    __table_args__ = (
        UniqueConstraint("conversation_id", "client_request_id"),
        UniqueConstraint("invocation_id", "role"),
        # Prevent deleted high IDs from ever being reused as pagination cursors.
        {"sqlite_autoincrement": True},
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    conversation_id = Column(ForeignKey("conversations.id"), index=True, nullable=False)
    role = Column(String, nullable=False)
    text = Column(Text, nullable=False)
    invocation_id = Column(ForeignKey("invocations.id"), index=True, nullable=True)
    client_request_id = Column(String, nullable=True)
    request_hash = Column(String, nullable=True)
    result_metadata = Column(JSON, nullable=True)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)


class ConversationSummaryRow(Base):
    """One saved version of a conversation's stage summary (T-2, ``agenthub.summary``).

    Versions are immutable: editing saves a new row. ``source`` is ``manual``
    or ``draft:<message_id>`` (edited from a drafting call's reply).
    """

    __tablename__ = "conversation_summaries"
    __table_args__ = (UniqueConstraint("conversation_id", "version"), {"sqlite_autoincrement": True})

    id = Column(Integer, primary_key=True, autoincrement=True)
    conversation_id = Column(ForeignKey("conversations.id"), index=True, nullable=False)
    version = Column(Integer, nullable=False)
    goal = Column(Text, nullable=False)
    constraints = Column(Text, nullable=False, default="")
    done = Column(Text, nullable=False, default="")
    open_issues = Column(Text, nullable=False, default="")
    next_steps = Column(Text, nullable=False, default="")
    file_refs = Column(JSON, nullable=False, default=list)
    source = Column(String, nullable=False, default="manual")
    created_at = Column(DateTime, server_default=func.now(), nullable=False)


class ConversationLinkRow(Base):
    """A conversation started by "compress and continue": where it came from (T-2).

    A new table, not columns on ``conversations``: ``create_all`` never alters
    existing tables. The source conversation is left as it was.
    """

    __tablename__ = "conversation_links"

    conversation_id = Column(ForeignKey("conversations.id"), primary_key=True)
    parent_conversation_id = Column(ForeignKey("conversations.id"), index=True, nullable=False)
    summary_id = Column(ForeignKey("conversation_summaries.id"), nullable=False)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)


class CollabPlanRow(Base):
    """A collaboration plan of a conversation (P4, ``agenthub.collab``).

    ``source`` is ``manual`` or ``reply:<message_id>`` (edited from the main
    agent's proposal); its items are dispatched as ``user`` or ``auto`` tasks.
    """

    __tablename__ = "collab_plans"
    __table_args__ = ({"sqlite_autoincrement": True},)

    id = Column(Integer, primary_key=True, autoincrement=True)
    conversation_id = Column(ForeignKey("conversations.id"), index=True, nullable=False)
    source = Column(String, nullable=False, default="manual")
    created_at = Column(DateTime, server_default=func.now(), nullable=False)


class CollabItemRow(Base):
    """One sub-task of a plan; ``task_id`` is its depth-1 task once first dispatched."""

    __tablename__ = "collab_items"
    __table_args__ = (UniqueConstraint("plan_id", "idx"), {"sqlite_autoincrement": True})

    id = Column(Integer, primary_key=True, autoincrement=True)
    plan_id = Column(ForeignKey("collab_plans.id"), index=True, nullable=False)
    idx = Column(Integer, nullable=False)
    agent_id = Column(String, nullable=False)
    goal = Column(Text, nullable=False)
    depends_on = Column(JSON, nullable=False, default=list)
    task_id = Column(ForeignKey("tasks.id"), nullable=True)


class CollabAttemptRow(Base):
    """One dispatch of a plan item: its call, numbered from 1."""

    __tablename__ = "collab_attempts"

    invocation_id = Column(ForeignKey("invocations.id"), primary_key=True)
    item_id = Column(ForeignKey("collab_items.id"), index=True, nullable=False)
    attempt = Column(Integer, nullable=False)


class StageRunRow(Base):
    """One user-authorized stage of automatic execution (P4.6, ``agenthub.stage``).

    ``rules`` is what the user approved (``StageRules.as_dict``) and
    ``rules_hash`` its fingerprint; ``checkpoint`` is the scheduler's
    progress; ``summary`` the completion or blocker summary once it ends.
    """

    __tablename__ = "stage_runs"

    id = Column(String, primary_key=True)
    conversation_id = Column(ForeignKey("conversations.id"), index=True, nullable=False)
    root_task_id = Column(String, index=True, nullable=False)
    status = Column(String, nullable=False)
    execution_mode = Column(String, nullable=False)
    goal = Column(Text, nullable=False)
    criteria = Column(Text, nullable=False, default="")
    rules = Column(JSON, nullable=False)
    rules_hash = Column(String, nullable=False)
    supervised_consent = Column(Boolean, nullable=False, default=False)
    granted_by = Column(String, nullable=False)
    expires_at = Column(DateTime, nullable=True)
    checkpoint = Column(JSON, nullable=False, default=dict)
    summary = Column(JSON, nullable=True)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)


class ProviderQuotaRow(Base):
    """One cooldown/probe per local provider bucket, shared by stages and persisted across restart."""

    __tablename__ = "provider_quota"
    bucket = Column(String, primary_key=True)
    next_check_at = Column(DateTime, nullable=False)
    reset_at = Column(DateTime, nullable=True)
    backoff_seconds = Column(Integer, nullable=False, default=300)
    probe_invocation_id = Column(String, nullable=True)
    last_failure_id = Column(String, nullable=True)


class StageGrantRow(Base):
    """The authorization source of an approval the scheduler gave under a stage's rules."""

    __tablename__ = "stage_grants"

    approval_id = Column(ForeignKey("approvals.id"), primary_key=True)
    invocation_id = Column(ForeignKey("invocations.id"), index=True, nullable=False)
    stage_id = Column(ForeignKey("stage_runs.id"), index=True, nullable=False)
    step = Column(String, nullable=False)
    rule = Column(String, nullable=False)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)


class StageEventRow(Base):
    """What the scheduler did under a stage (approvals, tool decisions, steps, the end).

    ``detail`` holds ids, codes and short reasons only - never prompts or replies.
    """

    __tablename__ = "stage_events"
    __table_args__ = ({"sqlite_autoincrement": True},)

    id = Column(Integer, primary_key=True, autoincrement=True)
    stage_id = Column(ForeignKey("stage_runs.id"), index=True, nullable=False)
    kind = Column(String, nullable=False)
    invocation_id = Column(String, nullable=True)
    tool_approval_id = Column(String, nullable=True)
    detail = Column(JSON, nullable=False, default=dict)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)


class PolicyVersionRow(Base):
    """A stored policy version (audit of what limits were in effect)."""

    __tablename__ = "policy_versions"

    id = Column(String, primary_key=True)
    version = Column(Integer, nullable=False, default=1)
    root_task_id = Column(String, index=True, nullable=False)
    payload = Column(JSON, nullable=False, default=dict)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    active = Column(Boolean, nullable=False, default=True)


class QuotaReservationRow(Base):
    """A reserved concurrent slot for a tree.

    One row per running/starting invocation. ``released_at`` is set when the
    invocation finishes; ``total_count`` on the task tracks the cumulative
    launched-sub-agent counter which is NOT returned on finish.
    """

    __tablename__ = "quota_reservations"

    id = Column(String, primary_key=True)
    invocation_id = Column(
        ForeignKey("invocations.id", ondelete="CASCADE"), index=True, nullable=False
    )
    root_task_id = Column(String, index=True, nullable=False)
    released_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)


class TreeQuotaRow(Base):
    """Atomic per-tree quota counters.

    Read-then-write quota checks race: several concurrent workers can all read
    "0 launched / 0 running" and then all proceed, breaking the whole-tree cap.
    This row exists so a reservation can be taken with a single conditional
    UPDATE (compare-and-swap) whose WHERE clause enforces the limits. Exactly
    one racer can win the last slot.

    Semantics (AGENT_HUB_PLAN §4.2):

    * ``launched_total`` is **cumulative** for the tree; it is never decremented,
      so finishing or failing a sub-agent does not return total budget.
    * ``concurrent`` is incremented on reserve and decremented on release.
    * ``total_invocations`` counts every consumed invocation (main + sub).
    """

    __tablename__ = "tree_quota"

    root_task_id = Column(String, primary_key=True)
    launched_total = Column(Integer, nullable=False, default=0)
    concurrent = Column(Integer, nullable=False, default=0)
    total_invocations = Column(Integer, nullable=False, default=0)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())


class EventRow(Base):
    """Streamed / audited event. ``seq`` is the per-tree ordering."""

    __tablename__ = "events"

    id = Column(Integer, primary_key=True, autoincrement=True)
    root_task_id = Column(String, index=True, nullable=False)
    seq = Column(Integer, nullable=False)
    type = Column(String, nullable=False)
    payload = Column(JSON, nullable=False, default=dict)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)


class ConversationEventRow(Base):
    """Ordered conversation event; ``seq`` is dense per conversation.

    Written only by :mod:`agenthub_kernel.events` in the same transaction as
    the state change it describes, so replay never shows an uncommitted or
    rolled-back change. Payloads hold IDs and statuses, never chat text.
    """

    __tablename__ = "conversation_events"
    __table_args__ = (
        UniqueConstraint("conversation_id", "seq"),
        {"sqlite_autoincrement": True},
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    conversation_id = Column(ForeignKey("conversations.id"), index=True, nullable=False)
    seq = Column(Integer, nullable=False)
    type = Column(String, nullable=False)
    payload = Column(JSON, nullable=False, default=dict)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)


class StreamChunkRow(Base):
    """Provisional model output of a running call, managed like chat content.

    The ``stream.delta`` event that announces a chunk stores only its id; the
    authenticated event readers attach ``text`` server-side. A chunk and its
    event are written in one transaction. Not a reply: the assistant
    ``chat_messages`` row written at settlement is.
    """

    __tablename__ = "stream_chunks"
    __table_args__ = ({"sqlite_autoincrement": True},)

    id = Column(Integer, primary_key=True, autoincrement=True)
    conversation_id = Column(ForeignKey("conversations.id"), index=True, nullable=False)
    invocation_id = Column(ForeignKey("invocations.id"), index=True, nullable=False)
    item_id = Column(String, nullable=True)
    text = Column(Text, nullable=False)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)


class ToolApprovalRow(Base):
    """One provider tool-approval request during a running call (a "card").

    Bound to the exact request: ``invocation_id`` + ``request_key`` (the
    JSON-RPC id) + ``thread_id``/``turn_id``/``item_id``. Answerable once, by a
    user, before ``expires_at`` and only while the call is active. ``details``
    (command, cwd, reason) can quote user or model content and is managed like
    chat text: it is returned only by the authenticated card endpoints, never
    copied into events. ``delivery`` records whether the provider actually got
    the answer - an approval is not proof that anything ran.
    """

    __tablename__ = "tool_approvals"
    __table_args__ = (UniqueConstraint("invocation_id", "request_key"),)

    id = Column(String, primary_key=True)
    invocation_id = Column(ForeignKey("invocations.id"), index=True, nullable=False)
    method = Column(String, nullable=False)
    request_key = Column(String, nullable=False)
    thread_id = Column(String, nullable=False)
    turn_id = Column(String, nullable=False)
    item_id = Column(String, nullable=False, default="")
    details = Column(JSON, nullable=False, default=dict)
    available_decisions = Column(JSON, nullable=True)
    status = Column(String, nullable=False, default="pending")
    expires_at = Column(DateTime, nullable=False)
    decided_by = Column(String, nullable=True)
    decided_at = Column(DateTime, nullable=True)
    delivery = Column(String, nullable=False, default="pending")
    decision_sent = Column(String, nullable=True)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)


class ToolGateClosureRow(Base):
    """No tool decision of this call may be sent to the provider any more.

    Written in the same transaction as an interrupt / tree stop / local stop
    request, before anything is sent to the provider: that commit is the
    effective point. ``reason`` is ``interrupt``, ``tree_stop`` or
    ``local_stop``. One row per call; later requests are repeats.
    """

    __tablename__ = "tool_gate_closures"

    invocation_id = Column(ForeignKey("invocations.id"), primary_key=True)
    reason = Column(String, nullable=False)
    closed_by = Column(String, nullable=False)
    closed_at = Column(DateTime, server_default=func.now(), nullable=False)


class UsageLedgerRow(Base):
    """One call's token usage in Hub terms (T-1a, ``agenthub.usage``).

    One row per invocation that reached its adapter, written in the same
    transaction as its settlement, failures and interrupts included. A number
    that is not known is NULL, never 0; ``completeness`` and ``notes`` (machine
    codes) say why. ``raw`` holds the provider's numbers only - no prompt,
    reply or tool text. Conversation, root-task and per-agent totals are
    queries over this table, never a second copy.
    """

    __tablename__ = "usage_ledger"

    invocation_id = Column(ForeignKey("invocations.id"), primary_key=True)
    root_task_id = Column(String, index=True, nullable=False)
    task_id = Column(String, nullable=False)
    agent_id = Column(String, index=True, nullable=False)
    conversation_id = Column(String, index=True, nullable=True)
    mapping = Column(String, nullable=False)
    mapping_version = Column(Integer, nullable=False)
    completeness = Column(String, nullable=False)
    source = Column(String, nullable=False)
    input_total = Column(Integer, nullable=True)
    input_uncached = Column(Integer, nullable=True)
    cache_read = Column(Integer, nullable=True)
    cache_write = Column(Integer, nullable=True)
    output_total = Column(Integer, nullable=True)
    reasoning = Column(Integer, nullable=True)
    notes = Column(JSON, nullable=False, default=list)
    raw = Column(JSON, nullable=False, default=dict)
    model = Column(String, nullable=True)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)


class TreeTokenBudgetRow(Base):
    """Per-tree token counters for atomic budget admission (T-1c).

    A strict reservation is taken with one conditional UPDATE whose WHERE
    clause encodes the total budget, as :class:`TreeQuotaRow` does for slots,
    so concurrent calls cannot together cross the remaining budget. A new
    table (not new columns on ``tree_quota``): ``create_all`` adds tables but
    never alters existing ones. Always equal to the sums over
    :class:`TokenBudgetItemRow`; created lazily from the tree's ledger rows.
    """

    __tablename__ = "tree_token_budget"

    root_task_id = Column(String, primary_key=True)
    known = Column(Integer, nullable=False, default=0)
    unsettled = Column(Integer, nullable=False, default=0)
    reserved = Column(Integer, nullable=False, default=0)
    unbounded = Column(Integer, nullable=False, default=0)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())


class TokenBudgetItemRow(Base):
    """One call's part of its tree's token counters.

    ``reservation`` is what the call reserved at admission (0 when admitted
    without a budget); ``reserved`` is what it still holds in flight (0 once
    settled). After settlement ``known`` / ``unsettled`` / ``unbounded`` say
    what it charges (``agenthub.budget.settle``); a later, better ledger row
    re-settles it by the difference.
    """

    __tablename__ = "token_budget_items"

    invocation_id = Column(ForeignKey("invocations.id"), primary_key=True)
    root_task_id = Column(String, index=True, nullable=False)
    estimate = Column(Integer, nullable=False, default=0)
    reservation = Column(Integer, nullable=False, default=0)
    reserved = Column(Integer, nullable=False, default=0)
    known = Column(Integer, nullable=False, default=0)
    unsettled = Column(Integer, nullable=False, default=0)
    unbounded = Column(Integer, nullable=False, default=0)
    overrun = Column(Boolean, nullable=False, default=False)
    state = Column(String, nullable=False, default="reserved")
    mode = Column(String, nullable=True)
    warnings = Column(JSON, nullable=False, default=list)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)


class ConversationEventCursorRow(Base):
    """Per-conversation sequence counter.

    Allocating with ``UPDATE ... SET last_seq = last_seq + n`` takes the write
    lock (SQLite) or the row lock (PostgreSQL) before the number is chosen, so
    writers of one conversation commit in seq order and a reader that sees
    seq N has already been able to see every seq below N.
    """

    __tablename__ = "conversation_event_cursors"

    conversation_id = Column(ForeignKey("conversations.id"), primary_key=True)
    last_seq = Column(Integer, nullable=False, default=0)


class Database:
    """Thin wrapper around an engine + sessionmaker."""

    def __init__(self, url: str = "sqlite:///:memory:") -> None:
        self.url = url
        engine_kwargs: dict[str, Any] = {"future": True}
        self._is_file_sqlite = url.startswith("sqlite") and ":memory:" not in url
        if url.startswith("sqlite"):
            # timeout = how long a writer waits for a competing lock instead of
            # immediately raising "database is locked". Needed for real
            # concurrent workers on a file database.
            engine_kwargs["connect_args"] = {
                "check_same_thread": False,
                "timeout": 30,
            }
            if ":memory:" in url:
                # An in-memory DB otherwise gives every connection its own
                # private database, so rows committed in one session vanish in
                # the next. StaticPool shares a single connection; note this
                # also means true multi-connection concurrency cannot be
                # exercised against :memory: (use a file DB for that).
                engine_kwargs["poolclass"] = StaticPool
        self.engine: Engine = create_engine(url, **engine_kwargs)
        if self._is_file_sqlite:
            self._enable_wal()
        self._sessionmaker = sessionmaker(
            self.engine, expire_on_commit=False, class_=Session,
            info={"agenthub_db": self},
        )
        # Conversation events are recorded from the flush itself; see events.py.
        self.events_ready = False
        from agenthub_kernel.events import install_event_capture

        install_event_capture(self._sessionmaker)

    def _enable_wal(self) -> None:
        """Enable WAL + a busy timeout so concurrent workers can interleave.

        WAL lets readers proceed while a writer holds the write lock, and
        ``busy_timeout`` makes a second writer wait rather than fail instantly.
        """
        from sqlalchemy import text

        with self.engine.begin() as conn:
            conn.execute(text("PRAGMA journal_mode=WAL"))
            conn.execute(text("PRAGMA busy_timeout=30000"))
            conn.execute(text("PRAGMA synchronous=NORMAL"))

    def create_all(self) -> None:
        Base.metadata.create_all(self.engine)
        self.events_ready = True

    @contextmanager
    def session(self) -> Iterator[Session]:
        with self._sessionmaker() as session:
            try:
                yield session
                session.commit()
            except Exception:
                session.rollback()
                raise


def make_database(url: str = "sqlite:///:memory:") -> Database:
    return Database(url)
