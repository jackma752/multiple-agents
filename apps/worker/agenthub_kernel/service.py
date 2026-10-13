"""Domain services: approval gateway and quota evaluation.

These are pure-ish SQLAlchemy-backed services that implement the safety
valves. Keeping them separate from the HTTP/FastAPI layer lets us test the
critical path (approval validity + quota + stop state) directly in pytest.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any, Optional

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from agenthub.contracts import (
    ApprovalStatus,
    InvocationSnapshot,
    InvocationStatus,
    Policy,
    RUNNING_OR_STARTING,
    TERMINAL_STATUSES,
)
from agenthub_kernel.db import (
    ApprovalRow,
    EventRow,
    InvocationRow,
    QuotaReservationRow,
    TaskRow,
)

UTC = dt.timezone.utc


def now() -> dt.datetime:
    """Current UTC as offset-naive.

    SQLite's ``DateTime`` column stores values without tz info (naive). Keeping
    ``now()`` naive-UTC avoids ``TypeError: can't compare offset-naive and
    offset-aware`` when comparing against ``expires_at`` read back from SQLite.
    """
    return dt.datetime.now(UTC).replace(tzinfo=None)


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


class QuotaError(Exception):
    """Raised when a quota or approval prevents starting the adapter."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class PolicyService:
    """Approval + quota + stop-state enforcement.

    ``consume`` is the single gate a Worker must call before starting an
    adapter. It performs, inside one transaction:

      1. Re-reads the invocation and recomputes/verifies its payload_hash.
      2. Confirms the approval is explicitly approved and not expired.
      3. Confirms the approval's stored payload_hash matches the invocation's.
      4. Confirms the tree is not stopped (no STOP_ALL request).
      5. Evaluates the three tree-scoped quotas.
      6. Atomically marks the approval ``consumed`` and inserts a concurrent
         quota reservation.
      7. Only if all of the above pass does the caller start the adapter.

    Any failure raises :class:`QuotaError` and rolls back, so an approval can
    never be double-consumed and an unapproved/expired/denied invocation can
    never reach an adapter.
    """

    def __init__(self, session: Session, default_policy: Optional[Policy] = None) -> None:
        self.session = session
        self.default_policy = default_policy or Policy()

    # ------------------------------------------------------------------ #
    # Approval gateway
    # ------------------------------------------------------------------ #

    def create_task(
        self,
        *,
        agent_id: str,
        goal: str,
        kind: str = "user",
        parent_task_id: Optional[str] = None,
        policy: Optional[Policy] = None,
    ) -> TaskRow:
        """Create a task node, assigning root_task_id and depth."""
        policy = policy or self.default_policy
        task_id = new_id("task")
        if parent_task_id is None:
            # The root task's own id IS the root_task_id, so the tree is
            # addressable via a single id and `get(TaskRow, root_task_id)` works.
            root_task_id = task_id
            depth = 0
        else:
            parent = self.session.get(TaskRow, parent_task_id)
            if parent is None:
                raise QuotaError("no_parent", f"parent task {parent_task_id} not found")
            root_task_id = parent.root_task_id
            depth = parent.depth + 1
            if depth > policy.max_depth:
                raise QuotaError(
                    "max_depth",
                    f"depth {depth} exceeds max_depth {policy.max_depth}",
                )

        task = TaskRow(
            id=task_id,
            root_task_id=root_task_id,
            parent_task_id=parent_task_id,
            depth=depth,
            kind=kind,
            goal=goal,
            agent_id=agent_id,
            status="draft",
            policy_version=policy.version,
            # Store the effective policy on the root node so the worker can
            # enforce the same tree-scoped limits without being told the policy
            # out-of-band (AGENT_HUB_PLAN §5: policy_versions are per-tree).
            policy=policy.as_dict() if depth == 0 else None,
        )
        self.session.add(task)
        self.session.flush()
        return task

    def create_invocation(
        self,
        *,
        task_id: str,
        snapshot: InvocationSnapshot,
    ) -> InvocationRow:
        """Create a draft invocation with an immutable payload_hash."""
        task = self.session.get(TaskRow, task_id)
        if task is None:
            raise QuotaError("no_task", f"task {task_id} not found")
        inv = InvocationRow(
            id=new_id("inv"),
            task_id=task_id,
            payload_hash=snapshot.payload_hash(),
            snapshot=snapshot.to_dict(),
            status=InvocationStatus.DRAFT.value,
        )
        self.session.add(inv)
        self.session.flush()
        return inv

    def create_approval(
        self,
        *,
        invocation_id: str,
        user_id: str = "local-user",
        ttl_seconds: int = 300,
    ) -> ApprovalRow:
        """Create a pending approval bound to the invocation's current hash."""
        inv = self.session.get(InvocationRow, invocation_id)
        if inv is None:
            raise QuotaError("no_invocation", f"invocation {invocation_id} not found")
        appr = ApprovalRow(
            id=new_id("appr"),
            invocation_id=invocation_id,
            payload_hash=inv.payload_hash,
            user_id=user_id,
            status=ApprovalStatus.PENDING.value,
            policy_version=self.default_policy.version,
            expires_at=now() + dt.timedelta(seconds=ttl_seconds),
        )
        self.session.add(appr)
        self.session.flush()
        return appr

    def transition_invocation(self, invocation_id: str, status: InvocationStatus) -> None:
        from agenthub.contracts import TERMINAL_STATUSES

        inv = self.session.get(InvocationRow, invocation_id)
        if inv is None:
            raise QuotaError("no_invocation", f"invocation {invocation_id} not found")
        if inv.status in TERMINAL_STATUSES:
            return
        inv.status = status.value
        if status is InvocationStatus.RUNNING:
            inv.started_at = now()

    def approve(self, approval_id: str) -> ApprovalRow:
        """Mark pending->approved. Only a valid pending approval can be approved."""
        appr = self.session.get(ApprovalRow, approval_id)
        if appr is None:
            raise QuotaError("no_approval", f"approval {approval_id} not found")
        if appr.status != ApprovalStatus.PENDING.value:
            raise QuotaError("not_pending", f"approval {approval_id} is {appr.status}")
        appr.status = ApprovalStatus.APPROVED.value
        appr.approved_at = now()
        self.session.flush()
        return appr

    def reject(self, approval_id: str) -> ApprovalRow:
        appr = self.session.get(ApprovalRow, approval_id)
        if appr is None:
            raise QuotaError("no_approval", f"approval {approval_id} not found")
        if appr.status != ApprovalStatus.PENDING.value:
            raise QuotaError("not_pending", f"approval {approval_id} is {appr.status}")
        appr.status = ApprovalStatus.REJECTED.value
        self.session.flush()
        return appr

    def revoke(self, approval_id: str) -> ApprovalRow:
        """Revoke a not-yet-consumed approval (pending or approved)."""
        appr = self.session.get(ApprovalRow, approval_id)
        if appr is None:
            raise QuotaError("no_approval", f"approval {approval_id} not found")
        if appr.status == ApprovalStatus.CONSUMED.value:
            raise QuotaError("already_consumed", f"approval {approval_id} already consumed")
        if appr.status in (
            ApprovalStatus.EXPIRED.value,
            ApprovalStatus.REVOKED.value,
        ):
            raise QuotaError("already_terminal", f"approval {approval_id} is {appr.status}")
        appr.status = ApprovalStatus.REVOKED.value
        self.session.flush()
        return appr

    def expire_stale(self) -> int:
        """Mark expired approvals. Returns count expired."""
        stmt = select(ApprovalRow).where(
            ApprovalRow.status.in_(
                [ApprovalStatus.PENDING.value, ApprovalStatus.APPROVED.value]
            ),
            ApprovalRow.expires_at < now(),
        )
        count = 0
        for appr in self.session.scalars(stmt):
            appr.status = ApprovalStatus.EXPIRED.value
            count += 1
        self.session.flush()
        return count

    # ------------------------------------------------------------------ #
    # Quota evaluation (tree-scoped)
    # ------------------------------------------------------------------ #

    def _quotas(self, invocation: InvocationRow) -> Policy:
        # The effective policy is per-tree and stored on the root task node.
        # Read it from the DB so the worker enforces the limits that were in
        # effect when the tree was created, not whatever default it was built
        # with (AGENT_HUB_PLAN §4.2 / §5).
        task = self.session.get(TaskRow, invocation.task_id)
        if task is not None:
            root = self.session.get(TaskRow, task.root_task_id)
            if root is not None and root.policy:
                return Policy(**{k: v for k, v in root.policy.items() if k != "version"})
        return self.default_policy

    def evaluate_quota(self, invocation: InvocationRow) -> dict[str, Any]:
        """Return the tree-scoped quota state for an invocation.

        Does not mutate anything; ``consume`` calls this and then commits.
        """
        task = self.session.get(TaskRow, invocation.task_id)
        policy = self._quotas(invocation)
        is_subagent = task.depth >= 1

        # cumulative launched sub-agents in this tree (total quota)
        launched = self._tree_subagent_launch_count(task.root_task_id, include_depth=1)
        # concurrent currently running/starting across the tree
        concurrent = self._tree_concurrent_count(task.root_task_id)

        tree_status = {
            "root_task_id": task.root_task_id,
            "depth": task.depth,
            "is_subagent": is_subagent,
            "launched_subagents": launched,
            "concurrent": concurrent,
            "max_subagents_total": policy.max_subagents_total,
            "max_subagents_concurrent": policy.max_subagents_concurrent,
            "max_depth": policy.max_depth,
            "max_agent_invocations": policy.max_agent_invocations,
            "total_invocations": self._tree_invocation_count(task.root_task_id),
        }
        return tree_status

    def _tree_subagent_launch_count(self, root_task_id: str, include_depth: int = 1) -> int:
        """Count sub-agent invocations (depth >= 1) that were launched.

        This is cumulative: finished/failed/canceled sub-agents STILL count
        toward ``max_subagents_total`` (AGENT_HUB_PLAN §4.2).
        """
        tasks = self.session.scalars(
            select(TaskRow).where(TaskRow.root_task_id == root_task_id)
        ).all()
        sub_task_ids = {t.id for t in tasks if t.depth >= include_depth}
        if not sub_task_ids:
            return 0
        launched = self.session.scalars(
            select(InvocationRow).where(InvocationRow.task_id.in_(sub_task_ids))
        ).all()
        # launched = created (draft->...); count those not draft
        return sum(1 for inv in launched if inv.status != InvocationStatus.DRAFT.value)

    def _tree_concurrent_count(self, root_task_id: str) -> int:
        """Count sub-agent (depth >= 1) invocations occupying a concurrent slot.

        Matches ``max_subagents_concurrent`` semantics: the limit applies to
        whole-tree sub-agent *instances* (AGENT_HUB_PLAN §4.2), not to the main
        agent (depth 0).
        """
        tasks = self.session.scalars(
            select(TaskRow).where(TaskRow.root_task_id == root_task_id)
        ).all()
        sub_task_ids = {t.id for t in tasks if t.depth >= 1}
        if not sub_task_ids:
            return 0
        running = self.session.scalars(
            select(InvocationRow).where(
                InvocationRow.task_id.in_(sub_task_ids),
                InvocationRow.status.in_(
                    [InvocationStatus.STARTING.value, InvocationStatus.RUNNING.value]
                ),
            )
        ).all()
        return len(running)

    def _tree_invocation_count(self, root_task_id: str) -> int:
        tasks = self.session.scalars(
            select(TaskRow).where(TaskRow.root_task_id == root_task_id)
        ).all()
        task_ids = {t.id for t in tasks}
        if not task_ids:
            return 0
        return self.session.scalar(
            select(func.count()).select_from(InvocationRow).where(
                InvocationRow.task_id.in_(task_ids)
            )
        )

    def _tree_stopped(self, root_task_id: str) -> bool:
        # A stop is recorded as an event; in this kernel we treat a STOP event
        # as the stop flag. The worker checks this before consuming.
        event = self.session.scalars(
            select(EventRow)
            .where(EventRow.root_task_id == root_task_id, EventRow.type == "tree_stop")
            .order_by(EventRow.seq.desc())
            .limit(1)
        ).first()
        return event is not None

    # ------------------------------------------------------------------ #
    # The atomic consume gate
    # ------------------------------------------------------------------ #

    def consume(self, invocation_id: str) -> dict[str, Any]:
        """Atomically validate + consume approval, reserve a slot.

        Returns a dict the caller uses to start the adapter. Raises
        :class:`QuotaError` (and rolls back) on any failure, so the adapter is
        never started for an unapproved/expired/denied/quota-exceeded call.
        """
        inv = self.session.get(InvocationRow, invocation_id)
        if inv is None:
            raise QuotaError("no_invocation", f"invocation {invocation_id} not found")

        # 1. Immutable snapshot verification
        task = self.session.get(TaskRow, inv.task_id)
        if task is None:
            raise QuotaError("no_task", f"task {inv.task_id} not found")
        current_snapshot = InvocationSnapshot(**inv.snapshot)
        if current_snapshot.payload_hash() != inv.payload_hash:
            inv.status = InvocationStatus.INVALID.value
            self.session.flush()
            raise QuotaError(
                "snapshot_changed",
                f"invocation {invocation_id} payload changed; approval is void",
            )
        self._maybe_invalidate_if_task_changed(task)

        # 2. Find an approval and validate consumability
        appr = self._find_consumable_approval(invocation_id)
        if appr is None:
            raise QuotaError("no_approval", f"no consumable approval for {invocation_id}")
        # Re-check at the execution gate even if the lookup changes later.
        # Pending and terminal approvals must never reserve a slot or start work.
        if appr.status != ApprovalStatus.APPROVED.value:
            raise QuotaError(
                "not_approved", f"approval {appr.id} is {appr.status}, not approved"
            )

        # 3. Approval bound to the same hash
        if appr.payload_hash != inv.payload_hash:
            raise QuotaError(
                "approval_hash_mismatch",
                f"approval {appr.id} bound to a different payload",
            )

        # 4. Expiry
        if appr.expires_at is not None and appr.expires_at < now():
            appr.status = ApprovalStatus.EXPIRED.value
            self.session.flush()
            raise QuotaError("expired", f"approval {appr.id} expired")

        # 5. Stop state
        if self._tree_stopped(task.root_task_id):
            raise QuotaError("tree_stopped", f"tree {task.root_task_id} is stopped")

        # 6. Quota evaluation
        quota = self.evaluate_quota(inv)
        policy = self._quotas(inv)
        is_subagent = quota["is_subagent"]
        if is_subagent:
            if quota["launched_subagents"] >= policy.max_subagents_total:
                raise QuotaError(
                    "max_subagents_total",
                    f"sub-agent total {quota['launched_subagents']} already at "
                    f"limit {policy.max_subagents_total}",
                )
            if quota["concurrent"] >= policy.max_subagents_concurrent:
                raise QuotaError(
                    "max_subagents_concurrent",
                    f"concurrent {quota['concurrent']} already at limit "
                    f"{policy.max_subagents_concurrent}",
                )
        if quota["total_invocations"] >= policy.max_agent_invocations:
            raise QuotaError(
                "max_agent_invocations",
                f"total invocations {quota['total_invocations']} at limit "
                f"{policy.max_agent_invocations}",
            )

        # 7. Atomic consume + reserve slot
        # 7. Atomic consume + reserve slot.
        #
        # The approval is consumed with a compare-and-swap UPDATE guarded on
        # status='approved'. Two workers racing on the same approval therefore
        # cannot both win: the loser's UPDATE affects 0 rows and is rejected
        # here, before any quota reservation or adapter start. This is what
        # makes "同一审批并发消费最多启动一次" hold under real concurrency,
        # rather than relying on a read-then-write race.
        cas = self.session.execute(
            update(ApprovalRow)
            .where(
                ApprovalRow.id == appr.id,
                ApprovalRow.status == ApprovalStatus.APPROVED.value,
            )
            .values(status=ApprovalStatus.CONSUMED.value, consumed_at=now())
            .execution_options(synchronize_session=False)
        )
        if cas.rowcount != 1:
            # Lost the race (or the status changed underneath us).
            self.session.rollback()
            raise QuotaError(
                "already_consumed",
                f"approval {appr.id} was consumed concurrently",
            )

        reservation = QuotaReservationRow(
            id=new_id("res"),
            invocation_id=invocation_id,
            root_task_id=task.root_task_id,
        )
        self.session.add(reservation)
        inv.status = InvocationStatus.STARTING.value
        self.session.add(
            EventRow(
                root_task_id=task.root_task_id,
                seq=self._next_seq(task.root_task_id),
                type="start",
                payload={"invocation_id": invocation_id},
            )
        )
        # Re-read the consumed approval so the returned plan reflects committed
        # state rather than the pre-CAS object.
        self.session.flush()
        return {
            "invocation_id": invocation_id,
            "root_task_id": task.root_task_id,
            "approval_id": appr.id,
            "snapshot": inv.snapshot,
        }

    # ------------------------------------------------------------------ #
    # Release a concurrent slot when an invocation finishes
    # ------------------------------------------------------------------ #

    def release(self, invocation_id: str, status: InvocationStatus) -> None:
        """Mark an invocation terminal and release its concurrent slot."""
        inv = self.session.get(InvocationRow, invocation_id)
        if inv is None:
            raise QuotaError("no_invocation", f"invocation {invocation_id} not found")
        inv.status = status.value
        inv.finished_at = now()
        res = self.session.scalars(
            select(QuotaReservationRow).where(
                QuotaReservationRow.invocation_id == invocation_id,
                QuotaReservationRow.released_at.is_(None),
            )
        ).first()
        if res is not None:
            res.released_at = now()
        self.session.flush()

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #

    def _find_consumable_approval(self, invocation_id: str) -> Optional[ApprovalRow]:
        # Creating an approval does not grant consent; only approval by the
        # user makes it eligible for consumption.
        appr = self.session.scalars(
            select(ApprovalRow)
            .where(
                ApprovalRow.invocation_id == invocation_id,
                ApprovalRow.status == ApprovalStatus.APPROVED.value,
            )
            .order_by(ApprovalRow.created_at)
            .limit(1)
        ).first()
        return appr

    def _maybe_invalidate_if_task_changed(self, task: TaskRow) -> None:
        """No-op placeholder for a real implementation.

        A real deployment would check the policy version / adapter version vs.
        the snapshot. P2 keeps the invocation snapshot as the source of truth.
        """

    def _next_seq(self, root_task_id: str) -> int:
        current = self.session.scalar(
            select(func.coalesce(func.max(EventRow.seq), 0)).where(
                EventRow.root_task_id == root_task_id
            )
        )
        return (current or 0) + 1

    def stop_tree(self, root_task_id: str) -> None:
        self.session.add(
            EventRow(
                root_task_id=root_task_id,
                seq=self._next_seq(root_task_id),
                type="tree_stop",
                payload={},
            )
        )
        self.session.flush()
