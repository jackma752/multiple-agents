"""Atomicity tests for the execution gate (plan §4.1).

Acceptance:
* the approval is consumed and the quota slot reserved in a transaction that is
  committed BEFORE the adapter starts; a failing commit must not start it;
* two workers racing on the same approval start the adapter at most once.

The concurrency test runs against a **file-based SQLite** with independent
connections, because an in-memory database shares a single connection and
therefore cannot express real concurrency.
"""

from __future__ import annotations

import threading
from contextlib import contextmanager

import pytest
from sqlalchemy import func, select

from agenthub.contracts import InvocationStatus, Policy
from agenthub_kernel.db import ApprovalRow, QuotaReservationRow
from agenthub_kernel.service import PolicyService, QuotaError
from tests.helpers import KernelHarness


def _approved_invocation(h):
    root = h.create_root()
    inv = h.create_invocation(root.id)
    appr = h.create_approval(inv.id)
    h.approve(appr.id)
    return root, inv, appr


def test_adapter_starts_only_after_consume_is_committed(tmp_path):
    """If the consuming commit fails, the adapter must never start.

    Failure is injected at the real commit point: ``Database.session`` commits
    when its context exits, so we make that exit raise.
    """
    h = KernelHarness(url=f"sqlite:///{tmp_path / 'atomic.db'}")
    _root, inv, _appr = _approved_invocation(h)

    real_session = h.db.session

    @contextmanager
    def failing_session():
        with real_session() as session:
            yield session
            # Simulate the COMMIT failing after consume() already succeeded.
            raise RuntimeError("simulated commit failure")

    h.worker.db.session = failing_session  # type: ignore[assignment]

    with pytest.raises(RuntimeError):
        h.worker.drain_once()

    # The adapter was never started, even though consume() itself succeeded.
    assert h.adapter.start_count == 0


def test_concurrent_consume_starts_at_most_once(tmp_path):
    """Two threads consuming one approval: at most one succeeds.

    Uses a file-backed SQLite so each session gets its own connection and the
    compare-and-swap in ``consume`` is exercised for real.
    """
    db_file = tmp_path / "concurrency.db"
    h = KernelHarness(url=f"sqlite:///{db_file}")
    _root, inv, appr = _approved_invocation(h)

    results: list[str] = []
    lock = threading.Lock()
    barrier = threading.Barrier(2)

    def consume_once(label: str) -> None:
        try:
            with h.db.session() as session:
                svc = PolicyService(session, h.policy)
                barrier.wait(timeout=10)  # maximise the race window
                svc.consume(inv.id)
                session.commit()
            outcome = f"{label}:ok"
        except QuotaError as exc:
            outcome = f"{label}:{exc.code}"
        except Exception as exc:  # pragma: no cover - diagnostic
            outcome = f"{label}:err:{type(exc).__name__}:{exc}"
        with lock:
            results.append(outcome)

    threads = [threading.Thread(target=consume_once, args=(f"w{i}",)) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    successes = [r for r in results if r.endswith(":ok")]
    assert len(successes) == 1, results

    with h.open_session() as s:
        row = s.get(ApprovalRow, appr.id)
        assert row.status == "consumed"
        n_res = s.scalar(select(func.count()).select_from(QuotaReservationRow))
        assert n_res == 1


def test_release_does_not_return_cumulative_total():
    """A finished sub-agent frees the concurrent slot but not the total budget."""
    h = KernelHarness(policy=Policy(max_subagents_total=1, max_subagents_concurrent=1))
    root = h.create_root()

    sub = h.create_subtask(root.id, "sub")
    inv = h.create_invocation(sub.id)
    appr = h.create_approval(inv.id)
    h.approve(appr.id)
    h.drain()
    assert h.adapter.start_count == 1

    with h.open_session() as s:
        PolicyService(s, h.policy).release(inv.id, InvocationStatus.SUCCEEDED)
        s.commit()

    sub2 = h.create_subtask(root.id, "sub2")
    inv2 = h.create_invocation(sub2.id)
    appr2 = h.create_approval(inv2.id)
    h.approve(appr2.id)
    h.drain()

    assert h.adapter.start_count == 1  # total cap (1) still blocks a second
