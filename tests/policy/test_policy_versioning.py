"""Policy persistence, versioning and approval invalidation (P2 item 5).

Acceptance:
* policy is persisted on the root task and applies to the whole tree;
* the version increments on every update and previous versions are retained;
* updating a policy voids outstanding UNCONSUMED approvals, while consumed
  approvals and already-running calls are left untouched;
* lowering a limit does not reset the cumulative launched count and does not
  cancel a running call;
* an illegal policy writes nothing (rolls back);
* policy survives a process restart;
* a policy update racing a consume cannot start a call under the old policy.
"""

from __future__ import annotations

import threading

import pytest
from sqlalchemy import func, select

from agenthub.contracts import ApprovalStatus, InvocationStatus, Policy
from agenthub_kernel.db import (
    ApprovalRow,
    Database,
    InvocationRow,
    PolicyVersionRow,
    QuotaReservationRow,
    TaskRow,
)
from agenthub_kernel.service import PolicyService, QuotaError
from tests.helpers import KernelHarness


# --------------------------------------------------------------------------- #
# persistence + versioning
# --------------------------------------------------------------------------- #


def test_policy_persists_and_version_increments(tmp_path):
    url = f"sqlite:///{(tmp_path / 'pol.db').as_posix()}"
    h = KernelHarness(policy=Policy(collaboration_enabled=True, max_subagents_total=3), url=url)
    root = h.create_root()
    assert root.policy_version == 1

    r2 = h.update_policy(root.root_task_id, Policy(collaboration_enabled=True, max_subagents_total=5, max_subagents_concurrent=2))
    assert r2["version"] == 2
    assert r2["policy"]["max_subagents_total"] == 5

    r3 = h.update_policy(root.root_task_id, Policy(collaboration_enabled=True, max_subagents_total=7, max_subagents_concurrent=3))
    assert r3["version"] == 3

    with h.open_session() as s:
        row = s.get(TaskRow, root.root_task_id)
        assert row.policy_version == 3
        assert row.policy["max_subagents_total"] == 7

        # Audit trail: exactly one active version, older ones retained inactive.
        versions = s.scalars(
            select(PolicyVersionRow).order_by(PolicyVersionRow.version)
        ).all()
        assert [v.version for v in versions] == [2, 3]
        active = [v.version for v in versions if v.active]
        assert active == [3]


def test_policy_update_emits_event_and_records_actor(tmp_path):
    url = f"sqlite:///{(tmp_path / 'pol_ev.db').as_posix()}"
    h = KernelHarness(url=url)
    root = h.create_root()
    h.update_policy(root.root_task_id, Policy(collaboration_enabled=True, max_subagents_total=2), updated_by="local-user")

    from agenthub_kernel.db import EventRow

    with h.open_session() as s:
        ev = s.scalars(
            select(EventRow).where(EventRow.type == "policy_updated")
        ).all()
        assert len(ev) == 1
        assert ev[0].payload["updated_by"] == "local-user"
        assert ev[0].payload["version"] == 2


def test_policy_restart_is_preserved(tmp_path):
    """A new process/worker over the same file keeps the updated policy."""
    url = f"sqlite:///{(tmp_path / 'pol_restart.db').as_posix()}"
    h = KernelHarness(url=url)
    root = h.create_root()
    h.update_policy(root.root_task_id, Policy(collaboration_enabled=True, max_subagents_total=0, max_subagents_concurrent=0))

    # Fresh objects, same file.
    from agenthub_adapters.mock import MockAdapter
    from agenthub_kernel.worker import Worker

    db2 = Database(url)
    adapter2 = MockAdapter()
    worker2 = Worker(db2, adapter2)

    with db2.session() as s:
        row = s.get(TaskRow, root.root_task_id)
        assert row.policy_version == 2
        assert row.policy["max_subagents_total"] == 0

    # And the persisted policy is actually enforced after restart: a sub-agent
    # approved under the new policy must not start.
    sub = h.create_subtask(root.id, "sub")
    inv = h.create_invocation(sub.id)
    appr = h.create_approval(inv.id)
    h.approve(appr.id)
    worker2.drain_once()
    assert adapter2.start_count == 0


# --------------------------------------------------------------------------- #
# whole-tree scope
# --------------------------------------------------------------------------- #


