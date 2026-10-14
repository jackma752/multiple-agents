"""Lost-evidence regression: a call that RAN must never be treated as never-started.

Scenario under test (from the P2→P3 handoff):

    a call was running  ->  it becomes unknown (e.g. a timeout whose
    cancellation could not be confirmed)  ->  the external execution record is
    missing

Before the fix, the "did it ever start?" test used only the *current status*.
Once the status became ``unknown`` that trace was gone, so a missing record was
read as "it never reached the adapter" - and the slot was released, even though
the call may still be running. A second approved call could then take the same
slot.

The fix keys the decision on durable columns (``started_at`` /
``adapter_invocation_id``), and the worker now persists
``adapter_invocation_id`` as soon as the adapter accepts a call.
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
from agenthub_kernel.service import PolicyService, QuotaError
from tests.helpers import KernelHarness


def _quota(h, root_id):
    with h.open_session() as s:
        return s.get(TreeQuotaRow, root_id)


def _one_slot_running(h):
    """One sub-agent RUNNING in a tree whose concurrent cap is 1."""
    root = h.create_root()
    sub = h.create_subtask(root.id, "was running")
    inv = h.create_invocation(sub.id)
    appr = h.create_approval(inv.id)
    h.approve(appr.id)
    h.drain()
    assert h.adapter.start_count == 1
    return root, inv


def test_worker_persists_adapter_id_and_started_at(tmp_path):
    """Durable 'this call started' evidence must survive the status update."""
    url = f"sqlite:///{(tmp_path / 'durable.db').as_posix()}"
    h = KernelHarness(url=url)
    _root, inv = _one_slot_running(h)

    with h.open_session() as s:
        row = s.get(InvocationRow, inv.id)
        assert row.status == InvocationStatus.RUNNING.value
        assert row.started_at is not None, "started_at was not persisted"
        assert row.adapter_invocation_id, "adapter_invocation_id was not persisted"


def test_running_then_unknown_then_missing_record_keeps_the_slot(tmp_path):
    """The core regression: unknown + no record must NOT free the slot."""
    url = f"sqlite:///{(tmp_path / 'lost.db').as_posix()}"
    h = KernelHarness(
        policy=Policy(max_subagents_total=3, max_subagents_concurrent=1), url=url
    )
    root, inv = _one_slot_running(h)

    # The call becomes unknown (a timeout whose cancel could not be confirmed).
    with h.db.session() as s:
        PolicyService(s, h.policy).mark_unknown(inv.id, reason="timeout unconfirmed")
        s.commit()
    assert _quota(h, root.root_task_id).concurrent == 1

    # ...and now the external record disappears.
    assert h.adapter.store.delete(inv.id) == 1
    assert h.adapter.lookup_by_invocation(inv.id) is None

    # Evidence must NOT be considered confirmed: the kernel knows it started.
    confirmed, evidence, _cancel = h.worker._stop_evidence(inv.id)
    assert confirmed is False, evidence
    assert "durable evidence" in evidence, evidence

    # A retry must therefore be refused, and nothing may be released.
    with pytest.raises(QuotaError) as exc:
        h.worker.reconcile(inv.id, action="retry", actor="local-user")
    assert exc.value.code == "not_stopped"

    with h.open_session() as s:
        assert s.get(InvocationRow, inv.id).status == InvocationStatus.UNKNOWN.value
        unreleased = s.scalar(
            select(func.count())
            .select_from(QuotaReservationRow)
            .where(QuotaReservationRow.released_at.is_(None))
        )
        assert unreleased == 1, "the reservation was released on lost evidence"
    assert _quota(h, root.root_task_id).concurrent == 1


def test_second_approved_call_blocked_after_lost_evidence(tmp_path):
    """With cap 1, a second approved call must still not start."""
    url = f"sqlite:///{(tmp_path / 'lost2.db').as_posix()}"
    h = KernelHarness(
        policy=Policy(max_subagents_total=3, max_subagents_concurrent=1), url=url
    )
    root, inv = _one_slot_running(h)

    with h.db.session() as s:
        PolicyService(s, h.policy).mark_unknown(inv.id, reason="timeout unconfirmed")
        s.commit()
    h.adapter.store.delete(inv.id)  # external record lost

    sub2 = h.create_subtask(root.id, "second")
    inv2 = h.create_invocation(sub2.id)
    appr2 = h.create_approval(inv2.id)
    h.approve(appr2.id)
    h.worker.drain_once()

    assert h.adapter.start_count == 1, "second call started on a possibly-live slot"
    assert _quota(h, root.root_task_id).concurrent == 1


def test_reap_with_lost_evidence_goes_unknown_not_timed_out(tmp_path):
    """An overdue call whose record is gone must stay unknown with its slot."""
    url = f"sqlite:///{(tmp_path / 'lost_reap.db').as_posix()}"
    h = KernelHarness(
        policy=Policy(
            max_subagents_total=3,
            max_subagents_concurrent=1,
            run_time_limit_seconds=60,
        ),
        url=url,
    )
    root, inv = _one_slot_running(h)
    h.mark_running_old(inv.id, seconds=600)
    h.adapter.store.delete(inv.id)  # evidence disappears before reaping

    result = h.worker.reap_timeouts()

    entry = result["timed_out"][0]
    assert entry["remote_stop_confirmed"] is False, entry
    assert entry["outcome"] == "unknown"
    assert entry["slot_released"] is False
    with h.open_session() as s:
        assert s.get(InvocationRow, inv.id).status == InvocationStatus.UNKNOWN.value
    assert _quota(h, root.root_task_id).concurrent == 1


def test_a_call_that_never_started_can_still_be_confirmed_and_retried(tmp_path):
    """Contrast case: no durable start evidence + no record = truly never ran.

    Window A (consume committed, adapter never called) must remain retryable,
    otherwise the lost-evidence rule would block legitimate retries forever.
    """
    url = f"sqlite:///{(tmp_path / 'never.db').as_posix()}"
    h = KernelHarness(
        policy=Policy(max_subagents_total=3, max_subagents_concurrent=1), url=url
    )
    root = h.create_root()
    sub = h.create_subtask(root.id, "never started")
    inv = h.create_invocation(sub.id)
    appr = h.create_approval(inv.id)
    h.approve(appr.id)

    # Consume (reserve slot) but never call the adapter.
    with h.db.session() as s:
        PolicyService(s, h.policy).consume(inv.id)
        s.commit()
    with h.db.session() as s:
        PolicyService(s, h.policy).mark_unknown(inv.id, reason="crash before start")
        s.commit()

    with h.open_session() as s:
        row = s.get(InvocationRow, inv.id)
        assert row.started_at is None
        assert row.adapter_invocation_id is None

    confirmed, evidence, _ = h.worker._stop_evidence(inv.id)
    assert confirmed is True, evidence
    assert "never reached the adapter" in evidence

    result = h.worker.reconcile(inv.id, action="retry", actor="local-user")
    assert result["action"] == "retry"
    assert _quota(h, root.root_task_id).concurrent == 0


def test_settle_still_resolves_lost_evidence(tmp_path):
    """The user can still adjudicate explicitly, releasing the slot once."""
    url = f"sqlite:///{(tmp_path / 'lost_settle.db').as_posix()}"
    h = KernelHarness(
        policy=Policy(max_subagents_total=3, max_subagents_concurrent=1), url=url
    )
    root, inv = _one_slot_running(h)

    with h.db.session() as s:
        PolicyService(s, h.policy).mark_unknown(inv.id, reason="timeout unconfirmed")
        s.commit()
    h.adapter.store.delete(inv.id)

    h.worker.reconcile(inv.id, action="settle", outcome="canceled", actor="local-user")

    q = _quota(h, root.root_task_id)
    assert q.concurrent == 0
    assert q.launched_total == 1
    with h.open_session() as s:
        assert s.get(InvocationRow, inv.id).status == InvocationStatus.CANCELED.value
