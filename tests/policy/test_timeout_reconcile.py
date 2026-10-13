"""Run-time limit (timed_out) and user reconciliation of unknown calls.

Acceptance:
* an invocation exceeding the tree's ``run_time_limit_seconds`` becomes
  ``timed_out`` and its slot is released exactly once;
* an adapter that cannot cancel is reported as such — we do NOT claim the remote
  side stopped;
* an ``unknown`` invocation can only be resolved by the USER: ``settle`` states
  the outcome (idempotent release), ``retry`` creates a NEW invocation that
  requires a NEW approval (nothing auto-starts).
"""

from __future__ import annotations

import pytest
from sqlalchemy import func, select

from agenthub.contracts import ApprovalStatus, InvocationStatus, Policy
from agenthub_kernel.db import (
    ApprovalRow,
    InvocationRow,
    QuotaReservationRow,
    TreeQuotaRow,
)
from agenthub_kernel.service import PolicyService, QuotaError
from tests.helpers import KernelHarness


def _running_sub(h, goal="sub"):
    root = h.create_root()
    sub = h.create_subtask(root.id, goal)
    inv = h.create_invocation(sub.id)
    appr = h.create_approval(inv.id)
    h.approve(appr.id)
    h.drain()
    return root, sub, inv, appr


def _quota(h, root_id):
    with h.open_session() as s:
        return s.get(TreeQuotaRow, root_id)


# --------------------------------------------------------------------------- #
# timed_out
# --------------------------------------------------------------------------- #


def test_overdue_invocation_is_timed_out_and_releases_slot(tmp_path):
    url = f"sqlite:///{(tmp_path / 'to.db').as_posix()}"
    h = KernelHarness(
        policy=Policy(max_subagents_total=3, max_subagents_concurrent=2, run_time_limit_seconds=60),
        url=url,
    )
    root, _sub, inv, _appr = _running_sub(h)
    assert _quota(h, root.root_task_id).concurrent == 1

    # Backdate the start so it is past the 60s limit.
    h.mark_running_old(inv.id, seconds=600)

    result = h.worker.reap_timeouts()
    assert result["examined"] == 1
    assert result["timed_out"][0]["invocation_id"] == inv.id
    # The Mock adapter supports cancel, so the remote stop IS confirmed.
    assert result["timed_out"][0]["remote_stop_confirmed"] is True

    with h.open_session() as s:
        assert s.get(InvocationRow, inv.id).status == InvocationStatus.TIMED_OUT.value
    q = _quota(h, root.root_task_id)
    assert q.concurrent == 0
    assert q.launched_total == 1, "timing out must not return cumulative budget"


def test_not_overdue_is_left_alone(tmp_path):
    url = f"sqlite:///{(tmp_path / 'noTo.db').as_posix()}"
    h = KernelHarness(
        policy=Policy(max_subagents_total=3, max_subagents_concurrent=2, run_time_limit_seconds=3600),
        url=url,
    )
    root, _sub, inv, _appr = _running_sub(h)

    result = h.worker.reap_timeouts()
    assert result["examined"] == 0
    with h.open_session() as s:
        assert s.get(InvocationRow, inv.id).status == InvocationStatus.RUNNING.value
    assert _quota(h, root.root_task_id).concurrent == 1


def test_timeout_reports_unconfirmed_remote_stop(tmp_path):
    """Without cancel support we must not claim the remote side stopped."""
    url = f"sqlite:///{(tmp_path / 'toUnc.db').as_posix()}"
    h = KernelHarness(
        policy=Policy(max_subagents_total=3, max_subagents_concurrent=2, run_time_limit_seconds=60),
        url=url,
    )
    root, _sub, inv, _appr = _running_sub(h)
    h.mark_running_old(inv.id, seconds=600)

    class NoCancelAdapter:
        def lookup_by_invocation(self, invocation_id):
            return None

    h.worker.adapter = NoCancelAdapter()  # type: ignore[assignment]
    result = h.worker.reap_timeouts()

    assert result["timed_out"][0]["cancel"] == {"ok": False, "reason": "unsupported"}
    assert result["timed_out"][0]["remote_stop_confirmed"] is False
    # Local state is still settled truthfully.
    with h.open_session() as s:
        assert s.get(InvocationRow, inv.id).status == InvocationStatus.TIMED_OUT.value


# --------------------------------------------------------------------------- #
# unknown reconciliation
# --------------------------------------------------------------------------- #


def _make_unknown(h, goal="u"):
    """Crash window A: consumed but the adapter was never called."""
    root = h.create_root()
    sub = h.create_subtask(root.id, goal)
    inv = h.create_invocation(sub.id)
    appr = h.create_approval(inv.id)
    h.approve(appr.id)
    with h.db.session() as s:
        PolicyService(s, h.policy).consume(inv.id)
        s.commit()
    with h.db.session() as s:
        PolicyService(s, h.policy).mark_unknown(inv.id)
        s.commit()
    return root, inv


