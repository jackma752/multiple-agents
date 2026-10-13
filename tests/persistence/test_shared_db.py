"""Shared persistence tests: API and Worker on one file database.

Acceptance (P2 item 4):
* the database path does not depend on the working directory;
* a second, independent connection (and a real separate process) sees records
  written by the first;
* unapproved calls start 0 times; an approved call executes;
* records survive a "restart" (a fresh Database object on the same file);
* an already-consumed approval is never started a second time;
* different invocations racing for the last quota slot do not exceed the cap.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
import threading

import pytest
from sqlalchemy import func, select

from agenthub.contracts import ApprovalStatus, InvocationStatus, Policy
from agenthub_kernel import config
from agenthub_kernel.db import (
    ApprovalRow,
    Database,
    InvocationRow,
    QuotaReservationRow,
)
from agenthub_kernel.service import PolicyService, QuotaError
from tests.helpers import KernelHarness

REPO_ROOT = config.REPO_ROOT
PKG_PATHS = [
    REPO_ROOT,
    os.path.join(REPO_ROOT, "packages", "contracts"),
    os.path.join(REPO_ROOT, "packages", "adapters"),
    os.path.join(REPO_ROOT, "apps", "worker"),
    os.path.join(REPO_ROOT, "apps", "api"),
]


def _url(tmp_path, name="shared.db") -> str:
    return f"sqlite:///{(tmp_path / name).as_posix()}"


# --------------------------------------------------------------------------- #
# path independence
# --------------------------------------------------------------------------- #


def test_default_db_path_is_cwd_independent():
    """The default DB path must not depend on the current working directory."""
    resolved = config.default_db_url()
    assert ":memory:" not in resolved

    original = os.getcwd()
    try:
        os.chdir(REPO_ROOT)
        a = config.default_db_url()
        os.chdir(os.path.dirname(REPO_ROOT))  # a different cwd entirely
        b = config.default_db_url()
    finally:
        os.chdir(original)

    assert a == b, (a, b)
    # Anchored inside the repo, not relative to cwd.
    assert os.path.abspath(REPO_ROOT) in os.path.abspath(a.replace("sqlite:///", ""))


# --------------------------------------------------------------------------- #
# cross-connection and cross-process visibility
# --------------------------------------------------------------------------- #


def test_second_connection_sees_records_from_first(tmp_path):
    """Two independent Database objects on one file see each other's writes."""
    url = _url(tmp_path)
    writer = Database(url)
    writer.create_all()

    with writer.session() as s:
        task = PolicyService(s).create_task(agent_id="mock", goal="hello", kind="user")
        inv = PolicyService(s).create_invocation(
            task_id=task.id,
            snapshot=__import__("agenthub.contracts", fromlist=["InvocationSnapshot"])
            .InvocationSnapshot(
                agent_id="mock",
                task_input="hello",
                parent_task_id=None,
                depth=0,
            ),
        )
        inv_id = inv.id

    reader = Database(url)  # separate engine/connection
    with reader.session() as s:
        row = s.get(InvocationRow, inv_id)
        assert row is not None
        assert row.task_id == task.id


def test_separate_process_reads_api_written_records(tmp_path):
    """A real separate python process reads what this process committed."""
    url = _url(tmp_path, "xproc.db")
    h = KernelHarness(url=url)
    root = h.create_root()
    inv = h.create_invocation(root.id)
    appr = h.create_approval(inv.id)  # stays pending

    script = textwrap.dedent(
        """
        import sys
        from agenthub_kernel.db import Database, ApprovalRow, InvocationRow
        from sqlalchemy import select, func
        url = sys.argv[1]
        db = Database(url)
        with db.session() as s:
            n_appr = s.scalar(select(func.count()).select_from(ApprovalRow))
            n_inv = s.scalar(select(func.count()).select_from(InvocationRow))
            row = s.get(ApprovalRow, sys.argv[2])
            print(f"APPROVALS={n_appr} INVOCATIONS={n_inv} STATUS={row.status}")
        """
    )
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(PKG_PATHS)
    out_file = tmp_path / "child_out.txt"
    with open(out_file, "w", encoding="utf-8") as fh:
        subprocess.run(
            [sys.executable, "-c", script, url, appr.id],
            stdout=fh,
            stderr=subprocess.STDOUT,
            env=env,
            cwd=str(tmp_path),  # deliberately NOT the repo root
            timeout=120,
            check=False,
        )
    text = out_file.read_text(encoding="utf-8")
    assert "APPROVALS=1" in text, text
    assert "INVOCATIONS=1" in text, text
    assert f"STATUS={ApprovalStatus.PENDING.value}" in text, text