def test_policy_applies_to_whole_tree_after_update(tmp_path):
    """Raising then lowering the cap is enforced for descendants too."""
    url = f"sqlite:///{(tmp_path / 'pol_tree.db').as_posix()}"
    h = KernelHarness(policy=Policy(collaboration_enabled=True, max_subagents_total=0, max_subagents_concurrent=0), url=url)
    root = h.create_root()

    # Under the initial cap of 0, a sub-agent cannot start.
    sub = h.create_subtask(root.id, "sub")
    inv = h.create_invocation(sub.id)
    appr = h.create_approval(inv.id)
    h.approve(appr.id)
    h.drain()
    assert h.adapter.start_count == 0

    # Raise the cap for the WHOLE tree: the same approved call can now run.
    h.update_policy(root.root_task_id, Policy(collaboration_enabled=True, max_subagents_total=2, max_subagents_concurrent=2))
    # A new approval is required because the update revoked the old one.
    inv2 = h.create_invocation(sub.id)
    appr2 = h.create_approval(inv2.id)
    h.approve(appr2.id)
    h.drain()
    assert h.adapter.start_count == 1


# --------------------------------------------------------------------------- #
# invalidation semantics
# --------------------------------------------------------------------------- #


def test_policy_update_voids_unconsumed_but_keeps_consumed_and_running(tmp_path):
    url = f"sqlite:///{(tmp_path / 'pol_inv.db').as_posix()}"
    h = KernelHarness(policy=Policy(collaboration_enabled=True, max_subagents_total=5, max_subagents_concurrent=5), url=url)
    root = h.create_root()

    # (c) first: an already-consumed / running call. Approve ONLY this one and
    # drain, so exactly one adapter start happens.
    sub_c = h.create_subtask(root.id, "running-sub")
    inv_c = h.create_invocation(sub_c.id)
    appr_c = h.create_approval(inv_c.id)
    h.approve(appr_c.id)
    h.drain()
    assert h.adapter.start_count == 1

    # (a) a wholly pending approval
    sub_p = h.create_subtask(root.id, "pending-sub")
    inv_p = h.create_invocation(sub_p.id)
    appr_p = h.create_approval(inv_p.id)

    # (b) an approved-but-not-yet-consumed approval
    sub_a = h.create_subtask(root.id, "approved-sub")
    inv_a = h.create_invocation(sub_a.id)
    appr_a = h.create_approval(inv_a.id)
    h.approve(appr_a.id)

    result = h.update_policy(root.root_task_id, Policy(collaboration_enabled=True, max_subagents_total=1, max_subagents_concurrent=1))
    assert result["revoked_approvals"] == 2  # the pending and the approved one

    with h.open_session() as s:
        assert s.get(ApprovalRow, appr_p.id).status == ApprovalStatus.REVOKED.value
        assert s.get(ApprovalRow, appr_a.id).status == ApprovalStatus.REVOKED.value
        # The consumed approval and the running call are untouched.
        assert s.get(ApprovalRow, appr_c.id).status == ApprovalStatus.CONSUMED.value
        assert s.get(InvocationRow, inv_c.id).status == InvocationStatus.RUNNING.value

    # Draining again must start nothing new: both remaining approvals are void.
    h.drain()
    assert h.adapter.start_count == 1


def test_lowering_limit_keeps_cumulative_and_running(tmp_path):
    """Lowering a cap must not reset the cumulative count or cancel running work."""
    url = f"sqlite:///{(tmp_path / 'pol_lower.db').as_posix()}"
    h = KernelHarness(policy=Policy(collaboration_enabled=True, max_subagents_total=3, max_subagents_concurrent=2), url=url)
    root = h.create_root()

    ids = []
    for i in range(2):
        sub = h.create_subtask(root.id, f"s{i}")
        inv = h.create_invocation(sub.id)
        appr = h.create_approval(inv.id)
        h.approve(appr.id)
        ids.append((inv.id, appr.id))
    h.drain()
    assert h.adapter.start_count == 2

    h.update_policy(root.root_task_id, Policy(collaboration_enabled=True, max_subagents_total=1, max_subagents_concurrent=1))

    with h.open_session() as s:
        # Two sub-agent invocations were launched; the count is NOT reset.
        sub_tasks = [
            t.id
            for t in s.scalars(select(TaskRow).where(TaskRow.root_task_id == root.root_task_id)).all()
            if t.depth >= 1
        ]
        launched = s.scalar(
            select(func.count())
            .select_from(InvocationRow)
            .where(
                InvocationRow.task_id.in_(sub_tasks),
                InvocationRow.status != InvocationStatus.DRAFT.value,
            )
        )
        assert launched == 2, "cumulative launched count was reset by lowering the cap"
        # Running calls are untouched (not cancelled).
        running = s.scalar(
            select(func.count())
            .select_from(InvocationRow)
            .where(InvocationRow.status == InvocationStatus.RUNNING.value)
        )
        assert running == 2


