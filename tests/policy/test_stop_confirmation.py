"""Stop confirmation and quota safety (P2 closeout, second correction).

The rule being enforced: a quota slot may only be freed once we can CONFIRM the
previous execution is no longer running. Two consequences, both tested here:

1. With ``max_subagents_concurrent=1``, if the first call's cancellation is
   unsupported or its outcome is unknown, a SECOND already-approved call must
   still not start — the first call may still be occupying the only slot.
2. Only after the old execution is confirmed stopped is the slot released
   (exactly once), which is what permits a new call to run.
"""

from __future__ import annotations

from sqlalchemy import func, select

import pytest

from agenthub.contracts import InvocationStatus, Policy
from agenthub_kernel.db import (
    ApprovalRow,
    InvocationRow,
    QuotaReservationRow,
    TreeQuotaRow,
)
from agenthub_kernel.service import PolicyService, QuotaError
from tests.helpers import KernelHarness


def _quota(h, root_id):
    with h.open_session() as s:
        return s.get(TreeQuotaRow, root_id)


def _second_approved(h, root, goal="second"):
    sub = h.create_subtask(root.id, goal)
    inv = h.create_invocation(sub.id)
    appr = h.create_approval(inv.id)
    h.approve(appr.id)
    return sub, inv, appr


def _one_slot_tree(tmp_path, name, limit=60):
    """A tree whose concurrent cap is exactly 1, with one sub-agent running."""
    url = f"sqlite:///{(tmp_path / name).as_posix()}"
    h = KernelHarness(
        policy=Policy(collaboration_enabled=True,
            max_subagents_total=5,
            max_subagents_concurrent=1,
            run_time_limit_seconds=limit,
        ),
        url=url,
    )
    root = h.create_root()
    sub = h.create_subtask(root.id, "first")
    inv = h.create_invocation(sub.id)
    appr = h.create_approval(inv.id)
    h.approve(appr.id)
    h.drain()
    assert h.adapter.start_count == 1
    return h, root, inv


# --------------------------------------------------------------------------- #
# 1. unconfirmed stop must not open the slot to a second call
# --------------------------------------------------------------------------- #


def test_second_approved_call_blocked_when_cancel_unsupported(tmp_path):
    """Adapter cannot cancel -> first call becomes unknown -> slot stays held."""
    h, root, first_inv = _one_slot_tree(tmp_path, "cap1_unsupported.db")
    assert _quota(h, root.root_task_id).concurrent == 1

    h.mark_running_old(first_inv.id, seconds=600)

    class NoCancelAdapter:
        def lookup_by_invocation(self, invocation_id):
            return None

    h.worker.adapter = NoCancelAdapter()  # type: ignore[assignment]
    h.worker.reap_timeouts()

    with h.open_session() as s:
        assert s.get(InvocationRow, first_inv.id).status == InvocationStatus.UNKNOWN.value

    # A second approved call must NOT start: the only slot is still held.
    _sub, second_inv, _appr = _second_approved(h, root)
    h.worker.drain_once()

    assert h.adapter.start_count == 1, "second call started while the slot was still held"
    with h.open_session() as s:
        assert s.get(InvocationRow, second_inv.id).status == InvocationStatus.QUEUED.value
    assert _quota(h, root.root_task_id).concurrent == 1
    # Only one reservation exists (the held one).
    with h.open_session() as s:
        unreleased = s.scalar(
            select(func.count())
            .select_from(QuotaReservationRow)
            .where(QuotaReservationRow.released_at.is_(None))
        )
        assert unreleased == 1


def test_second_approved_call_blocked_when_outcome_unknown(tmp_path):
    """External record still says running -> unknown -> slot stays held."""
    h, root, first_inv = _one_slot_tree(tmp_path, "cap1_running.db")
    h.mark_running_old(first_inv.id, seconds=600)

    class StuckAdapter:
        def cancel_invocation(self, invocation_id):
            return {"ok": False, "reason": "timeout"}

        def lookup_by_invocation(self, invocation_id):
            return {"status": "running", "adapter_invocation_id": "a1"}

    h.worker.adapter = StuckAdapter()  # type: ignore[assignment]
    h.worker.reap_timeouts()

    _sub, second_inv, _appr = _second_approved(h, root)
    h.worker.drain_once()

    assert h.adapter.start_count == 1, "second call started despite unknown first call"
    assert _quota(h, root.root_task_id).concurrent == 1


def test_second_approved_call_blocked_after_unknown_recovery(tmp_path):
    """Same guarantee for an unknown produced by crash recovery (window A)."""
    url = f"sqlite:///{(tmp_path / 'cap1_rec.db').as_posix()}"
    h = KernelHarness(
        policy=Policy(collaboration_enabled=True, max_subagents_total=5, max_subagents_concurrent=1),
        url=url,
    )
    root = h.create_root()

    sub1 = h.create_subtask(root.id, "first")
    inv1 = h.create_invocation(sub1.id)
    appr1 = h.create_approval(inv1.id)
    h.approve(appr1.id)
    with h.db.session() as s:
        PolicyService(s, h.policy).consume(inv1.id)
        s.commit()

    # Recovery cannot confirm -> unknown, slot retained.
    worker2, adapter2 = h.new_worker()
    worker2.recover()
    with h.open_session() as s:
        assert s.get(InvocationRow, inv1.id).status == InvocationStatus.UNKNOWN.value
    assert _quota(h, root.root_task_id).concurrent == 1

    # A second approved call must not start.
    _sub, second_inv, _appr = _second_approved(h, root)
    worker2.drain_once()
    assert adapter2.start_count == 0
    assert _quota(h, root.root_task_id).concurrent == 1


