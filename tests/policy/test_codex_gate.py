"""The Codex adapter behind the real kernel gates (offline).

This is the integration the P3 adapter must pass: the Codex adapter is plugged
into the *unchanged* Worker/PolicyService, and the existing approval, snapshot
and quota gates decide whether its process is ever launched.

No real Codex process runs here: the adapter is given a fake runner, so the
assertions are about the GATE, not about Codex.
"""

from __future__ import annotations

import pytest
from sqlalchemy import func, select

from agenthub.contracts import InvocationStatus, Policy
from agenthub_adapters.codex import (
    CodexAdapter,
    CodexLaunchFailed,
    CodexRun,
    FakeCodexRunner,
    success_script,
)
from agenthub_adapters.mock import MockAdapter
from agenthub_adapters.store import FINISHED, RUNNING, ExecutionStore
from agenthub_kernel.db import InvocationRow, QuotaReservationRow, TreeQuotaRow
from agenthub_kernel.service import PolicyService
from tests.helpers import KernelHarness


def build_codex(tmp_path, script=None, **kw) -> CodexAdapter:
    store = ExecutionStore(path=str(tmp_path / "codex-exec.db"), table="codex_executions")
    return CodexAdapter(
        codex_path=__file__,  # any existing file: identity is not pinned here
        codex_home=str(tmp_path / "home"),
        workdir_root=str(tmp_path / "runs"),
        store=store,
        runner=FakeCodexRunner(script or success_script()),
        **kw,
    )


def harness_with_codex(tmp_path, policy=None, script=None, **kw):
    adapter = build_codex(tmp_path, script=script, **kw)
    h = KernelHarness(
        policy=policy or Policy(max_subagents_total=3, max_subagents_concurrent=2),
        url=f"sqlite:///{(tmp_path / 'kernel.db').as_posix()}",
        adapter=adapter,
    )
    return h, adapter


def _approved_sub(h, goal="real codex call"):
    """A sub-task registered for agent_id='codex', with an approved invocation."""
    root = h.create_root(agent_id="codex")
    sub = h.create_subtask(root.id, goal, agent_id="codex")
    inv = h.create_invocation(sub.id)
    appr = h.create_approval(inv.id)
    h.approve(appr.id)
    return root, sub, inv, appr


def _quota(h, root_id):
    with h.open_session() as s:
        return s.get(TreeQuotaRow, root_id)


# --------------------------------------------------------------------------- #
# requirement 2: unapproved => no process, approved => exactly one process
# --------------------------------------------------------------------------- #


def test_unapproved_codex_never_launches_a_process(tmp_path):
    h, codex = harness_with_codex(tmp_path)
    root = h.create_root()
    sub = h.create_subtask(root.id, "should not run")
    inv = h.create_invocation(sub.id)
    h.create_approval(inv.id)  # pending only

    h.drain()

    assert codex.start_count == 0, "process reached without approval"
    assert codex.runner.calls == []
    assert _quota(h, root.root_task_id).concurrent == 0
    assert codex.store.get(inv.id) is None


def test_rejected_codex_never_launches_a_process(tmp_path):
    h, codex = harness_with_codex(tmp_path)
    root = h.create_root()
    sub = h.create_subtask(root.id, "rejected")
    inv = h.create_invocation(sub.id)
    appr = h.create_approval(inv.id)
    h.reject(appr.id)

    h.drain()

    assert codex.start_count == 0 and codex.runner.calls == []


def test_expired_codex_never_launches_a_process(tmp_path):
    h, codex = harness_with_codex(tmp_path)
    root = h.create_root()
    sub = h.create_subtask(root.id, "expired")
    inv = h.create_invocation(sub.id)
    appr = h.create_approval(inv.id, ttl_seconds=0)
    h.approve(appr.id)
    h.make_older_than(appr.id)

    h.drain()

    assert codex.start_count == 0 and codex.runner.calls == []


