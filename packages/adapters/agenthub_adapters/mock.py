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

import itertools
import threading
from typing import Any, Optional

from agenthub.contracts import (
    AdapterContract,
    AdapterStartResult,
    ExecutionAdmission,
    InvocationSnapshot,
)

from agenthub_adapters.store import (
    CANCELLED as MOCK_CANCELLED,
    FAILED as MOCK_FAILED,
    FINISHED as MOCK_FINISHED,
    RUNNING as MOCK_RUNNING,
    ExecutionStore,
)


class MockExecutionStore(ExecutionStore):
    """Persisted record of what the Mock "external agent" was asked to run.

    Keyed by `invocation_id` so recovery can ask, per invocation, whether the
    call ever reached the adapter and what happened to it. Shares the generic
    implementation; only the table name differs.
    """

    TABLE = "mock_executions"

    def record_start(self, invocation_id: str, adapter_invocation_id: str) -> None:
        super().record_start(invocation_id, adapter_invocation_id)
        # The Mock has always exposed the raw row shape; keep that.
        return None


class MockAdapter(AdapterContract):
    """In-memory adapter that counts how many times it was asked to start.

    ``start_count`` is the assertion surface for "unapproved calls never reach
    the adapter". ``store`` additionally persists per-invocation records so
    recovery tests can distinguish "started" from "never started".
    """

    name = "mock"
    version = "0.2.0"
    #: The inert Mock stands for an external service that outlives this process,
    #: so its "running" record is a real confirmation in restart recovery. Real
    #: adapters run a child process or HTTP stream that ends with the Hub.
    execution_survives_restart = True

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

    def execution_admission(self) -> ExecutionAdmission:
        return ExecutionAdmission(True, "simulation", "mock_only", "Mock 仅模拟执行，不调用真实 Agent。")

    def start(
        self,
        invocation_id: str,
        snapshot: InvocationSnapshot,
        *,
        gate_token: Optional[str] = None,
    ) -> AdapterStartResult:
        """Record a start. The Mock is inert, so it accepts but ignores the token."""
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
                adapter_invocation_id=adapter_id,
                ok=False,
                terminal=True,
                detail="mock failure",
            )
        # terminal=False: the Mock simulates a call that keeps running until the
        # kernel releases it, which is what the concurrency tests rely on. A real
        # adapter that completes synchronously reports terminal=True instead.
        return AdapterStartResult(
            adapter_invocation_id=adapter_id, ok=True, terminal=False
        )

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
