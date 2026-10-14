"""Persisted execution records shared by adapters.

An adapter's execution record models **the external side's own view** of a run.
The kernel needs it for two things:

* recovery - "did this call actually reach the adapter, and is it still going?";
* stop confirmation - only a terminal external status (or the positive absence of
  any record for a call that never started) lets a quota slot be released.

Records are keyed by ``invocation_id``. The store deliberately uses its own
sqlite connection rather than the kernel's SQLAlchemy metadata, so it stays a
separate concern from the kernel's own tables.

The table name is parameterised so each adapter keeps its own log
(``mock_executions``, ``codex_executions``) while sharing the implementation.
"""

from __future__ import annotations

import datetime as dt
import os
import sqlite3
import threading
from typing import Any, Optional

#: External-side run states understood by the kernel's stop-confirmation logic.
RUNNING = "running"
FINISHED = "finished"
FAILED = "failed"
CANCELLED = "cancelled"

TERMINAL_STATUSES = frozenset({FINISHED, FAILED, CANCELLED})


class ExecutionStore:
    """SQLite-backed per-invocation execution log."""

    TABLE = "adapter_executions"

    #: The table name is interpolated into SQL, so it is validated rather than
    #: trusted. Only plain identifiers are accepted.
    _NAME_RE = __import__("re").compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

    def __init__(self, path: str = ":memory:", table: Optional[str] = None) -> None:
        self.path = path
        if table is not None:
            if not self._NAME_RE.match(table):
                raise ValueError(f"unsafe table name: {table!r}")
            self.TABLE = table
        self._lock = threading.Lock()
        self._init()

    # -- connection helpers -------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        if self.path == ":memory:":
            # One shared in-memory database so several connections agree.
            if not hasattr(self, "_mem_conn"):
                self._mem_conn = sqlite3.connect(
                    f"file:{self.TABLE}?mode=memory&cache=shared",
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
                f"""
                CREATE TABLE IF NOT EXISTS {self.TABLE} (
                    invocation_id         TEXT PRIMARY KEY,
                    adapter_invocation_id TEXT,
                    status                TEXT NOT NULL,
                    detail                TEXT,
                    payload               TEXT,
                    started_at            TEXT,
                    updated_at            TEXT
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

    def record_start(
        self, invocation_id: str, adapter_invocation_id: str, detail: str = ""
    ) -> None:
        with self._lock:
            conn = self._connect()
            conn.execute(
                f"""
                INSERT INTO {self.TABLE}
                    (invocation_id, adapter_invocation_id, status, detail, started_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(invocation_id) DO UPDATE SET
                    adapter_invocation_id=excluded.adapter_invocation_id,
                    status=excluded.status,
                    updated_at=excluded.updated_at
                """,
                (invocation_id, adapter_invocation_id, RUNNING, detail, self._ts(), self._ts()),
            )
            conn.commit()
            if self.path != ":memory:":
                conn.close()

    def set_status(self, invocation_id: str, status: str, detail: str = "") -> None:
        with self._lock:
            conn = self._connect()
            conn.execute(
                f"UPDATE {self.TABLE} SET status=?, detail=?, updated_at=? WHERE invocation_id=?",
                (status, detail, self._ts(), invocation_id),
            )
            conn.commit()
            if self.path != ":memory:":
                conn.close()

    def set_adapter_id(self, invocation_id: str, adapter_invocation_id: str) -> None:
        """Fill in the external execution id once the adapter knows it.

        ``record_start`` runs BEFORE the process is launched, when the external id
        does not exist yet (it is usually the thread id the process reports later).
        Without this update the adapter's own record kept an empty
        ``adapter_invocation_id``, so anyone reading only the adapter's store -
        recovery, a stop check, an operator - could not tell WHICH external
        execution a record refers to, even though the kernel row had it.
        """
        if not adapter_invocation_id:
            return
        with self._lock:
            conn = self._connect()
            conn.execute(
                f"UPDATE {self.TABLE} SET adapter_invocation_id=?, updated_at=? "
                "WHERE invocation_id=?",
                (adapter_invocation_id, self._ts(), invocation_id),
            )
            conn.commit()
            if self.path != ":memory:":
                conn.close()

    def set_payload(self, invocation_id: str, payload: Any) -> None:
        """Store adapter-specific results (parsed events, thread id, usage)."""
        import json

        with self._lock:
            conn = self._connect()
            conn.execute(
                f"UPDATE {self.TABLE} SET payload=?, updated_at=? WHERE invocation_id=?",
                (json.dumps(payload, ensure_ascii=False), self._ts(), invocation_id),
            )
            conn.commit()
            if self.path != ":memory:":
                conn.close()

    def get(self, invocation_id: str) -> Optional[dict[str, Any]]:
        with self._lock:
            conn = self._connect()
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                f"SELECT * FROM {self.TABLE} WHERE invocation_id=?", (invocation_id,)
            ).fetchone()
            if self.path != ":memory:":
                conn.close()
            if row is None:
                return None
            rec = dict(row)
        if rec.get("payload"):
            import json

            try:
                rec["payload"] = json.loads(rec["payload"])
            except (ValueError, TypeError):
                pass
        return rec

    def all_records(self) -> list[dict[str, Any]]:
        with self._lock:
            conn = self._connect()
            conn.row_factory = sqlite3.Row
            rows = conn.execute(f"SELECT * FROM {self.TABLE}").fetchall()
            if self.path != ":memory:":
                conn.close()
            return [dict(r) for r in rows]

    def delete(self, invocation_id: str) -> int:
        """Remove one record. TEST/MOCK-ONLY.

        Lets tests reproduce "the external side has no record for a call the
        kernel knows it started" - lost evidence, which must NOT be read as proof
        that nothing is running. A real adapter would not offer this.
        """
        with self._lock:
            conn = self._connect()
            cur = conn.execute(
                f"DELETE FROM {self.TABLE} WHERE invocation_id=?", (invocation_id,)
            )
            conn.commit()
            if self.path != ":memory:":
                conn.close()
            return cur.rowcount


def store_path_for(db_url: str, suffix: str = "adapter.db") -> str:
    """Pick a store path that sits beside the kernel DB when it is file-backed."""
    if db_url.startswith("sqlite:///") and ":memory:" not in db_url:
        return db_url.replace("sqlite:///", "", 1)
    return ":memory:"


def ensure_parent(path: str) -> str:
    if path != ":memory:":
        parent = os.path.dirname(os.path.abspath(path))
        os.makedirs(parent, exist_ok=True)
    return path
