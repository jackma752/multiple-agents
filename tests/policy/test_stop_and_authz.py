"""Stop-state and authorization-boundary tests (AGENT_HUB_PLAN §6).

* Stop-all: a stopped tree must not start new invocations.
* Approval privilege boundary: only the user approval endpoint may turn a
  pending approval into approved. The consume gate re-checks that only an
  APPROVED approval is consumable, so a still-pending one never reaches the
  adapter.
"""

from __future__ import annotations

from agenthub.contracts import ApprovalStatus, Policy
from agenthub_kernel.service import QuotaError
from tests.helpers import KernelHarness


def test_stopped_tree_never_starts_new_invocation():
    h = KernelHarness()
    root = h.create_root()
    inv = h.create_invocation(root.id)
    appr = h.create_approval(inv.id)
    h.approve(appr.id)

    # Stop the tree before draining.
    h.stop_tree(root.root_task_id)

    h.drain()

    assert h.adapter.start_count == 0


def test_approval_cannot_be_reapproved_after_reject():
    h = KernelHarness()
    root = h.create_root()
    inv = h.create_invocation(root.id)
    appr = h.create_approval(inv.id)
    h.reject(appr.id)

    # A rejected approval is not pending, so approve() must refuse.
    try:
        h.approve(appr.id)
        raised = False
    except QuotaError:
        raised = True
    assert raised

    h.drain()
    assert h.adapter.start_count == 0


def test_approval_status_machine_is_linear():
    """pending -> approved -> consumed; consumed is terminal."""
    h = KernelHarness()
    root = h.create_root()
    inv = h.create_invocation(root.id)
    appr = h.create_approval(inv.id)
    h.approve(appr.id)
    h.drain()

    from agenthub_kernel.db import ApprovalRow

    with h.open_session() as session:
        row = session.get(ApprovalRow, appr.id)
        assert row.status == ApprovalStatus.CONSUMED.value

    # A consumed approval cannot be revoked.
    try:
        h.revoke(appr.id)
        revoke_raised = False
    except QuotaError:
        revoke_raised = True
    assert revoke_raised


def test_pending_approval_is_not_consumable():
    """Consume must refuse a pending (never-approved) approval."""
    h = KernelHarness()
    root = h.create_root()
    inv = h.create_invocation(root.id)
    h.create_approval(inv.id)  # stays pending

    with h.open_session() as session:
        from agenthub_kernel.service import PolicyService

        svc = PolicyService(session, h.policy)
        try:
            svc.consume(inv.id)
            consumed = True
        except QuotaError:
            consumed = False
        session.commit()

    assert consumed is False
    assert h.adapter.start_count == 0
