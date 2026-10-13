"""Mock adapter used to exercise the approval & quota kernel.

The Mock adapter does nothing real; it only records that it was *started*.
Tests assert on ``start_count`` to prove that an invocation which was not
approved (or was expired / revoked / denied / snapshot-changed) never reaches
the adapter.

It also simulates a stable external id so the worker can track an invocation
whose real status is unknown.
"""

from __future__ import annotations

import itertools
import threading
from typing import Any

from agenthub.contracts import (
    AdapterContract,
    AdapterStartResult,
    InvocationSnapshot,
)


class MockAdapter(AdapterContract):
    """In-memory adapter that counts how many times it was asked to start."""

    name = "mock"
    version = "0.1.0"

    def __init__(self, fail_on_start: bool = False) -> None:
        self._lock = threading.Lock()
        self._start_count = 0
        self._adapter_ids: dict[str, str] = {}
        self._counter = itertools.count(1)
        self._fail_on_start = fail_on_start

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
            if self._fail_on_start:
                return AdapterStartResult(
                    adapter_invocation_id=adapter_id, ok=False, detail="mock failure"
                )
            return AdapterStartResult(adapter_invocation_id=adapter_id, ok=True)

    def cancel(self, adapter_invocation_id: str) -> Any:
        return {"ok": True, "adapter_invocation_id": adapter_invocation_id}

    def get_status(self, adapter_invocation_id: str) -> Any:
        return {"ok": True, "status": "running", "adapter_invocation_id": adapter_invocation_id}