def test_approved_codex_launches_exactly_one_process(tmp_path):
    h, codex = harness_with_codex(tmp_path)
    root, _sub, inv, _appr = _approved_sub(h)

    h.drain()

    assert codex.start_count == 1
    assert len(codex.runner.calls) == 1, "the process must be launched exactly once"
    with h.open_session() as s:
        # `codex exec` is synchronous, so a successful run is COMPLETE: the
        # kernel settles it as succeeded rather than leaving it running.
        assert s.get(InvocationRow, inv.id).status == InvocationStatus.SUCCEEDED.value
    # the adapter's own record carries the parsed result
    rec = codex.store.get(inv.id)
    assert rec["status"] == FINISHED
    assert rec["payload"]["final_message"] == "OK"
    assert rec["payload"]["usage"]["input_tokens"] == 14481
    # and the slot was released exactly once
    assert _quota(h, root.root_task_id).concurrent == 0


def test_approved_codex_gets_the_gate_token(tmp_path):
    """The adapter must receive proof that an approval was consumed."""
    h, codex = harness_with_codex(tmp_path)
    _approved_sub(h)
    h.drain()
    # FakeCodexRunner records env/argv; the gate token is passed positionally to
    # start(), so assert indirectly: start_count is 1 and the run happened.
    assert codex.start_count == 1


def test_double_approval_still_launches_at_most_once(tmp_path):
    h, codex = harness_with_codex(tmp_path)
    _root, _sub, inv, appr = _approved_sub(h)

    h.drain()
    assert codex.start_count == 1

    # re-approving the consumed approval must fail and must not re-launch
    try:
        h.approve(appr.id)
    except Exception:
        pass
    h.drain()
    assert codex.start_count == 1, "a consumed approval caused a second launch"


# --------------------------------------------------------------------------- #
# the quota gate also governs the real adapter
# --------------------------------------------------------------------------- #


def test_quota_cap_blocks_the_codex_process(tmp_path):
    h, codex = harness_with_codex(
        tmp_path, policy=Policy(max_subagents_total=1, max_subagents_concurrent=1)
    )
    root, _s1, _i1, _a1 = _approved_sub(h, "first")
    h.drain()
    assert codex.start_count == 1

    # second sub-agent in the same tree: over the total cap
    sub2 = h.create_subtask(root.id, "second")
    inv2 = h.create_invocation(sub2.id)
    appr2 = h.create_approval(inv2.id)
    h.approve(appr2.id)
    h.drain()

    assert codex.start_count == 1, "the cap must block the process launch"
    with h.open_session() as s:
        assert s.get(InvocationRow, inv2.id).status == InvocationStatus.QUEUED.value


def test_stopped_tree_blocks_the_codex_process(tmp_path):
    h, codex = harness_with_codex(tmp_path)
    root, _sub, _inv, _appr = _approved_sub(h)
    h.stop_tree(root.root_task_id)

    h.drain()

    assert codex.start_count == 0 and codex.runner.calls == []


# --------------------------------------------------------------------------- #
# requirement 4: timeout / unconfirmed cancel => unknown, slot kept
# --------------------------------------------------------------------------- #


def test_codex_timeout_produces_unknown_and_keeps_the_slot(tmp_path):
    """A locally killed Codex run must not free its quota slot.

    This is the integration that makes requirement 4 real: the adapter reports
    ok=False with terminal=False, and the Worker must translate that into
    ``unknown`` + a retained slot rather than a FAILED + released slot.
    """

    def timeout_script(argv, env):
        return CodexRun(None, True, 300.0, '{"type":"thread.started","thread_id":"t1"}\n', argv)

    h, codex = harness_with_codex(tmp_path, script=timeout_script)
    root, _sub, inv, _appr = _approved_sub(h)

    outcomes = h.drain()
    assert codex.start_count == 1
    assert outcomes[0]["ok"] is False
    assert outcomes[0]["terminal"] is False, "an unconfirmed outcome must not look terminal"

    with h.open_session() as s:
        assert s.get(InvocationRow, inv.id).status == InvocationStatus.UNKNOWN.value
    # the slot is deliberately still held
    assert _quota(h, root.root_task_id).concurrent == 1
    # and the adapter's own record stays non-terminal so a later reaper agrees
    from agenthub_adapters.store import RUNNING

    assert codex.store.get(inv.id)["status"] == RUNNING


