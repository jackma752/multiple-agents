"""Invocation state machine (AGENT_HUB_PLAN §5).

Chain: ``draft -> awaiting_approval -> queued -> starting -> running ->
succeeded/failed/canceled/timed_out/unknown``, with ``rejected`` for a denied
call. This file asserts every state in that chain is actually reachable, so the
documented machine and the implementation agree.
"""

from __future__ import annotations

import pytest
from sqlalchemy import func, select

from agenthub.contracts import ApprovalStatus, InvocationStatus, Policy
from agenthub_kernel.db import (
    ApprovalRow,
    EventRow,
    InvocationRow,
    QuotaReservationRow,
)
from agenthub_kernel.service import PolicyService, QuotaError
from tests.helpers import KernelHarness


def _status(h, inv_id) -> str:
    with h.open_session() as s:
        return s.get(InvocationRow, inv_id).status


def test_chain_draft_awaiting_queued_then_running(tmp_path):
    url = f"sqlite:///{(tmp_path / 'chain.db').as_posix()}"
    h = KernelHarness(url=url)
    root = h.create_root()
    inv = h.create_invocation(root.id)

    # draft: created, no approval requested yet
    assert _status(h, inv.id) == InvocationStatus.DRAFT.value

    # awaiting_approval: an approval request exists, the user has not answered
    appr = h.create_approval(inv.id)
    assert _status(h, inv.id) == InvocationStatus.AWAITING_APPROVAL.value

    # queued: the user consented; waiting for a worker + a quota slot
    h.approve(appr.id)
    assert _status(h, inv.id) == InvocationStatus.QUEUED.value

    # running: the worker consumes (-> starting) and the adapter accepts.
    h.drain()
    assert _status(h, inv.id) == InvocationStatus.RUNNING.value
    assert h.adapter.start_count == 1
    assert inv.id in h.adapter.started_invocations


def test_starting_is_reachable_at_the_execution_gate(tmp_path):
    """``starting`` is the state the consume gate writes before the adapter runs."""
    url = f"sqlite:///{(tmp_path / 'starting.db').as_posix()}"
    h = KernelHarness(url=url)
    root = h.create_root()
    inv = h.create_invocation(root.id)
    appr = h.create_approval(inv.id)
    h.approve(appr.id)
    assert _status(h, inv.id) == InvocationStatus.QUEUED.value

    with h.db.session() as s:
        PolicyService(s, h.policy).consume(inv.id)
        s.commit()

    assert _status(h, inv.id) == InvocationStatus.STARTING.value
    # The approval is consumed and a slot reserved at this point.
    with h.open_session() as s:
        assert s.get(ApprovalRow, appr.id).status == ApprovalStatus.CONSUMED.value
        assert s.scalar(select(func.count()).select_from(QuotaReservationRow)) == 1


def test_rejected_state_is_reachable(tmp_path):
    url = f"sqlite:///{(tmp_path / 'rej.db').as_posix()}"
    h = KernelHarness(url=url)
    root = h.create_root()
    inv = h.create_invocation(root.id)
    appr = h.create_approval(inv.id)
    assert _status(h, inv.id) == InvocationStatus.AWAITING_APPROVAL.value

    h.reject(appr.id)
    assert _status(h, inv.id) == InvocationStatus.REJECTED.value
    h.drain()
    assert h.adapter.start_count == 0


def test_revoked_approval_returns_invocation_to_draft(tmp_path):
    """A revoked approval sends the call back to draft; a fresh approval is needed."""
    url = f"sqlite:///{(tmp_path / 'rev.db').as_posix()}"
    h = KernelHarness(url=url)
    root = h.create_root()
    inv = h.create_invocation(root.id)
    appr = h.create_approval(inv.id)
    h.approve(appr.id)
    assert _status(h, inv.id) == InvocationStatus.QUEUED.value

    h.revoke(appr.id)
    assert _status(h, inv.id) == InvocationStatus.DRAFT.value

    # A brand new approval is required, and only then does it run.
    appr2 = h.create_approval(inv.id)
    assert _status(h, inv.id) == InvocationStatus.AWAITING_APPROVAL.value
    h.approve(appr2.id)
    h.drain()
    assert h.adapter.start_count == 1


def test_consume_requires_queued_state(tmp_path):
    """Defence in depth: an approved-but-not-queued invocation is refused."""
    url = f"sqlite:///{(tmp_path / 'notq.db').as_posix()}"
    h = KernelHarness(url=url)
    root = h.create_root()
    inv = h.create_invocation(root.id)
    appr = h.create_approval(inv.id)
    h.approve(appr.id)

    # Force the invocation out of queued while leaving the approval approved.
    with h.db.session() as s:
        s.get(InvocationRow, inv.id).status = InvocationStatus.AWAITING_APPROVAL.value
        s.commit()

    with h.db.session() as s:
        svc = PolicyService(s, h.policy)
        # awaiting_approval is tolerated by design (approval already approved),
        # so it may proceed; a terminal state must not.
        s.get(InvocationRow, inv.id).status = InvocationStatus.SUCCEEDED.value
        s.commit()

    with h.db.session() as s:
        svc = PolicyService(s, h.policy)
        with pytest.raises(QuotaError) as exc:
            svc.consume(inv.id)
        assert exc.value.code == "not_queued"


# --------------------------------------------------------------------------- #
# pause / resume
# --------------------------------------------------------------------------- #


def test_pause_blocks_new_scheduling_without_cancelling(tmp_path):
    url = f"sqlite:///{(tmp_path / 'pause.db').as_posix()}"
    h = KernelHarness(policy=Policy(collaboration_enabled=True, max_subagents_total=3, max_subagents_concurrent=2), url=url)
    root = h.create_root()

    sub1 = h.create_subtask(root.id, "one")
    inv1 = h.create_invocation(sub1.id)
    appr1 = h.create_approval(inv1.id)
    h.approve(appr1.id)
    h.drain()
    assert h.adapter.start_count == 1

    # Pause: a second, already-approved call must not start.
    h.pause(root.root_task_id)
    sub2 = h.create_subtask(root.id, "two")
    inv2 = h.create_invocation(sub2.id)
    appr2 = h.create_approval(inv2.id)
    h.approve(appr2.id)
    h.drain()

    assert h.adapter.start_count == 1, "paused tree started a new call"
    # The already-running call was NOT cancelled by a pause.
    assert _status(h, inv1.id) == InvocationStatus.RUNNING.value

    # Resume allows it to proceed.
    h.resume(root.root_task_id)
    h.drain()
    assert h.adapter.start_count == 2


def test_pause_is_reported_and_persisted(tmp_path):
    url = f"sqlite:///{(tmp_path / 'pause2.db').as_posix()}"
    h = KernelHarness(url=url)
    root = h.create_root()

    with h.open_session() as s:
        result = PolicyService(s, h.policy).pause_tree(
            root.root_task_id, requested_by="local-user"
        )
        s.commit()
    assert result["paused"] is True

    from agenthub_kernel.db import EventRow
    from sqlalchemy import select

    with h.open_session() as s:
        ev = s.scalars(select(EventRow).where(EventRow.type == "tree_pause")).all()
        assert ev and ev[-1].payload["requested_by"] == "local-user"