def test_settle_unknown_releases_slot_once(tmp_path):
    url = f"sqlite:///{(tmp_path / 'settle.db').as_posix()}"
    h = KernelHarness(policy=Policy(max_subagents_total=3, max_subagents_concurrent=2), url=url)
    root, inv = _make_unknown(h)
    assert _quota(h, root.root_task_id).concurrent == 1

    result = h.worker.reconcile(inv.id, action="settle", outcome="failed", actor="local-user")
    assert result["action"] == "settle"
    assert result["outcome"] == "failed"

    with h.open_session() as s:
        assert s.get(InvocationRow, inv.id).status == InvocationStatus.FAILED.value
    q = _quota(h, root.root_task_id)
    assert q.concurrent == 0
    assert q.launched_total == 1

    # Settling again is refused (it is no longer unknown), and cannot
    # double-release the slot.
    with pytest.raises(QuotaError) as exc:
        h.worker.reconcile(inv.id, action="settle", outcome="failed")
    assert exc.value.code == "not_unknown"
    assert _quota(h, root.root_task_id).concurrent == 0


def test_settle_rejects_bad_outcome(tmp_path):
    url = f"sqlite:///{(tmp_path / 'settleBad.db').as_posix()}"
    h = KernelHarness(url=url)
    root, inv = _make_unknown(h)
    with pytest.raises(QuotaError) as exc:
        h.worker.reconcile(inv.id, action="settle", outcome="banana")
    assert exc.value.code == "bad_outcome"
    # Still unknown, slot still held.
    with h.open_session() as s:
        assert s.get(InvocationRow, inv.id).status == InvocationStatus.UNKNOWN.value
    assert _quota(h, root.root_task_id).concurrent == 1


def test_retry_requires_a_new_approval_and_does_not_auto_start(tmp_path):
    url = f"sqlite:///{(tmp_path / 'retry.db').as_posix()}"
    h = KernelHarness(policy=Policy(max_subagents_total=3, max_subagents_concurrent=2), url=url)
    root, inv = _make_unknown(h)
    starts_before = h.adapter.start_count

    result = h.worker.reconcile(inv.id, action="retry", actor="local-user")

    assert result["action"] == "retry"
    assert result["requires_approval"] is True
    new_inv = result["retry_invocation_id"]
    new_appr = result["retry_approval_id"]
    assert new_inv and new_inv != inv.id
    assert new_appr

    # The superseded attempt is settled and its slot freed.
    with h.open_session() as s:
        assert s.get(InvocationRow, inv.id).status == InvocationStatus.FAILED.value
        # The retry is awaiting approval, NOT queued or running.
        assert s.get(InvocationRow, new_inv).status == InvocationStatus.AWAITING_APPROVAL.value
        assert s.get(ApprovalRow, new_appr).status == ApprovalStatus.PENDING.value

    # Nothing auto-starts: no new approval, no adapter call.
    h.drain()
    assert h.adapter.start_count == starts_before

    # Only after the user approves does the retry run.
    h.approve(new_appr)
    h.drain()
    assert h.adapter.start_count == starts_before + 1


def test_retry_snapshot_carries_current_policy_version(tmp_path):
    """A retry created after a policy change must not be born stale."""
    url = f"sqlite:///{(tmp_path / 'retryPol.db').as_posix()}"
    h = KernelHarness(policy=Policy(max_subagents_total=3, max_subagents_concurrent=2), url=url)
    root, inv = _make_unknown(h)

    h.update_policy(root.root_task_id, Policy(max_subagents_total=5, max_subagents_concurrent=3))

    result = h.worker.reconcile(inv.id, action="retry", actor="local-user")
    new_inv = result["retry_invocation_id"]

    with h.open_session() as s:
        row = s.get(InvocationRow, new_inv)
        assert row.snapshot["policy_version"] == 2, row.snapshot

    # And it is genuinely runnable once approved.
    h.approve(result["retry_approval_id"])
    h.drain()
    assert h.adapter.start_count == 1


def test_reconcile_only_applies_to_unknown(tmp_path):
    url = f"sqlite:///{(tmp_path / 'onlyUnknown.db').as_posix()}"
    h = KernelHarness(url=url)
    root, _sub, inv, _appr = _running_sub(h)  # RUNNING, not unknown

    with pytest.raises(QuotaError) as exc:
        h.worker.reconcile(inv.id, action="settle", outcome="failed")
    assert exc.value.code == "not_unknown"


def test_bad_action_is_refused(tmp_path):
    url = f"sqlite:///{(tmp_path / 'badAction.db').as_posix()}"
    h = KernelHarness(url=url)
    _root, inv = _make_unknown(h)
    with pytest.raises(QuotaError) as exc:
        h.worker.reconcile(inv.id, action="explode")
    assert exc.value.code == "bad_action"
