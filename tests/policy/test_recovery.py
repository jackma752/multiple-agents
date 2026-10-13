"""Crash recovery (P2 item 6, part B).

Acceptance:
* recovery reconciles ``starting`` / ``running`` rows against the external side
  (the Mock adapter's persisted execution records, keyed by invocation_id);
* a call that cannot be confirmed becomes ``unknown`` — it is NOT restarted and
  its quota slot is NOT released early;
* a call confirmed still running stays ``running`` and keeps its slot;
* a call confirmed terminal is settled and its slot freed exactly once;
* recovery never silently restarts anything, and running it twice is a no-op.

Crash windows covered:
  A. after the consume commit, BEFORE adapter.start
  B. after adapter.start, BEFORE the kernel marks the call running
  C. after adapter.start, external side reports a terminal outcome
"""

from __future__ import annotations

import pytest
from sqlalchemy import func, select

from agenthub.contracts import InvocationStatus, Policy
from agenthub_kernel.db import (
    InvocationRow,
    QuotaReservationRow,
    TreeQuotaRow,
)
from agenthub_kernel.service import PolicyService
from tests.helpers import KernelHarness


def _approved(h, goal="sub"):
    root = h.create_root()
    sub = h.create_subtask(root.id, goal)
    inv = h.create_invocation(sub.id)
    appr = h.create_approval(inv.id)
    h.approve(appr.id)
    return root, sub, inv, appr


def _consume_only(h, inv_id):
    """Reach STARTING + reserved slot WITHOUT calling the adapter (window A)."""
    with h.db.session() as s:
        PolicyService(s, h.policy).consume(inv_id)
        s.commit()


def _consume_and_start(h, inv_id):
    """Consume AND start the adapter, but never mark the call running (window B)."""
    with h.db.session() as s:
        plan = PolicyService(s, h.policy).consume(inv_id)
        s.commit()
    from agenthub.contracts import InvocationSnapshot

    h.adapter.start(inv_id, InvocationSnapshot(**plan["snapshot"]))


def _quota(h, root_task_id):
    with h.open_session() as s:
        return s.get(TreeQuotaRow, root_task_id)


# --------------------------------------------------------------------------- #
# window A: crashed before start
# --------------------------------------------------------------------------- #


def test_crash_before_start_becomes_unknown_without_restart(tmp_path):
    url = f"sqlite:///{(tmp_path / 'recA.db').as_posix()}"
    h = KernelHarness(url=url)
    root, _sub, inv, _appr = _approved(h)
    _consume_only(h, inv.id)

    # Simulate restart: fresh worker + fresh adapter over the same store.
    worker2, adapter2 = h.new_worker()
    summary = worker2.recover()

    assert summary["examined"] == 1
    assert summary["unknown"] == [inv.id]
    assert summary["confirmed_running"] == []
    assert summary["restarted"] == []

    # NOT restarted.
    assert adapter2.start_count == 0

    with h.open_session() as s:
        assert s.get(InvocationRow, inv.id).status == InvocationStatus.UNKNOWN.value

    # The slot is NOT released early: we cannot prove the call is not running.
    q = _quota(h, root.root_task_id)
    assert q.concurrent == 1, "unknown call released its slot prematurely"
    with h.open_session() as s:
        unreleased = s.scalar(
            select(func.count())
            .select_from(QuotaReservationRow)
            .where(QuotaReservationRow.released_at.is_(None))
        )
        assert unreleased == 1


# --------------------------------------------------------------------------- #
# window B: crashed after start, before marking running
# --------------------------------------------------------------------------- #


def test_crash_after_start_confirms_running_and_holds_slot(tmp_path):
    url = f"sqlite:///{(tmp_path / 'recB.db').as_posix()}"
    h = KernelHarness(url=url)
    root, _sub, inv, _appr = _approved(h)
    _consume_and_start(h, inv.id)

    assert h.adapter.lookup_by_invocation(inv.id)["status"] == "running"

    worker2, adapter2 = h.new_worker()
    summary = worker2.recover()

    assert summary["examined"] == 1
    assert summary["confirmed_running"] == [inv.id]
    assert summary["unknown"] == []
    assert summary["restarted"] == []

    # Confirmed running: NOT restarted, and it keeps its slot.
    assert adapter2.start_count == 0
    with h.open_session() as s:
        row = s.get(InvocationRow, inv.id)
        assert row.status == InvocationStatus.RUNNING.value
        assert row.adapter_invocation_id, "adapter id should be recovered"
    assert _quota(h, root.root_task_id).concurrent == 1