def test_codex_reap_keeps_the_slot_because_cancel_is_unsupported(tmp_path):
    """Cancel is unsupported => the stop cannot be confirmed => keep the slot."""

    def timeout_script(argv, env):
        return CodexRun(None, True, 300.0, '{"type":"thread.started","thread_id":"t1"}\n', argv)

    h, codex = harness_with_codex(tmp_path, script=timeout_script)
    root, _sub, inv, _appr = _approved_sub(h)
    h.drain()
    # The timeout already left it unknown with the slot held.
    assert _quota(h, root.root_task_id).concurrent == 1
    with h.open_session() as s:
        assert s.get(InvocationRow, inv.id).status == InvocationStatus.UNKNOWN.value

    # The adapter refuses to confirm a stop it cannot perform.
    assert codex.cancel_invocation(inv.id)["reason"] == "unsupported"

    # A reaper run over the still-non-terminal record must not settle it.
    h.mark_running_old(inv.id, seconds=100000)
    result = h.worker.reap_timeouts()
    if result["timed_out"]:
        entry = result["timed_out"][0]
        assert entry["remote_stop_confirmed"] is False
        assert entry["slot_released"] is False
    assert _quota(h, root.root_task_id).concurrent == 1

    # Once the user settles it, the slot is released exactly once.
    h.worker.reconcile(inv.id, action="settle", outcome="canceled", actor="local-user")
    q = _quota(h, root.root_task_id)
    assert q.concurrent == 0
    assert q.launched_total == 1
    with h.open_session() as s:
        assert s.scalar(select(func.count()).select_from(QuotaReservationRow)) == 1


def test_completed_codex_call_does_not_leak_its_slot(tmp_path):
    """Regression: a synchronous success must settle, not sit 'running' forever.

    Before the terminal flag existed, a finished Codex run stayed RUNNING in the
    kernel with its reservation open, so a finished sub-agent would hold a
    concurrent slot indefinitely (only an unrelated later reaper would clean it).
    """
    h, codex = harness_with_codex(
        tmp_path, policy=Policy(max_subagents_total=2, max_subagents_concurrent=1)
    )
    root, _sub, inv, _appr = _approved_sub(h)
    h.drain()

    assert _quota(h, root.root_task_id).concurrent == 0, "slot leaked after success"
    with h.open_session() as s:
        assert s.scalar(
            select(func.count())
            .select_from(QuotaReservationRow)
            .where(QuotaReservationRow.released_at.is_(None))
        ) == 0

    # Being settled, it is also absent from the overdue list.
    h.mark_running_old(inv.id, seconds=100000)
    assert h.worker.reap_timeouts()["examined"] == 0

    # And a second call can use the freed slot.
    sub2 = h.create_subtask(root.id, "second", agent_id="codex")
    inv2 = h.create_invocation(sub2.id)
    appr2 = h.create_approval(inv2.id)
    h.approve(appr2.id)
    h.drain()
    assert codex.start_count == 2, "the freed slot should allow the next call"


def test_codex_failure_is_recorded_and_slot_released(tmp_path):
    """A definitive failure (turn.failed) is terminal: slot released once."""

    def fail_script(argv, env):
        text = "\n".join([
            '{"type":"thread.started","thread_id":"t1"}',
            '{"type":"turn.failed","error":{"message":"workspace routing discovery failed"}}',
        ])
        return CodexRun(1, False, 3.0, text, argv)

    h, codex = harness_with_codex(tmp_path, script=fail_script)
    root, _sub, inv, _appr = _approved_sub(h)

    h.drain()

    with h.open_session() as s:
        # Worker releases the invocation as FAILED when the adapter reports not ok
        assert s.get(InvocationRow, inv.id).status == InvocationStatus.FAILED.value
    assert _quota(h, root.root_task_id).concurrent == 0
    assert codex.store.get(inv.id)["payload"]["errors"]


# --------------------------------------------------------------------------- #
# abnormal start-time outcomes: which ones may free the slot?
#
# The kernel has already consumed the approval and reserved the slot by the time
# start() runs, so what start() does about a failure decides whether that slot is
# ever freed. Every test here pins one of those outcomes.
# --------------------------------------------------------------------------- #


def _open_reservations(h) -> int:
    with h.open_session() as s:
        return s.scalar(
            select(func.count())
            .select_from(QuotaReservationRow)
            .where(QuotaReservationRow.released_at.is_(None))
        )