# --------------------------------------------------------------------------- #
# 2. retry must not implicitly write off the old call
# --------------------------------------------------------------------------- #


def test_retry_refused_while_old_execution_still_running(tmp_path):
    """retry must NOT silently fail the old call and free its slot."""
    h, root, first_inv = _one_slot_tree(tmp_path, "retry_running.db")
    h.mark_running_old(first_inv.id, seconds=600)

    class StuckAdapter:
        def cancel_invocation(self, invocation_id):
            return {"ok": False, "reason": "still running"}

        def lookup_by_invocation(self, invocation_id):
            return {"status": "running", "adapter_invocation_id": "a1"}

    h.worker.adapter = StuckAdapter()  # type: ignore[assignment]
    h.worker.reap_timeouts()
    with h.open_session() as s:
        assert s.get(InvocationRow, first_inv.id).status == InvocationStatus.UNKNOWN.value

    # Attempting a retry must be refused: the old execution is not confirmed over.
    with pytest.raises(QuotaError) as exc:
        h.worker.reconcile(first_inv.id, action="retry", actor="local-user")
    assert exc.value.code == "not_stopped"

    with h.open_session() as s:
        row = s.get(InvocationRow, first_inv.id)
        # The old call is untouched: still unknown, not failed.
        assert row.status == InvocationStatus.UNKNOWN.value
    # Its slot is still held, and no new invocation/approval was created.
    assert _quota(h, root.root_task_id).concurrent == 1
    with h.open_session() as s:
        n_inv = s.scalar(select(func.count()).select_from(InvocationRow))
        n_appr = s.scalar(select(func.count()).select_from(ApprovalRow))
    assert n_inv == 1, "retry created a new invocation without confirmation"
    assert n_appr == 1


class DelegatingAdapter:
    """Wraps the real Mock adapter, overriding only stop-evidence behaviour.

    Everything else (start counting, the execution store) still delegates, so
    the worker can really call the adapter.
    """

    def __init__(self, inner, *, cancel_ok: bool, lookup_status: str | None):
        self._inner = inner
        self._cancel_ok = cancel_ok
        self._lookup_status = lookup_status

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def cancel_invocation(self, invocation_id):
        if self._cancel_ok:
            return self._inner.cancel_invocation(invocation_id)
        return {"ok": False, "reason": "still running"}

    def lookup_by_invocation(self, invocation_id):
        if self._lookup_status is None:
            return None
        return {"status": self._lookup_status, "adapter_invocation_id": "stub-1"}


def test_retry_allowed_only_after_confirmation_and_releases_once(tmp_path):
    """Once the old execution is CONFIRMED over: release once, then allow a new call."""
    h, root, first_inv = _one_slot_tree(tmp_path, "retry_confirmed.db")
    h.mark_running_old(first_inv.id, seconds=600)

    # Unconfirmed first: the external side still reports the call as running.
    h.worker.adapter = DelegatingAdapter(
        h.adapter, cancel_ok=False, lookup_status="running"
    )
    h.worker.reap_timeouts()
    with pytest.raises(QuotaError) as exc:
        h.worker.reconcile(first_inv.id, action="retry")
    assert exc.value.code == "not_stopped"
    assert _quota(h, root.root_task_id).concurrent == 1

    # Now the external side confirms it ended.
    h.worker.adapter = DelegatingAdapter(
        h.adapter, cancel_ok=True, lookup_status="cancelled"
    )

    result = h.worker.reconcile(first_inv.id, action="retry", actor="local-user")
    assert result["action"] == "retry"
    assert result["remote_stop_confirmed"] is True
    new_appr = result["retry_approval_id"]

    # Released exactly once, and the old call is now settled as superseded.
    q = _quota(h, root.root_task_id)
    assert q.concurrent == 0
    assert q.launched_total == 1, "cumulative total must not be returned"
    with h.open_session() as s:
        assert s.get(InvocationRow, first_inv.id).status == InvocationStatus.FAILED.value
        released = s.scalar(
            select(func.count())
            .select_from(QuotaReservationRow)
            .where(QuotaReservationRow.released_at.is_not(None))
        )
        assert released == 1

    # The retry needs its own approval and does not auto-start.
    with h.open_session() as s:
        assert s.get(ApprovalRow, new_appr).status == "pending"
    h.drain()
    assert h.adapter.start_count == 1  # unchanged: nothing auto-started

    # After approval, the new call occupies the freed slot.
    h.approve(new_appr)
    h.drain()
    assert h.adapter.start_count == 2
    assert _quota(h, root.root_task_id).concurrent == 1


def test_settle_is_the_explicit_way_to_confirm_and_frees_once(tmp_path):
    """A user 'settle' is an explicit confirmation, so it may release the slot."""
    h, root, first_inv = _one_slot_tree(tmp_path, "settle_confirm.db")
    starts_before = h.adapter.start_count
    with h.db.session() as s:
        PolicyService(s, h.policy).mark_unknown(first_inv.id, reason="test")
        s.commit()
    assert _quota(h, root.root_task_id).concurrent == 1

    h.worker.reconcile(first_inv.id, action="settle", outcome="canceled", actor="local-user")

    q = _quota(h, root.root_task_id)
    assert q.concurrent == 0
    assert q.launched_total == 1
    with h.open_session() as s:
        assert s.get(InvocationRow, first_inv.id).status == InvocationStatus.CANCELED.value

    # Now a second approved call can use the freed slot.
    _sub, second_inv, _appr = _second_approved(h, root)
    h.drain()
    assert h.adapter.start_count == starts_before + 1
    assert _quota(h, root.root_task_id).concurrent == 1