# --------------------------------------------------------------------------- #
# illegal parameters roll back
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "bad",
    [
        dict(max_subagents_total=1, max_subagents_concurrent=2),   # concurrent > total
        dict(max_subagents_total=-1, max_subagents_concurrent=0),  # negative
        dict(max_subagents_total=3, max_subagents_concurrent=3, max_depth=-2),
        dict(max_subagents_total=3, max_subagents_concurrent=1, max_agent_invocations=0),
    ],
)
def test_illegal_policy_writes_nothing(tmp_path, bad):
    url = f"sqlite:///{(tmp_path / 'pol_bad.db').as_posix()}"
    h = KernelHarness(policy=Policy(collaboration_enabled=True, max_subagents_total=3, max_subagents_concurrent=2), url=url)
    root = h.create_root()

    # A live approval that must survive the rejected update.
    sub = h.create_subtask(root.id, "sub")
    inv = h.create_invocation(sub.id)
    appr = h.create_approval(inv.id)
    h.approve(appr.id)

    with pytest.raises(ValueError):
        h.update_policy(root.root_task_id, Policy(collaboration_enabled=True, **bad))

    with h.open_session() as s:
        row = s.get(TaskRow, root.root_task_id)
        assert row.policy_version == 1, "version bumped despite invalid policy"
        assert row.policy["max_subagents_total"] == 3, "policy written despite invalid input"
        assert s.get(ApprovalRow, appr.id).status == ApprovalStatus.APPROVED.value
        assert s.scalar(select(func.count()).select_from(PolicyVersionRow)) == 0

    # And the still-approved call can run normally afterwards.
    h.drain()
    assert h.adapter.start_count == 1


def test_concurrent_race_for_last_concurrent_slot(tmp_path):
    """Several approved sub-agents race for the LAST concurrent slot.

    ``max_subagents_concurrent=1`` while the total cap is generous, so the
    binding constraint is the concurrent slot, not the cumulative total.
    Exactly one may occupy it; the others must be refused and reserve nothing.
    """
    url = f"sqlite:///{(tmp_path / 'pol_conc_slot.db').as_posix()}"
    h = KernelHarness(policy=Policy(collaboration_enabled=True, max_subagents_total=5, max_subagents_concurrent=1), url=url)
    root = h.create_root()

    invocations = []
    for i in range(4):
        sub = h.create_subtask(root.id, f"c{i}")
        inv = h.create_invocation(sub.id)
        appr = h.create_approval(inv.id)
        h.approve(appr.id)
        invocations.append(inv.id)

    results: list[str] = []
    lock = threading.Lock()
    barrier = threading.Barrier(len(invocations))

    def consume_one(inv_id: str) -> None:
        try:
            with h.db.session() as s:
                svc = PolicyService(s, h.policy)
                barrier.wait(timeout=15)
                svc.consume(inv_id)
                s.commit()
            out = "ok"
        except QuotaError as exc:
            out = exc.code
        except Exception as exc:  # pragma: no cover - diagnostic
            out = f"err:{type(exc).__name__}:{exc}"
        with lock:
            results.append(out)

    threads = [threading.Thread(target=consume_one, args=(i,)) for i in invocations]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    successes = [r for r in results if r == "ok"]
    assert len(successes) == 1, results
    for r in results:
        assert r == "ok" or r in (
            "max_subagents_concurrent",
            "max_subagents_total",
            "already_consumed",
        ), results

    with h.open_session() as s:
        # Exactly one concurrent slot is held.
        assert s.scalar(select(func.count()).select_from(QuotaReservationRow)) == 1
        held = s.scalar(
            select(func.count())
            .select_from(InvocationRow)
            .where(
                InvocationRow.status.in_(
                    [InvocationStatus.STARTING.value, InvocationStatus.RUNNING.value]
                )
            )
        )
        assert held == 1, (held, results)


# --------------------------------------------------------------------------- #
# update vs consume competition
# --------------------------------------------------------------------------- #


def test_consume_is_refused_after_policy_change(tmp_path):
    """A call approved under v1 must not run once the tree is at v2.

    The update revokes the outstanding approval, so the worker never even picks
    the invocation (first line of defence); consume itself would also refuse it
    via the policy-version check (second line of defence).
    """
    url = f"sqlite:///{(tmp_path / 'pol_race_det.db').as_posix()}"
    h = KernelHarness(url=url)
    root = h.create_root()
    sub = h.create_subtask(root.id, "sub")
    inv = h.create_invocation(sub.id)
    appr = h.create_approval(inv.id)
    h.approve(appr.id)

    h.update_policy(root.root_task_id, Policy(collaboration_enabled=True, max_subagents_total=5, max_subagents_concurrent=5))

    h.drain()
    assert h.adapter.start_count == 0, "a call approved under the old policy was started"

    with h.open_session() as s:
        # First line of defence: the approval was revoked by the update.
        assert s.get(ApprovalRow, appr.id).status == ApprovalStatus.REVOKED.value
        # The invocation was never consumed.
        assert s.get(InvocationRow, inv.id).status != InvocationStatus.RUNNING.value
        assert s.scalar(select(func.count()).select_from(QuotaReservationRow)) == 0


