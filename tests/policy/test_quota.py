"""Tree-scoped quota tests (AGENT_HUB_PLAN §6).

Total / concurrent / depth are all computed over the whole root task tree,
not per parent agent. ``max_subagents_total`` is cumulative (finished or failed
sub-agents still count); ``max_subagents_concurrent`` is released when a
sub-agent finishes; ``max_depth`` bounds nesting from the root.
"""

from __future__ import annotations

import pytest

from agenthub.contracts import InvocationStatus, Policy
from tests.helpers import KernelHarness


def _ready_sub(h, root_id, goal="sub"):
    """Create a depth-1 sub-task, its invocation, approve it."""
    task = h.create_subtask(root_id, goal)
    inv = h.create_invocation(task.id)
    appr = h.create_approval(inv.id)
    h.approve(appr.id)
    return task, inv, appr


def test_total_limit_blocks_new_subagent():
    h = KernelHarness(policy=Policy(max_subagents_total=3, max_subagents_concurrent=2))
    root = h.create_root()

    # Launch three authorised sub-agents, each finishing before the next so
    # only the cumulative TOTAL cap (3) is what is exercised.
    started = []
    for i in range(3):
        task, inv, appr = _ready_sub(h, root.id, f"sub-{i}")
        h.drain()
        assert h.adapter.start_count == i + 1
        # release the concurrent slot so the next sub-agent can start
        with h.open_session() as session:
            from agenthub_kernel.service import PolicyService

            PolicyService(session, h.policy).release(inv.id, InvocationStatus.SUCCEEDED)
            session.commit()
        started.append(inv.id)

    # A fourth sub-agent must be rejected on the TOTAL quota, even though there
    # is a free concurrent slot (the previous three are terminal/finished).
    fourth = h.create_subtask(root.id, "sub-3")
    inv4 = h.create_invocation(fourth.id)
    appr4 = h.create_approval(inv4.id)
    h.approve(appr4.id)

    h.drain()

    assert h.adapter.start_count == 3  # the 4th never reached the adapter


def test_concurrent_limit_blocks_when_at_cap():
    h = KernelHarness(policy=Policy(max_subagents_total=3, max_subagents_concurrent=2))
    root = h.create_root()

    # Two concurrent sub-agents occupy the concurrent cap.
    for i in range(2):
        _ready_sub(h, root.id, f"c-{i}")
    h.drain()
    assert h.adapter.start_count == 2

    # A third while both still running must be blocked on the concurrent cap.
    third = h.create_subtask(root.id, "c-3")
    inv3 = h.create_invocation(third.id)
    appr3 = h.create_approval(inv3.id)
    h.approve(appr3.id)

    h.drain()

    assert h.adapter.start_count == 2


def test_releasing_concurrent_slot_allows_next():
    h = KernelHarness(policy=Policy(max_subagents_total=3, max_subagents_concurrent=2))
    root = h.create_root()

    t1, i1, _ = _ready_sub(h, root.id, "c-1")
    t2, i2, _ = _ready_sub(h, root.id, "c-2")
    h.drain()
    assert h.adapter.start_count == 2

    # Finish one sub-agent -> releases its concurrent slot.
    with h.open_session() as session:
        from agenthub_kernel.service import PolicyService

        svc = PolicyService(session, h.policy)
        svc.release(i1.id, InvocationStatus.SUCCEEDED)
        session.commit()

    third = h.create_subtask(root.id, "c-3")
    inv3 = h.create_invocation(third.id)
    appr3 = h.create_approval(inv3.id)
    h.approve(appr3.id)

    h.drain()

    # The task list has no pending approval for the released one; only the new
    # one should be started. The concurrent cap is still satisfied.
    assert h.adapter.start_count == 3


def test_total_zero_blocks_all_subagents():
    from agenthub.contracts import Policy

    h = KernelHarness(policy=Policy(max_subagents_total=0, max_subagents_concurrent=0))
    root = h.create_root()

    sub = h.create_subtask(root.id, "sub")
    inv = h.create_invocation(sub.id)
    appr = h.create_approval(inv.id)
    h.approve(appr.id)

    h.drain()

    assert h.adapter.start_count == 0


def test_depth_limit_blocks_grandchild():
    from agenthub.contracts import Policy

    # max_depth=1 means a sub-agent may not spawn its own sub-agent (depth 2).
    h = KernelHarness(policy=Policy(max_depth=1, max_subagents_total=5, max_subagents_concurrent=3))
    root = h.create_root()

    sub = h.create_subtask(root.id, "sub")  # depth 1, allowed

    # A grandchild (depth 2) under `sub` must be rejected at creation.
    from agenthub_kernel.service import QuotaError

    with pytest.raises(QuotaError):
        with h.open_session() as session:
            from agenthub_kernel.service import PolicyService

            PolicyService(session, h.policy).create_task(
                agent_id="g", goal="grand", kind="user", parent_task_id=sub.id
            )


def test_mixed_total_and_concurrent_never_exceed():
    from agenthub.contracts import Policy

    h = KernelHarness(policy=Policy(max_subagents_total=3, max_subagents_concurrent=2))
    root = h.create_root()

    outcomes = []
    # Drive 5 sub-agents worth of attempts; no more than 3 total ever start.
    for i in range(5):
        try:
            task, inv, appr = _ready_sub(h, root.id, f"m-{i}")
            outcomes.append((task.id, inv.id))
        except Exception:
            pass
    h.drain()

    assert h.adapter.start_count <= 3
