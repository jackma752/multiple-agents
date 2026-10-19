"""Stop and cancel (P2 item 6, part A).

Acceptance:
* a stop request is persisted, so nothing new is scheduled afterwards;
* the stop is wired through to the adapter's cancel;
* quota release is idempotent — driven by the reservation record — so duplicate
  stops or a cancel racing a completion free exactly one slot;
* an adapter that cannot cancel reports ``unsupported`` instead of pretending,
  and such a call becomes ``unknown`` with its slot KEPT (an unconfirmed stop must
  never free a slot or claim the remote work stopped).
"""

from __future__ import annotations

import threading

import pytest
from sqlalchemy import func, select

from agenthub.contracts import InvocationStatus, Policy
from agenthub_kernel.db import (
    ApprovalRow,
    EventRow,
    InvocationRow,
    QuotaReservationRow,
    TreeQuotaRow,
)
from agenthub_kernel.service import PolicyService
from tests.helpers import KernelHarness


def _running_sub(h, goal="sub"):
    """Drive one sub-agent all the way to RUNNING on the harness adapter."""
    root = h.create_root()
    sub = h.create_subtask(root.id, goal)
    inv = h.create_invocation(sub.id)
    appr = h.create_approval(inv.id)
    h.approve(appr.id)
    h.drain()
    assert h.adapter.start_count == 1
    return root, sub, inv, appr


def _tree_quota(h, root_task_id):
    with h.open_session() as s:
        return s.get(TreeQuotaRow, root_task_id)


# --------------------------------------------------------------------------- #
# persisted stop
# --------------------------------------------------------------------------- #


def test_stop_request_is_persisted_and_blocks_new_starts(tmp_path):
    url = f"sqlite:///{(tmp_path / 'stop.db').as_posix()}"
    h = KernelHarness(url=url)
    root = h.create_root()

    # An approved sub-agent exists, but the tree is stopped before we drain.
    sub = h.create_subtask(root.id, "sub")
    inv = h.create_invocation(sub.id)
    appr = h.create_approval(inv.id)
    h.approve(appr.id)

    h.stop_tree(root.root_task_id)
    h.drain()
    assert h.adapter.start_count == 0

    # The stop is durable: it is recorded as an event.
    with h.open_session() as s:
        ev = s.scalars(select(EventRow).where(EventRow.type == "tree_stop")).all()
        assert len(ev) >= 1


def test_stop_request_reports_running_and_persists_actor(tmp_path):
    url = f"sqlite:///{(tmp_path / 'stop2.db').as_posix()}"
    h = KernelHarness(url=url)
    root, _sub, inv, _appr = _running_sub(h)

    with h.open_session() as s:
        plan = PolicyService(s, h.policy).request_stop(
            root.root_task_id, requested_by="local-user"
        )
        s.commit()

    assert plan["already_stopped"] is False
    assert [r["invocation_id"] for r in plan["running"]] == [inv.id]

    with h.open_session() as s:
        ev = s.scalars(
            select(EventRow).where(EventRow.type == "tree_stop")
        ).all()
        assert ev[-1].payload["requested_by"] == "local-user"


# --------------------------------------------------------------------------- #
# cancel wiring
# --------------------------------------------------------------------------- #


def test_cancel_tree_stops_in_flight_call_and_releases_slot(tmp_path):
    url = f"sqlite:///{(tmp_path / 'cancel.db').as_posix()}"
    h = KernelHarness(policy=Policy(collaboration_enabled=True, max_subagents_total=3, max_subagents_concurrent=2), url=url)
    root, _sub, inv, _appr = _running_sub(h)

    before = _tree_quota(h, root.root_task_id)
    assert before.concurrent == 1

    result = h.worker.cancel_tree(root.root_task_id, requested_by="local-user")
    assert result["attempted"] == 1
    assert result["outcomes"][0]["cancel"]["ok"] is True

    with h.open_session() as s:
        assert s.get(InvocationRow, inv.id).status == InvocationStatus.CANCELED.value
        # Reservation claimed exactly once.
        released = s.scalar(
            select(func.count())
            .select_from(QuotaReservationRow)
            .where(QuotaReservationRow.released_at.is_not(None))
        )
        assert released == 1

    after = _tree_quota(h, root.root_task_id)
    assert after.concurrent == 0, "concurrent slot was not released"
    assert after.launched_total == 1, "cumulative total must NOT be returned"

    # The external side recorded the cancellation.
    rec = h.adapter.lookup_by_invocation(inv.id)
    assert rec is not None and rec["status"] == "cancelled"