# --------------------------------------------------------------------------- #
# window C: crashed after start, external side is terminal
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "external,expected",
    [
        ("finished", InvocationStatus.SUCCEEDED),
        ("failed", InvocationStatus.FAILED),
        ("cancelled", InvocationStatus.CANCELED),
    ],
)
def test_crash_after_start_settles_confirmed_terminal(tmp_path, external, expected):
    url = f"sqlite:///{(tmp_path / f'recC_{external}.db').as_posix()}"
    h = KernelHarness(url=url)
    root, _sub, inv, _appr = _approved(h)
    _consume_and_start(h, inv.id)
    h.adapter.store.set_status(inv.id, external)

    worker2, adapter2 = h.new_worker()
    summary = worker2.recover()

    assert summary["settled"] == [{"invocation_id": inv.id, "status": expected.value}]
    assert summary["restarted"] == []
    assert adapter2.start_count == 0

    with h.open_session() as s:
        assert s.get(InvocationRow, inv.id).status == expected.value
    # Terminal and confirmed: the slot is freed, and only the concurrent counter.
    q = _quota(h, root.root_task_id)
    assert q.concurrent == 0
    assert q.launched_total == 1, "cumulative total must survive recovery"


def test_unrecognised_external_state_becomes_unknown(tmp_path):
    url = f"sqlite:///{(tmp_path / 'recU.db').as_posix()}"
    h = KernelHarness(url=url)
    root, _sub, inv, _appr = _approved(h)
    _consume_and_start(h, inv.id)
    h.adapter.store.set_status(inv.id, "some-future-state")

    worker2, _adapter2 = h.new_worker()
    summary = worker2.recover()

    assert summary["unknown"] == [inv.id]
    assert summary["settled"] == []
    with h.open_session() as s:
        assert s.get(InvocationRow, inv.id).status == InvocationStatus.UNKNOWN.value
    assert _quota(h, root.root_task_id).concurrent == 1


# --------------------------------------------------------------------------- #
# idempotence and no-op behaviour
# --------------------------------------------------------------------------- #


def test_recovery_is_idempotent_and_never_restarts(tmp_path):
    url = f"sqlite:///{(tmp_path / 'recIdem.db').as_posix()}"
    h = KernelHarness(policy=Policy(max_subagents_total=5, max_subagents_concurrent=3), url=url)

    # Both calls must live in the SAME root tree so the quota counters are shared.
    root = h.create_root()

    sub_a = h.create_subtask(root.id, "a")
    inv_a = h.create_invocation(sub_a.id)
    appr_a = h.create_approval(inv_a.id)
    h.approve(appr_a.id)
    _consume_only(h, inv_a.id)  # window A -> no adapter record

    sub_b = h.create_subtask(root.id, "b")
    inv_b = h.create_invocation(sub_b.id)
    appr_b = h.create_approval(inv_b.id)
    h.approve(appr_b.id)
    _consume_and_start(h, inv_b.id)  # window B -> adapter record present

    worker2, adapter2 = h.new_worker()
    first = worker2.recover()
    assert first["examined"] == 2
    assert first["unknown"] == [inv_a.id]
    assert first["confirmed_running"] == [inv_b.id]

    # Second pass: nothing left to settle, and still nothing restarted.
    second = worker2.recover()
    assert second["settled"] == []
    assert second["restarted"] == []
    assert adapter2.start_count == 0

    q = _quota(h, root.root_task_id)
    # Both slots still held: one unknown (deliberately) + one running.
    assert q.concurrent == 2
    assert q.launched_total == 2


def test_recovery_does_not_touch_settled_invocations(tmp_path):
    url = f"sqlite:///{(tmp_path / 'recSettled.db').as_posix()}"
    h = KernelHarness(url=url)
    root = h.create_root()
    sub = h.create_subtask(root.id, "done")
    inv = h.create_invocation(sub.id)
    appr = h.create_approval(inv.id)
    h.approve(appr.id)
    h.drain()  # RUNNING

    with h.open_session() as s:
        PolicyService(s, h.policy).release(inv.id, InvocationStatus.SUCCEEDED)
        s.commit()

    worker2, adapter2 = h.new_worker()
    summary = worker2.recover()

    assert summary["examined"] == 0
    assert adapter2.start_count == 0
    with h.open_session() as s:
        assert s.get(InvocationRow, inv.id).status == InvocationStatus.SUCCEEDED.value
    assert _quota(h, root.root_task_id).concurrent == 0
