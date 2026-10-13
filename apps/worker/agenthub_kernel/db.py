"""SQLAlchemy persistence for the Agent Hub kernel.

Uses SQLite in the P2 Mock deployment (per the confirmed stack). The schema is
kept intentionally small and mirrors AGENT_HUB_PLAN §5's table list:
``agents, projects, conversations, tasks, invocations, approvals,
policy_versions, quota_reservations, events, audit_logs``.

Only the tables needed for the approval & quota kernel are implemented here:
``tasks`` (the root-task tree), ``invocations``, ``approvals``,
``policy_versions``, ``events``, ``quota_reservations``.
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


class EventRow(Base):
    """Streamed / audited event. ``seq`` is the per-tree ordering."""

    __tablename__ = "events"

    id = Column(Integer, primary_key=True, autoincrement=True)
    root_task_id = Column(String, index=True, nullable=False)
    seq = Column(Integer, nullable=False)
    type = Column(String, nullable=False)
    payload = Column(JSON, nullable=False, default=dict)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)


class Database:
    """Thin wrapper around an engine + sessionmaker."""

    def __init__(self, url: str = "sqlite:///:memory:") -> None:
        self.url = url
        engine_kwargs: dict[str, Any] = {"future": True}
        if url.startswith("sqlite"):
            engine_kwargs["connect_args"] = {"check_same_thread": False}
            # For an in-memory database every connection otherwise gets its own
            # private DB, so rows committed in one session are invisible in the
            # next. StaticPool shares a single connection across all sessions.
            if ":memory:" in url:
                engine_kwargs["poolclass"] = StaticPool
        self.engine: Engine = create_engine(url, **engine_kwargs)
        self._sessionmaker = sessionmaker(
            self.engine, expire_on_commit=False, class_=Session
        )

    def create_all(self) -> None:
        Base.metadata.create_all(self.engine)

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