def test_duplicate_stop_is_idempotent(tmp_path):
    """Stopping twice must not double-release the slot or raise."""
    url = f"sqlite:///{(tmp_path / 'stop2x.db').as_posix()}"
    h = KernelHarness(policy=Policy(collaboration_enabled=True, max_subagents_total=3, max_subagents_concurrent=2), url=url)
    root, _sub, inv, _appr = _running_sub(h)

    first = h.worker.cancel_tree(root.root_task_id)
    second = h.worker.cancel_tree(root.root_task_id)

    assert first["already_stopped"] is False
    assert second["already_stopped"] is True
    # Second pass has nothing running to cancel.
    assert second["attempted"] == 0

    q = _tree_quota(h, root.root_task_id)
    assert q.concurrent == 0
    assert q.launched_total == 1

    with h.open_session() as s:
        released = s.scalar(
            select(func.count())
            .select_from(QuotaReservationRow)
            .where(QuotaReservationRow.released_at.is_not(None))
        )
        assert released == 1, "slot released more than once"


def test_release_is_idempotent_on_repeat_calls(tmp_path):
    """Calling release() repeatedly frees exactly one concurrent slot."""
    url = f"sqlite:///{(tmp_path / 'rel.db').as_posix()}"
    h = KernelHarness(policy=Policy(collaboration_enabled=True, max_subagents_total=3, max_subagents_concurrent=2), url=url)
    root, _sub, inv, _appr = _running_sub(h)

    for _ in range(4):
        with h.open_session() as s:
            PolicyService(s, h.policy).release(inv.id, InvocationStatus.SUCCEEDED)
            s.commit()

    q = _tree_quota(h, root.root_task_id)
    assert q.concurrent == 0, f"concurrent went negative or double-freed: {q.concurrent}"
    assert q.launched_total == 1