# --------------------------------------------------------------------------- #
# behaviour across restart
# --------------------------------------------------------------------------- #


def test_unapproved_zero_starts_then_approved_executes(tmp_path):
    """Same as the core invariant, but on a file DB: 0 starts, then 1."""
    url = _url(tmp_path, "flow.db")
    h = KernelHarness(url=url)
    root = h.create_root()
    inv = h.create_invocation(root.id)
    appr = h.create_approval(inv.id)

    h.drain()  # pending -> nothing may start
    assert h.adapter.start_count == 0

    h.approve(appr.id)
    h.drain()  # approved -> executes once
    assert h.adapter.start_count == 1


def test_restart_preserves_records_and_does_not_restart_consumed(tmp_path):
    """A fresh worker process over the same file must not re-run a consumed call."""
    url = _url(tmp_path, "restart.db")
    h = KernelHarness(url=url)
    root = h.create_root()
    inv = h.create_invocation(root.id)
    appr = h.create_approval(inv.id)
    h.approve(appr.id)
    h.drain()
    assert h.adapter.start_count == 1

    # Simulate a process restart: brand new Database + new Worker + new adapter,
    # same file. Nothing should start again.
    from agenthub_adapters.mock import MockAdapter
    from agenthub_kernel.worker import Worker

    restarted_db = Database(url)
    restarted_adapter = MockAdapter()
    restarted_worker = Worker(restarted_db, restarted_adapter)
    restarted_worker.drain_once()

    assert restarted_adapter.start_count == 0, "consumed approval was re-started"
    with restarted_db.session() as s:
        assert s.get(InvocationRow, inv.id).status == InvocationStatus.RUNNING.value
        assert s.get(ApprovalRow, appr.id).status == ApprovalStatus.CONSUMED.value
        # exactly one reservation from the original run
        assert s.scalar(select(func.count()).select_from(QuotaReservationRow)) == 1


# --------------------------------------------------------------------------- #
# concurrency on the last quota slot
# --------------------------------------------------------------------------- #


def test_concurrent_distinct_invocations_do_not_exceed_total_cap(tmp_path):
    """Several sub-invocations race for the last quota slot; the cap holds.

    Each thread drives its own invocation through consume() on a file-backed
    DB with independent connections. With max_subagents_total=1 exactly one may
    win; the others must be refused by a quota code and reserve nothing.
    """
    url = _url(tmp_path, "quota_race.db")
    h = KernelHarness(policy=Policy(max_subagents_total=1, max_subagents_concurrent=1), url=url)
    root = h.create_root()

    invocations = []
    for i in range(4):
        sub = h.create_subtask(root.id, f"sub-{i}")
        inv = h.create_invocation(sub.id)
        appr = h.create_approval(inv.id)
        h.approve(appr.id)
        invocations.append(inv.id)

    results: list[str] = []
    lock = threading.Lock()
    barrier = threading.Barrier(len(invocations))

    def consume_one(inv_id: str) -> None:
        try:
            with h.db.session() as session:
                svc = PolicyService(session, h.policy)
                barrier.wait(timeout=15)
                svc.consume(inv_id)
                session.commit()
            outcome = "ok"
        except QuotaError as exc:
            outcome = exc.code
        except Exception as exc:  # pragma: no cover - diagnostic
            outcome = f"err:{type(exc).__name__}:{exc}"
        with lock:
            results.append(outcome)

    threads = [threading.Thread(target=consume_one, args=(i,)) for i in invocations]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    successes = [r for r in results if r == "ok"]
    assert len(successes) == 1, results
    # Losers must all be quota/approval refusals, never silent successes.
    for r in results:
        assert r == "ok" or r in (
            "max_subagents_total",
            "max_subagents_concurrent",
            "already_consumed",
        ), results

    with h.open_session() as s:
        assert s.scalar(select(func.count()).select_from(QuotaReservationRow)) == 1
        consumed = s.scalar(
            select(func.count())
            .select_from(ApprovalRow)
            .where(ApprovalRow.status == ApprovalStatus.CONSUMED.value)
        )
        assert consumed == 1
