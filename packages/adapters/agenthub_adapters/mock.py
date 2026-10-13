"""Mock adapter used to exercise the approval & quota kernel.

The Mock adapter does nothing real; it only records that it was *started*.
Tests assert on ``start_count`` to prove that an invocation which was not
approved (or was expired / revoked / denied / snapshot-changed) never reaches
the adapter.

It also simulates the *external system's own state*: a standalone SQLite table
keyed by ``invocation_id``. That matters for crash recovery, because after a
kernel crash the only way to tell "did this call actually start?" is to ask the
external side. The store is deliberately independent of the kernel's SQLAlchemy
metadata (its own table, its own connection) to model that boundary honestly.
"""

from __future__ import annotations

import datetime as dt
import itertools
import os
import sqlite3
import threading
from typing import Any, Optional

from agenthub.contracts import (
    AdapterContract,
    AdapterStartResult,
    InvocationSnapshot,
)

#: External-side run states.
MOCK_RUNNING = "running"
MOCK_FINISHED = "finished"
MOCK_FAILED = "failed"
MOCK_CANCELLED = "cancelled"


class MockExecutionStore:
    """Persisted record of what the Mock "external agent" was asked to run.

    Keyed by ``invocation_id`` so recovery can ask, per invocation, whether the
    call ever reached the adapter and what happened to it.
    """

    def __init__(self, path: str = ":memory:") -> None:
        self.path = path
        self._lock = threading.Lock()
        # A shared-cache in-memory DB so several connections see one store.
        self._uri = path == ":memory:" or path.startswith("file:")
        self._init()

    # -- connection helpers -------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        if self.path == ":memory:":
            # Keep one connection alive for in-memory mode.
            if not hasattr(self, "_mem_conn"):
                self._mem_conn = sqlite3.connect(
                    "file:mockexec?mode=memory&cache=shared",
                    uri=True,
                    check_same_thread=False,
                )
                self._mem_conn.execute("PRAGMA busy_timeout=30000")
            return self._mem_conn
        conn = sqlite3.connect(self.path, timeout=30, check_same_thread=False)
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    def _init(self) -> None:
        with self._lock:
            conn = self._connect()
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS mock_executions (
                    invocation_id       TEXT PRIMARY KEY,
                    adapter_invocation_id TEXT,
                    status              TEXT NOT NULL,
                    detail              TEXT,
                    started_at          TEXT,
                    updated_at          TEXT
                )
                """
            )
            conn.commit()
            if self.path != ":memory:":
                conn.close()

    # -- api ----------------------------------------------------------------

    @staticmethod
    def _ts() -> str:
        return dt.datetime.now(dt.timezone.utc).isoformat()

    def record_start(self, invocation_id: str, adapter_invocation_id: str) -> None:
        with self._lock:
            conn = self._connect()
            conn.execute(
                """
                INSERT INTO mock_executions
                    (invocation_id, adapter_invocation_id, status, started_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(invocation_id) DO UPDATE SET
                    adapter_invocation_id=excluded.adapter_invocation_id,
                    status=excluded.status,
                    updated_at=excluded.updated_at
                """,
                (invocation_id, adapter_invocation_id, MOCK_RUNNING, self._ts(), self._ts()),
            )
            conn.commit()
            if self.path != ":memory:":
                conn.close()

    def set_status(self, invocation_id: str, status: str, detail: str = "") -> None:
        with self._lock:
            conn = self._connect()
            conn.execute(
                "UPDATE mock_executions SET status=?, detail=?, updated_at=? "
                "WHERE invocation_id=?",
                (status, detail, self._ts(), invocation_id),
            )
            conn.commit()
            if self.path != ":memory:":
                conn.close()

    def get(self, invocation_id: str) -> Optional[dict[str, Any]]:
        with self._lock:
            conn = self._connect()
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM mock_executions WHERE invocation_id=?",
                (invocation_id,),
            ).fetchone()
            if self.path != ":memory:":
                conn.close()
            return dict(row) if row is not None else None

    def all_records(self) -> list[dict[str, Any]]:
        with self._lock:
            conn = self._connect()
            conn.row_factory = sqlite3.Row
            rows = conn.execute("SELECT * FROM mock_executions").fetchall()
            if self.path != ":memory:":
                conn.close()
            return [dict(r) for r in rows]


class MockAdapter(AdapterContract):
    """In-memory adapter that counts how many times it was asked to start.

    ``start_count`` is the assertion surface for "unapproved calls never reach
    the adapter". ``store`` additionally persists per-invocation records so
    recovery tests can distinguish "started" from "never started".
    """

    name = "mock"
    version = "0.2.0"

    def __init__(
        self,
        fail_on_start: bool = False,
        store: Optional[MockExecutionStore] = None,
        store_path: str = ":memory:",
    ) -> None:
        self._lock = threading.Lock()
        self._start_count = 0
        self._adapter_ids: dict[str, str] = {}
        self._counter = itertools.count(1)
        self._fail_on_start = fail_on_start
        self.store = store or MockExecutionStore(store_path)

    @property
    def start_count(self) -> int:
        with self._lock:
            return self._start_count

    @property
    def started_invocations(self) -> set[str]:
        with self._lock:
            return set(self._adapter_ids)

    def capabilities(self) -> dict[str, Any]:
        return {
            "post_message": True,
            "stream": True,
            "resume_session": True,
            "cancel": True,
            "limit_dir": True,
            "intercept_tool": True,
            "control_subagent": True,
            "real_usage": False,
        }

    def health(self) -> bool:
        return True

    def start(self, invocation_id: str, snapshot: InvocationSnapshot) -> AdapterStartResult:
        with self._lock:
            self._start_count += 1
            adapter_id = f"mock-{next(self._counter)}-{invocation_id}"
            self._adapter_ids[invocation_id] = adapter_id
        # Persist the external-side record BEFORE returning, so a crash right
        # after start() still leaves evidence that the call began.
        self.store.record_start(invocation_id, adapter_id)
        if self._fail_on_start:
            self.store.set_status(invocation_id, MOCK_FAILED, "mock failure")
            return AdapterStartResult(
                adapter_invocation_id=adapter_id, ok=False, detail="mock failure"
            )
        return AdapterStartResult(adapter_invocation_id=adapter_id, ok=True)

    def cancel(self, adapter_invocation_id: str) -> Any:
        # Unsupported operations must say so rather than pretending; the Mock
        # does support cancel, and records it on the external side.
        self.store.set_status(adapter_invocation_id, MOCK_CANCELLED, "cancelled")
        return {"ok": True, "adapter_invocation_id": adapter_invocation_id}

    def cancel_invocation(self, invocation_id: str) -> dict[str, Any]:
        """Cancel by invocation_id (what the Worker knows)."""
        record = self.store.get(invocation_id)
        if record is None:
            return {"ok": False, "reason": "unknown_invocation"}
        self.store.set_status(invocation_id, MOCK_CANCELLED, "cancelled by user")
        return {"ok": True, "adapter_invocation_id": record.get("adapter_invocation_id")}

    def get_status(self, adapter_invocation_id: str) -> Any:
        return {
            "ok": True,
            "status": "running",
            "adapter_invocation_id": adapter_invocation_id,
        }

    def lookup_by_invocation(self, invocation_id: str) -> Optional[dict[str, Any]]:
        """Recovery hook: what does the external side know about this call?"""
        return self.store.get(invocation_id)

    def find_invocation_for_adapter_id(self, adapter_invocation_id: str) -> Optional[str]:
        for rec in self.store.all_records():
            if rec.get("adapter_invocation_id") == adapter_invocation_id:
                return str(rec["invocation_id"])
        return None