def test_cancel_racing_completion_releases_once(tmp_path):
    """A cancel concurrent with a normal completion must free one slot only."""
    url = f"sqlite:///{(tmp_path / 'race_rel.db').as_posix()}"
    h = KernelHarness(policy=Policy(collaboration_enabled=True, max_subagents_total=3, max_subagents_concurrent=2), url=url)
    root, _sub, inv, _appr = _running_sub(h)

    errors: list[str] = []
    barrier = threading.Barrier(2)

    def do_cancel():
        try:
            barrier.wait(timeout=10)
            h.worker.cancel_tree(root.root_task_id)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"cancel:{type(exc).__name__}:{exc}")

    def do_complete():
        try:
            barrier.wait(timeout=10)
            with h.db.session() as s:
                PolicyService(s, h.policy).release(inv.id, InvocationStatus.SUCCEEDED)
                s.commit()
        except Exception as exc:  # noqa: BLE001
            errors.append(f"complete:{type(exc).__name__}:{exc}")

    threads = [threading.Thread(target=do_cancel), threading.Thread(target=do_complete)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, errors
    q = _tree_quota(h, root.root_task_id)
    assert q.concurrent == 0, f"double release or leak: concurrent={q.concurrent}"
    with h.open_session() as s:
        released = s.scalar(
            select(func.count())
            .select_from(QuotaReservationRow)
            .where(QuotaReservationRow.released_at.is_not(None))
        )
        assert released == 1


def test_cancel_does_not_stomp_a_finished_call(tmp_path):
    """If the call finished first, cancel must not overwrite a terminal status."""
    url = f"sqlite:///{(tmp_path / 'cancel_done.db').as_posix()}"
    h = KernelHarness(url=url)
    root, _sub, inv, _appr = _running_sub(h)

    with h.open_session() as s:
        PolicyService(s, h.policy).release(inv.id, InvocationStatus.SUCCEEDED)
        s.commit()

    # Cancel is asked for a call that is already done.
    result = h.worker._cancel_one(inv.id)
    assert result.get("note") == "already_terminal"

    with h.open_session() as s:
        assert s.get(InvocationRow, inv.id).status == InvocationStatus.SUCCEEDED.value


def test_adapter_without_cancel_reports_unsupported(tmp_path):
    """An adapter that cannot cancel must say 'unsupported', not fake success."""
    url = f"sqlite:///{(tmp_path / 'nocancel.db').as_posix()}"
    h = KernelHarness(url=url)
    root, _sub, inv, _appr = _running_sub(h)

    h.worker.adapter = NoCancelAdapter()  # type: ignore[assignment]
    result = h.worker._cancel_one(inv.id)
    assert result["ok"] is False
    assert result["reason"] == "unsupported"
    assert result["remote_stop_confirmed"] is False


class NoCancelAdapter:
    """Inert fixture without cancellation, isolating the tree-stop gate."""

    def execution_admission(self):
        from agenthub.contracts import ExecutionAdmission
        return ExecutionAdmission(True, "simulation", "test_only", "inert fixture")

    def start(self, *a, **k):  # pragma: no cover - unused here
        raise NotImplementedError


def test_unconfirmed_cancel_keeps_the_slot_and_marks_unknown(tmp_path):
    """Regression: an unsupported cancel used to FREE a slot it could not free.

    ``_cancel_one`` settled the invocation as ``canceled`` and released its
    concurrent slot even when the adapter answered ``{"ok": False, "reason":
    "unsupported"}``. For Codex - which cannot cancel - the remote turn may still
    be running, so that both overclaimed a stop and let a second call take a slot
    the first one still occupied. The rule is the same as the timeout path:
    cannot confirm the stop -> ``unknown`` and KEEP the slot.
    """
    url = f"sqlite:///{(tmp_path / 'nocancel-slot.db').as_posix()}"
    h = KernelHarness(
        policy=Policy(collaboration_enabled=True, max_subagents_total=2, max_subagents_concurrent=1), url=url
    )
    root, _sub, inv, _appr = _running_sub(h)
    assert _tree_quota(h, root.root_task_id).concurrent == 1

    h.worker.adapter = NoCancelAdapter()  # type: ignore[assignment]
    result = h.worker.cancel_tree(root.root_task_id, requested_by="local-user")

    assert result["attempted"] == 1
    assert result["confirmed"] == 0, "an unsupported cancel must not count as stopped"
    assert result["unconfirmed"] == 1
    assert result["outcomes"][0]["cancel"]["reason"] == "unsupported"

    with h.open_session() as s:
        row = s.get(InvocationRow, inv.id)
        assert row.status == InvocationStatus.UNKNOWN.value
        assert "could not be confirmed" in (row.error or "")
        # The reservation is deliberately NOT released.
        released = s.scalar(
            select(func.count())
            .select_from(QuotaReservationRow)
            .where(QuotaReservationRow.released_at.is_not(None))
        )
        assert released == 0
    assert _tree_quota(h, root.root_task_id).concurrent == 1, "slot must be kept"


def test_stop_still_blocks_new_calls_when_cancel_is_unsupported(tmp_path):
    """The stop is persisted first, so it holds even if the cancel cannot."""
    url = f"sqlite:///{(tmp_path / 'nocancel-stop.db').as_posix()}"
    h = KernelHarness(
        policy=Policy(collaboration_enabled=True, max_subagents_total=2, max_subagents_concurrent=2), url=url
    )
    root, _sub, _inv, _appr = _running_sub(h)

    h.worker.adapter = NoCancelAdapter()  # type: ignore[assignment]
    h.worker.cancel_tree(root.root_task_id, requested_by="local-user")

    # A second, already-approved sub-agent must not start in a stopped tree.
    sub2 = h.create_subtask(root.id, "second")
    inv2 = h.create_invocation(sub2.id)
    appr2 = h.create_approval(inv2.id)
    h.approve(appr2.id)
    outcomes = h.drain()

    assert outcomes and outcomes[0]["ok"] is False
    assert outcomes[0]["code"] == "tree_stopped"
    with h.open_session() as s:
        assert s.get(InvocationRow, inv2.id).status == InvocationStatus.QUEUED.value


def test_confirmed_cancel_still_settles_and_releases(tmp_path):
    """The honest rule must not break the adapter that CAN cancel."""
    url = f"sqlite:///{(tmp_path / 'cancel-ok.db').as_posix()}"
    h = KernelHarness(
        policy=Policy(collaboration_enabled=True, max_subagents_total=2, max_subagents_concurrent=1), url=url
    )
    root, _sub, inv, _appr = _running_sub(h)

    result = h.worker.cancel_tree(root.root_task_id, requested_by="local-user")

    assert result["confirmed"] == 1 and result["unconfirmed"] == 0
    with h.open_session() as s:
        assert s.get(InvocationRow, inv.id).status == InvocationStatus.CANCELED.value
    assert _tree_quota(h, root.root_task_id).concurrent == 0