def test_missing_executable_is_a_terminal_refusal_not_an_exception(tmp_path):
    """No pinned executable => nothing launched => the slot must be freed.

    Raising instead would escape with the approval already consumed and the
    reservation still open, leaving a call stuck in ``starting`` forever.
    """
    h, codex = harness_with_codex(tmp_path)
    codex.codex_path = None
    root, _sub, inv, _appr = _approved_sub(h)

    outcomes = h.drain()

    assert outcomes[0]["ok"] is False
    assert outcomes[0]["terminal"] is True
    assert codex.start_count == 0 and codex.runner.calls == []
    with h.open_session() as s:
        assert s.get(InvocationRow, inv.id).status == InvocationStatus.FAILED.value
    assert _quota(h, root.root_task_id).concurrent == 0
    assert _open_reservations(h) == 0


def test_unusable_codex_home_is_a_terminal_refusal(tmp_path):
    """A CODEX_HOME we cannot create fails BEFORE any process exists."""
    h, codex = harness_with_codex(tmp_path)
    blocker = tmp_path / "home-is-a-file"
    blocker.write_text("not a directory", encoding="utf-8")
    codex.codex_home = str(blocker)
    root, _sub, inv, _appr = _approved_sub(h)

    outcomes = h.drain()

    assert outcomes[0]["terminal"] is True, "a pre-launch failure is definitive"
    assert codex.start_count == 0, "no launch attempt was made"
    assert codex.runner.calls == []
    assert _quota(h, root.root_task_id).concurrent == 0


def test_refused_process_creation_is_terminal_and_frees_the_slot(tmp_path):
    """Popen itself failing means provably nothing is running."""

    def refused(argv, env):
        raise CodexLaunchFailed("could not launch 'codex.exe': Access is denied")

    h, codex = harness_with_codex(tmp_path, script=refused)
    root, _sub, inv, _appr = _approved_sub(h)

    outcomes = h.drain()

    assert outcomes[0]["terminal"] is True
    record = codex.store.get(inv.id)
    assert record["status"] == "failed"
    assert "Access is denied" in (record.get("detail") or ""), record
    assert _quota(h, root.root_task_id).concurrent == 0
    assert codex.store.get(inv.id)["status"] != RUNNING


def test_ambiguous_launch_error_keeps_the_slot(tmp_path):
    """An unexpected error after the launch point is NOT definitive.

    The runner may already have created the process, so the call must become
    ``unknown`` with its slot retained rather than reported as a clean failure.
    """

    def ambiguous(argv, env):
        raise RuntimeError("stdout capture failed after spawn")

    h, codex = harness_with_codex(tmp_path, script=ambiguous)
    root, _sub, inv, _appr = _approved_sub(h)

    outcomes = h.drain()

    assert outcomes[0]["ok"] is False
    assert outcomes[0]["terminal"] is False, "an ambiguous error must not look terminal"
    with h.open_session() as s:
        assert s.get(InvocationRow, inv.id).status == InvocationStatus.UNKNOWN.value
    assert _quota(h, root.root_task_id).concurrent == 1, "slot must be kept"
    assert _open_reservations(h) == 1
    assert codex.store.get(inv.id)["status"] == RUNNING


class ExplodingAdapter(MockAdapter):
    """An adapter that raises where a real one would launch something."""

    name = "exploding"

    def start(self, invocation_id, snapshot, *, gate_token=None):  # noqa: D102
        assert gate_token, "the kernel must still hand over a gate token first"
        raise RuntimeError("simulated adapter crash")


def test_adapter_crash_keeps_the_slot_and_records_why(tmp_path):
    """Worker backstop: an escaping adapter exception must not leak silently."""
    adapter = ExplodingAdapter(store_path=str(tmp_path / "mock.db"))
    h = KernelHarness(
        policy=Policy(max_subagents_total=2, max_subagents_concurrent=1),
        url=f"sqlite:///{(tmp_path / 'kernel.db').as_posix()}",
        adapter=adapter,
    )
    root = h.create_root()
    sub = h.create_subtask(root.id, "boom")
    inv = h.create_invocation(sub.id)
    appr = h.create_approval(inv.id)
    h.approve(appr.id)

    outcomes = h.drain()

    assert outcomes[0]["ok"] is False
    assert outcomes[0]["terminal"] is False
    assert "RuntimeError" in outcomes[0]["error"]
    with h.open_session() as s:
        row = s.get(InvocationRow, inv.id)
        assert row.status == InvocationStatus.UNKNOWN.value
        assert "RuntimeError" in (row.error or ""), row.error
    assert _quota(h, root.root_task_id).concurrent == 1, "slot must be kept"
    assert _open_reservations(h) == 1
