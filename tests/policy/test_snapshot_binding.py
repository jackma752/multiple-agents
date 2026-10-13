"""Snapshot binding: changing input, permissions, agent or resources voids the
approval (AGENT_HUB_PLAN §6 bullet 3).

The approval is bound to the invocation's ``payload_hash``, which covers
``agent_id / task_input / permissions / resources / parent / depth /
adapter_version / policy_version``. Any change must make the old approval
unusable, through one of two mechanisms:

* the snapshot changed but the stored hash was not recomputed -> the invocation
  itself is detected as tampered with (``snapshot_changed``);
* the snapshot changed and the hash WAS recomputed (as an API would) -> the
  approval no longer matches the invocation (``approval_hash_mismatch``).

Either way the adapter must not be started.
"""

from __future__ import annotations

import pytest

from agenthub.contracts import InvocationSnapshot
from agenthub_kernel.db import ApprovalRow, InvocationRow
from agenthub_kernel.service import PolicyService, QuotaError
from tests.helpers import KernelHarness


def _approved(h, **snapshot_kwargs):
    root = h.create_root()
    inv = h.create_invocation(root.id, **snapshot_kwargs)
    appr = h.create_approval(inv.id)
    h.approve(appr.id)
    return root, inv, appr


MUTATIONS = {
    "task_input": "A DIFFERENT GOAL",
    "permissions": {"dir": "elsewhere"},
    "agent_id": "some-other-agent",
    "resources": {"cpu": 99},
    "depth": 2,
}


@pytest.mark.parametrize("field,new_value", list(MUTATIONS.items()))
def test_mutating_snapshot_without_rehash_is_refused(tmp_path, field, new_value):
    """A tampered snapshot (hash not recomputed) must not start anything."""
    url = f"sqlite:///{(tmp_path / f'tamper_{field}.db').as_posix()}"
    h = KernelHarness(url=url)
    _root, inv, appr = _approved(h, permissions={"dir": "here"})

    with h.open_session() as s:
        row = s.get(InvocationRow, inv.id)
        snapshot = dict(row.snapshot)
        snapshot[field] = new_value
        row.snapshot = snapshot  # note: stored payload_hash left untouched
        s.commit()

    h.drain()
    assert h.adapter.start_count == 0, f"mutating {field} did not void the approval"
    with h.open_session() as s:
        assert s.get(ApprovalRow, appr.id).status == "approved"  # never consumed


@pytest.mark.parametrize("field,new_value", list(MUTATIONS.items()))
def test_mutating_snapshot_with_rehash_mismatches_approval(tmp_path, field, new_value):
    """A legitimately updated snapshot must not match the old approval hash."""
    url = f"sqlite:///{(tmp_path / f'rehash_{field}.db').as_posix()}"
    h = KernelHarness(url=url)
    _root, inv, appr = _approved(h, permissions={"dir": "here"})

    with h.open_session() as s:
        row = s.get(InvocationRow, inv.id)
        snapshot = dict(row.snapshot)
        snapshot[field] = new_value
        row.snapshot = snapshot
        # Recompute the hash, as an update endpoint would.
        row.payload_hash = InvocationSnapshot(**snapshot).payload_hash()
        s.commit()

    h.drain()
    assert h.adapter.start_count == 0, f"changing {field} did not void the approval"

    with h.open_session() as s:
        row = s.get(InvocationRow, inv.id)
        assert row.status in ("invalid", "draft"), row.status
        assert s.get(ApprovalRow, appr.id).status == "approved"


def test_consume_reports_hash_mismatch_explicitly(tmp_path):
    """The refusal code distinguishes a hash mismatch from a missing approval."""
    url = f"sqlite:///{(tmp_path / 'mismatch.db').as_posix()}"
    h = KernelHarness(url=url)
    _root, inv, _appr = _approved(h, permissions={"dir": "here"})

    with h.open_session() as s:
        row = s.get(InvocationRow, inv.id)
        snapshot = dict(row.snapshot)
        snapshot["permissions"] = {"dir": "elsewhere"}
        row.snapshot = snapshot
        row.payload_hash = InvocationSnapshot(**snapshot).payload_hash()
        s.commit()

    with h.open_session() as s:
        svc = PolicyService(s, h.policy)
        with pytest.raises(QuotaError) as exc:
            svc.consume(inv.id)
        assert exc.value.code == "approval_hash_mismatch"


def test_unchanged_snapshot_still_starts(tmp_path):
    """Control case: no mutation means the approval is still good for one run."""
    url = f"sqlite:///{(tmp_path / 'control.db').as_posix()}"
    h = KernelHarness(url=url)
    _root, _inv, _appr = _approved(h, permissions={"dir": "here"})
    h.drain()
    assert h.adapter.start_count == 1