def test_consume_refuses_when_approval_survived_but_version_moved(tmp_path):
    """Second line of defence: even an APPROVED approval is refused at consume.

    We force the approval back to ``approved`` after the policy update, which
    simulates any path that fails to revoke it. The consume-time policy-version
    check must still refuse to start the call.
    """
    url = f"sqlite:///{(tmp_path / 'pol_race_forced.db').as_posix()}"
    h = KernelHarness(url=url)
    root = h.create_root()
    sub = h.create_subtask(root.id, "sub")
    inv = h.create_invocation(sub.id)
    appr = h.create_approval(inv.id)
    h.approve(appr.id)

    h.update_policy(root.root_task_id, Policy(collaboration_enabled=True, max_subagents_total=5, max_subagents_concurrent=5))

    # Deliberately resurrect the approval to bypass the revocation defence.
    with h.open_session() as s:
        s.get(ApprovalRow, appr.id).status = ApprovalStatus.APPROVED.value
        s.commit()

    h.drain()
    assert h.adapter.start_count == 0, "consume did not enforce the policy-version check"
    with h.open_session() as s:
        assert s.get(InvocationRow, inv.id).status == InvocationStatus.INVALID.value
        assert s.scalar(select(func.count()).select_from(QuotaReservationRow)) == 0


def test_policy_update_racing_consume_never_uses_stale_policy(tmp_path):
    """Concurrent update + consume: either it wins before the update, or it is refused.

    The invariant is that a call can never *start* under a policy version that
    the tree has already moved past.
    """
    url = f"sqlite:///{(tmp_path / 'pol_race.db').as_posix()}"
    h = KernelHarness(policy=Policy(collaboration_enabled=True, max_subagents_total=5, max_subagents_concurrent=5), url=url)
    root = h.create_root()

    invocations = []
    for i in range(4):
        sub = h.create_subtask(root.id, f"r{i}")
        inv = h.create_invocation(sub.id)
        appr = h.create_approval(inv.id)
        h.approve(appr.id)
        invocations.append(inv.id)

    results: list[str] = []
    lock = threading.Lock()
    barrier = threading.Barrier(len(invocations) + 1)

    def racer_update():
        try:
            barrier.wait(timeout=15)
            with h.db.session() as s:
                PolicyService(s, h.policy).update_policy(
                    root.root_task_id,
                    Policy(collaboration_enabled=True, max_subagents_total=0, max_subagents_concurrent=0),
                )
                s.commit()
            out = "update:ok"
        except Exception as exc:  # pragma: no cover - diagnostic
            out = f"update:err:{type(exc).__name__}"
        with lock:
            results.append(out)

    def racer_consume(inv_id: str):
        try:
            barrier.wait(timeout=15)
            with h.db.session() as s:
                PolicyService(s, h.policy).consume(inv_id)
                s.commit()
            out = "consume:ok"
        except QuotaError as exc:
            out = f"consume:{exc.code}"
        except Exception as exc:  # pragma: no cover - diagnostic
            out = f"consume:err:{type(exc).__name__}"
        with lock:
            results.append(out)

    threads = [threading.Thread(target=racer_update)]
    threads += [threading.Thread(target=racer_consume, args=(i,)) for i in invocations]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    update_results = [r for r in results if r.startswith("update:")]
    assert update_results == ["update:ok"], results

    # Whatever the interleaving, the tree ends at v2 with a cap of 0, so any
    # consume that ran AFTER the update must have been refused, and the number
    # of successes must equal the reservations actually held.
    with h.open_session() as s:
        root_row = s.get(TaskRow, root.root_task_id)
        assert root_row.policy_version == 2
        reservations = s.scalar(select(func.count()).select_from(QuotaReservationRow))
        successes = len([r for r in results if r == "consume:ok"])
        assert reservations == successes, (results, reservations, successes)
        # No consume may still be sitting on an approved (unconsumed) approval
        # that the update should have revoked, unless it already succeeded.
        for inv_id in invocations:
            status = s.get(InvocationRow, inv_id).status
            assert status in (
                InvocationStatus.STARTING.value,
                InvocationStatus.RUNNING.value,
                InvocationStatus.INVALID.value,
                InvocationStatus.DRAFT.value,
            ), (inv_id, status, results)
