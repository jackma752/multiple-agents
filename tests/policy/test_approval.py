"""Approval safety valve tests (AGENT_HUB_PLAN §6).

The core invariant: an adapter is only ever started for a genuinely approved,
in-quota, non-expired, non-revoked invocation whose payload did not change.
These tests assert against the Mock adapter's ``start_count``.
"""

from __future__ import annotations

from agenthub.contracts import ApprovalStatus, InvocationStatus
from tests.helpers import KernelHarness


def _make_approved_invocation(harness, *, depth=0, parent=None, permissions=None):
    if parent is None:
        task = harness.create_root()
    elif depth == 1:
        task = harness.create_subtask(parent)
    else:
        # build a grandchild (depth 2)
        raise NotImplementedError("grandchild creation handled in a dedicated test")
    inv = harness.create_invocation(task.id, permissions=permissions)
    appr = harness.create_approval(inv.id)
    harness.approve(appr.id)
    return task, inv, appr


def test_unapproved_invocation_never_reaches_adapter():
    """An invocation with no approval must never call the adapter."""
    h = KernelHarness()
    task = h.create_root()
    inv = h.create_invocation(task.id)
    h.create_approval(inv.id)  # pending, NOT approved

    h.drain()

    assert h.adapter.start_count == 0
    assert inv.status in (InvocationStatus.DRAFT.value, InvocationStatus.AWAITING_APPROVAL.value)


def test_expired_approval_never_reaches_adapter():
    """An expired approval must yield 0 adapter starts."""
    h = KernelHarness()
    task = h.create_root()
    inv = h.create_invocation(task.id)
    appr = h.create_approval(inv.id, ttl_seconds=0)
    h.approve(appr.id)
    h.make_older_than(appr.id)  # force expiry into the past

    h.drain()

    assert h.adapter.start_count == 0


def test_rejected_approval_never_reaches_adapter():
    h = KernelHarness()
    task = h.create_root()
    inv = h.create_invocation(task.id)
    appr = h.create_approval(inv.id)

    h.reject(appr.id)
    h.drain()

    assert h.adapter.start_count == 0


def test_revoked_approval_never_reaches_adapter():
    h = KernelHarness()
    task = h.create_root()
    inv = h.create_invocation(task.id)
    appr = h.create_approval(inv.id)
    h.approve(appr.id)

    h.revoke(appr.id)
    h.drain()

    assert h.adapter.start_count == 0


def test_approved_invocation_starts_adapter_once():
    h = KernelHarness()
    task = h.create_root()
    inv = h.create_invocation(task.id)
    appr = h.create_approval(inv.id)
    h.approve(appr.id)

    h.drain()

    assert h.adapter.start_count == 1
    assert inv.id in h.adapter.started_invocations


def test_snapshot_change_invalidates_old_approval():
    """Changing the payload hash after approval must void the approval."""
    h = KernelHarness()
    task = h.create_root()
    inv = h.create_invocation(task.id, permissions={"dir": "a"})
    appr = h.create_approval(inv.id)
    h.approve(appr.id)

    # Simulate the user editing the task input/permissions afterwards, which
    # changes the payload_hash. Update the invocation snapshot to a new input.
    with h.open_session() as session:
        row = session.get(type(inv), inv.id)
        row.snapshot = dict(row.snapshot)
        row.snapshot["task_input"] = "BUILD SOMETHING ELSE"
        # Recompute the stored hash to the new payload so the mismatch is the
        # trigger (in a real run the API would recompute the hash).
        from agenthub.contracts import InvocationSnapshot

        new_hash = InvocationSnapshot(**row.snapshot).payload_hash()
        row.payload_hash = new_hash
        session.commit()

    # The approval is bound to the OLD hash, so it no longer matches.
    h.drain()

    assert h.adapter.start_count == 0


def test_double_approval_consumed_once():
    """Two approving calls for the same approval must not cause two starts."""
    h = KernelHarness()
    task = h.create_root()
    inv = h.create_invocation(task.id)
    appr = h.create_approval(inv.id)
    h.approve(appr.id)

    h.drain()
    first_count = h.adapter.start_count

    # Try to re-approve the same (now consumed) approval.
    try:
        h.approve(appr.id)
    except Exception:
        pass

    h.drain()

    assert h.adapter.start_count == first_count == 1
