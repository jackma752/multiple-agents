"""Regression coverage for the approved-only execution gate."""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import func, select

from agenthub.contracts import ApprovalStatus, InvocationStatus
from agenthub_kernel.db import (
    ApprovalRow,
    EventRow,
    InvocationRow,
    QuotaReservationRow,
)
from agenthub_kernel.service import PolicyService, QuotaError
from tests.helpers import KernelHarness


def _assert_not_started(session, invocation_id, approval_id, status):
    approval = session.get(ApprovalRow, approval_id)
    assert approval.status == status.value
    assert approval.consumed_at is None
    assert session.get(InvocationRow, invocation_id).status == InvocationStatus.DRAFT.value
    assert session.scalar(select(func.count()).select_from(QuotaReservationRow)) == 0
    assert session.scalar(select(func.count()).select_from(EventRow)) == 0


def test_pending_consume_does_not_change_approval_or_reserve_resources():
    h = KernelHarness()
    inv = h.create_invocation(h.create_root().id)
    approval = h.create_approval(inv.id)

    with h.open_session() as session:
        svc = PolicyService(session, h.policy)
        with pytest.raises(QuotaError) as denied:
            svc.consume(inv.id)
        assert denied.value.code == "no_approval"
        # Commit even after rejection to catch accidental writes before denial.
        session.commit()
        _assert_not_started(session, inv.id, approval.id, ApprovalStatus.PENDING)

    h.drain()
    assert h.adapter.start_count == 0


@pytest.mark.parametrize(
    "status",
    [
        ApprovalStatus.PENDING,
        ApprovalStatus.REJECTED,
        ApprovalStatus.EXPIRED,
        ApprovalStatus.REVOKED,
        ApprovalStatus.CONSUMED,
    ],
)
def test_consume_rechecks_status_if_lookup_returns_ineligible_approval(monkeypatch, status):
    h = KernelHarness()
    inv = h.create_invocation(h.create_root().id)
    approval = h.create_approval(inv.id)

    with h.open_session() as session:
        row = session.get(ApprovalRow, approval.id)
        row.status = status.value
        session.commit()
        svc = PolicyService(session, h.policy)
        # Simulate an overly permissive lookup: consume must independently
        # refuse the result before making any execution or quota changes.
        monkeypatch.setattr(svc, "_find_consumable_approval", lambda _: row)
        with pytest.raises(QuotaError) as denied:
            svc.consume(inv.id)
        assert denied.value.code == "not_approved"
        session.commit()
        _assert_not_started(session, inv.id, approval.id, status)

    assert h.adapter.start_count == 0


def test_pending_approval_does_not_shadow_user_approved_record():
    h = KernelHarness()
    inv = h.create_invocation(h.create_root().id)
    pending = h.create_approval(inv.id)
    approved = h.create_approval(inv.id)
    h.approve(approved.id)
    with h.open_session() as session:
        # Force deterministic ordering: the older, unapproved record must
        # remain pending rather than being consumed ahead of the approved one.
        session.get(ApprovalRow, pending.id).created_at = dt.datetime(2020, 1, 1)
        session.get(ApprovalRow, approved.id).created_at = dt.datetime(2020, 1, 2)

    h.drain()
    assert h.adapter.start_count == 1
    with h.open_session() as session:
        pending_row = session.get(ApprovalRow, pending.id)
        approved_row = session.get(ApprovalRow, approved.id)
        assert pending_row.status == ApprovalStatus.PENDING.value
        assert pending_row.consumed_at is None
        assert approved_row.status == ApprovalStatus.CONSUMED.value
        assert approved_row.consumed_at is not None
