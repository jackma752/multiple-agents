"""The task-status rule (P3 follow-up).

``tasks.status`` was written once at creation and never updated, so a task whose
call had already succeeded still read ``draft``. The rule is now a pure function of
the task's invocation statuses and every write path derives through it.

These tests pin the RULE; `tests/policy/test_codex_gate.py` pins that the write
paths actually apply it.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from agenthub.contracts import InvocationStatus, Policy
from agenthub_kernel.db import InvocationRow, TaskRow, TreeQuotaRow
from agenthub_kernel.service import PolicyService
from agenthub_kernel.taskstatus import DEFAULT_STATUS, derive_task_status, explain
from tests.helpers import KernelHarness


def test_no_calls_means_draft():
    assert derive_task_status([]) == DEFAULT_STATUS == "draft"


def test_single_states_map_to_themselves():
    for status in (
        InvocationStatus.SUCCEEDED,
        InvocationStatus.FAILED,
        InvocationStatus.TIMED_OUT,
        InvocationStatus.CANCELED,
        InvocationStatus.REJECTED,
        InvocationStatus.UNKNOWN,
        InvocationStatus.QUEUED,
        InvocationStatus.RUNNING,
        InvocationStatus.STARTING,
        InvocationStatus.AWAITING_APPROVAL,
        InvocationStatus.INVALID,
        InvocationStatus.DRAFT,
    ):
        expected = {
            # `timed_out` and `rejected` are summarised into the closest
            # actionable bucket rather than inventing new task states.
            InvocationStatus.TIMED_OUT.value: "failed",
            InvocationStatus.REJECTED.value: "canceled",
            InvocationStatus.INVALID.value: "draft",
            InvocationStatus.STARTING.value: "running",
        }.get(status.value, status.value)
        assert derive_task_status([status.value]) == expected, status


@pytest.mark.parametrize(
    "statuses,expected",
    [
        # "needs a human" wins over everything.
        (["succeeded", "unknown"], "unknown"),
        (["running", "unknown"], "unknown"),
        # in-flight beats pending.
        (["running", "queued", "succeeded"], "running"),
        (["starting", "awaiting_approval"], "running"),
        (["queued", "awaiting_approval"], "queued"),
        (["awaiting_approval", "draft"], "awaiting_approval"),
        # a finished-but-imperfect task is reported as needing attention.
        (["succeeded", "failed"], "failed"),
        (["succeeded", "timed_out"], "failed"),
        (["succeeded", "canceled"], "canceled"),
        (["succeeded", "rejected"], "canceled"),
        (["succeeded", "invalid"], "succeeded"),
        (["succeeded", "succeeded"], "succeeded"),
    ],
)
def test_priority_order(statuses, expected):
    assert derive_task_status(statuses) == expected


def test_unknown_values_and_none_are_treated_as_draft():
    assert derive_task_status([None, "", "something_new"]) == "draft"


def test_explain_names_the_order():
    text = explain()
    assert text.startswith("unknown > running")
    assert text.endswith("draft")


# --------------------------------------------------------------------------- #
# the write paths apply it
# --------------------------------------------------------------------------- #


def test_service_syncs_the_task_row_after_a_status_change(tmp_path):
    h = KernelHarness(
        policy=Policy(collaboration_enabled=True, max_subagents_total=2, max_subagents_concurrent=1),
        url=f"sqlite:///{(tmp_path / 'sync.db').as_posix()}",
    )
    root = h.create_root()
    sub = h.create_subtask(root.id, "sync me")
    inv = h.create_invocation(sub.id)

    with h.open_session() as s:
        svc = PolicyService(s, h.policy)
        svc.transition_invocation(inv.id, InvocationStatus.RUNNING)
        s.commit()

    with h.open_session() as s:
        assert s.get(TaskRow, sub.id).status == InvocationStatus.RUNNING.value

    with h.open_session() as s:
        svc = PolicyService(s, h.policy)
        svc.release(inv.id, InvocationStatus.SUCCEEDED)
        s.commit()

    with h.open_session() as s:
        assert s.get(TaskRow, sub.id).status == InvocationStatus.SUCCEEDED.value


def test_read_model_derives_status_even_if_the_row_is_stale(tmp_path):
    """Databases written before this rule existed still report the truth."""
    from fastapi.testclient import TestClient

    from agenthub_kernel.db import Database
    from agenthub_rt.api import create_app

    url = f"sqlite:///{(tmp_path / 'stale.db').as_posix()}"
    db = Database(url)
    db.create_all()
    app = create_app(db=db)
    user = {"Authorization": "Bearer dev-user-token"}

    with TestClient(app) as c:
        created = c.post(
            "/api/invocations",
            json={"task_id": "-", "agent_id": "mock", "task_input": "x"},
            headers=user,
        ).json()
        root_id, inv_id, appr_id = (
            created["task_id"],
            created["invocation_id"],
            created["approval_id"],
        )
        c.post(f"/api/approvals/{appr_id}/approve", headers=user)
        c.post("/api/worker/drain", headers=user)

        # Force the stored column back to the old, wrong value.
        with db.session() as s:
            task = s.get(TaskRow, root_id)
            task.status = "draft"
            s.commit()
            assert s.get(InvocationRow, inv_id).status == InvocationStatus.RUNNING.value

        view = c.get(f"/api/tasks/{root_id}/view", headers=user).json()

    served = {t["id"]: t for t in view["tasks"]}[root_id]
    assert served["status"] == InvocationStatus.RUNNING.value, "derived status wins"
    assert served["stored_status"] == "draft", "the stale row is still visible"
