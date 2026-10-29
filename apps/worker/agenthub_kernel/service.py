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
from agenthub_kernel.budget import admit as admit_budget
from agenthub_kernel.db import (
    ApprovalRow,
    ChatMessageRow,
    EventRow,
    InvocationRow,
    PolicyVersionRow,
    QuotaReservationRow,
    StageGrantRow,
    StageRunRow,
    TaskRow,
    TreeQuotaRow,
)
from agenthub.stage import STAGE_RUNNABLE
from agenthub_kernel.taskstatus import derive_task_status

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
            effective_policy_version = policy.version
        else:
            parent = self.session.get(TaskRow, parent_task_id)
            if parent is None:
                raise QuotaError("no_parent", f"parent task {parent_task_id} not found")
            root_task_id = parent.root_task_id
            depth = parent.depth + 1
            # Inherit the tree's CURRENT policy version, so a call created after
            # a policy update is not immediately invalidated by that update.
            root = self.session.get(TaskRow, root_task_id)
            effective = Policy(**{k: v for k, v in (root.policy or {}).items() if k != "version"})
            if not effective.collaboration_enabled:
                raise QuotaError("collaboration_disabled", "Hub 子任务协作扩展未启用。")
            policy = effective
            effective_policy_version = int(
                (root.policy_version if root is not None else parent.policy_version) or 1
            )
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
            policy_version=effective_policy_version,
            # Store the effective policy on the root node so the worker can
            # enforce the same tree-scoped limits without being told the policy
            # out-of-band (AGENT_HUB_PLAN §5: policy_versions are per-tree).
            policy=policy.as_dict() if depth == 0 else None,
        )
        self.session.add(task)
        self.session.flush()
        if depth == 0:
            # Initialise the atomic quota counters for this tree.
            self.session.add(
                TreeQuotaRow(
                    root_task_id=root_task_id,
                    launched_total=0,
                    concurrent=0,
                    total_invocations=0,
                )
            )
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
        from agenthub_kernel.desktop import workspace_permissions
        from dataclasses import replace
        try:
            workspace = workspace_permissions(snapshot.agent_id)
        except ValueError as exc:
            raise QuotaError("workspace_required", str(exc)) from exc
        if workspace:
            snapshot = replace(snapshot, permissions={**snapshot.permissions, **workspace})
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
        """Create a pending approval bound to the invocation's current hash.

        Also moves the invocation to ``awaiting_approval``: once an approval
        request exists, the call is waiting on the user (AGENT_HUB_PLAN §5
        state chain ``draft -> awaiting_approval -> queued -> ...``).
        """
        inv = self.session.get(InvocationRow, invocation_id)
        if inv is None:
            raise QuotaError("no_invocation", f"invocation {invocation_id} not found")
        appr = ApprovalRow(
            id=new_id("appr"),
            invocation_id=invocation_id,
            payload_hash=inv.payload_hash,
            user_id=user_id,
            status=ApprovalStatus.PENDING.value,
            policy_version=int(inv.snapshot.get("policy_version") or self.default_policy.version),
            expires_at=now() + dt.timedelta(seconds=ttl_seconds),
        )
        self.session.add(appr)
        if inv.status in (
            InvocationStatus.DRAFT.value,
            InvocationStatus.AWAITING_APPROVAL.value,
        ):
            self._set_invocation_status(inv, InvocationStatus.AWAITING_APPROVAL)
        self.session.flush()
        return appr

    def transition_invocation(self, invocation_id: str, status: InvocationStatus) -> None:
        from agenthub.contracts import TERMINAL_STATUSES

        inv = self.session.get(InvocationRow, invocation_id)
        if inv is None:
            raise QuotaError("no_invocation", f"invocation {invocation_id} not found")
        if inv.status in TERMINAL_STATUSES:
            return
        self._set_invocation_status(inv, status)
        if status is InvocationStatus.RUNNING:
            inv.started_at = now()

    # ------------------------------------------------------------------ #
    # Task status: derived from the calls, never left stale
    # ------------------------------------------------------------------ #

    def _set_invocation_status(self, inv: InvocationRow, status: Any) -> None:
        """Set an invocation status and immediately re-derive its task status.

        Every status write goes through here, so ``tasks.status`` cannot drift
        from the calls again (it used to stay ``draft`` forever).
        """
        inv.status = (
            status.value if isinstance(status, InvocationStatus) else str(status)
        )
        self.sync_task_status(inv.task_id)

    def sync_task_status(self, task_id: str) -> str:
        """Recompute and store one task's derived status. Returns it."""
        task = self.session.get(TaskRow, task_id)
        if task is None:
            return ""
        statuses = self.session.scalars(
            select(InvocationRow.status).where(InvocationRow.task_id == task_id)
        ).all()
        derived = derive_task_status(statuses)
        if task.status != derived:
            task.status = derived
        return derived

    def approve(self, approval_id: str, *, supervised_consent: bool = False) -> ApprovalRow:
        """Mark pending->approved. Only a valid pending approval can be approved.

        The invocation moves to ``queued``: the user has consented, so the call
        is now waiting for a Worker to pick it up (and for a quota slot).
        """
        appr = self.session.get(ApprovalRow, approval_id)
        if appr is None:
            raise QuotaError("no_approval", f"approval {approval_id} not found")
        invocation = self.session.get(InvocationRow, appr.invocation_id)
        if invocation is not None and invocation.snapshot.get("execution_mode") == "supervised":
            if supervised_consent is not True:
                raise QuotaError("boundary_consent_required", "请明确同意用户监督边界：原生派生与远端停止不保证受控。")
        if appr.status != ApprovalStatus.PENDING.value:
            raise QuotaError("not_pending", f"approval {approval_id} is {appr.status}")
        appr.status = ApprovalStatus.APPROVED.value
        appr.approved_at = now()
        inv = self.session.get(InvocationRow, appr.invocation_id)
        if inv is not None and inv.status not in (
            InvocationStatus.STARTING.value,
            InvocationStatus.RUNNING.value,
        ):
            self._set_invocation_status(inv, InvocationStatus.QUEUED)
        self.session.flush()
        return appr

    def reject(self, approval_id: str) -> ApprovalRow:
        appr = self.session.get(ApprovalRow, approval_id)
        if appr is None:
            raise QuotaError("no_approval", f"approval {approval_id} not found")
        if appr.status != ApprovalStatus.PENDING.value:
            raise QuotaError("not_pending", f"approval {approval_id} is {appr.status}")
        appr.status = ApprovalStatus.REJECTED.value
        inv = self.session.get(InvocationRow, appr.invocation_id)
        if inv is not None and inv.status not in (
            InvocationStatus.STARTING.value,
            InvocationStatus.RUNNING.value,
        ):
            self._set_invocation_status(inv, InvocationStatus.REJECTED)
        self.session.flush()
        return appr

    def revoke(self, approval_id: str) -> ApprovalRow:
        """Revoke a not-yet-consumed approval (pending or approved).

        The invocation returns to ``draft`` because, with the approval void, the
        call is once again awaiting a user decision — and a fresh approval is
        required.
        """
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
        inv = self.session.get(InvocationRow, appr.invocation_id)
        if inv is not None and inv.status in (
            InvocationStatus.DRAFT.value,
            InvocationStatus.AWAITING_APPROVAL.value,
            InvocationStatus.QUEUED.value,
        ):
            self._set_invocation_status(inv, InvocationStatus.DRAFT)
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

    def consume(self, invocation_id: str, *, metered: Optional[bool] = None) -> dict[str, Any]:
        """Atomically validate + consume approval, reserve a slot.

        Returns a dict the caller uses to start the adapter. Raises
        :class:`QuotaError` (and rolls back) on any failure, so the adapter is
        never started for an unapproved/expired/denied/quota-exceeded call.

        ``metered``: whether the serving adapter reports usage (the Worker
        knows the adapter; this service does not). Only token budgets read it;
        ``None`` counts as not metered.
        """
        inv = self.session.get(InvocationRow, invocation_id)
        if inv is None:
            raise QuotaError("no_invocation", f"invocation {invocation_id} not found")

        # 1. Immutable snapshot verification
        task = self.session.get(TaskRow, inv.task_id)
        if task is None:
            raise QuotaError("no_task", f"task {inv.task_id} not found")
        try:
            current_snapshot = InvocationSnapshot(**inv.snapshot)
        except (TypeError, ValueError):
            # A stored snapshot that no longer validates (e.g. a context
            # preview that does not describe its input) is void like a changed one.
            self._set_invocation_status(inv, InvocationStatus.INVALID)
            self.session.flush()
            raise QuotaError("snapshot_invalid",
                             f"invocation {invocation_id} snapshot does not validate; approval is void")
        if current_snapshot.payload_hash() != inv.payload_hash:
            self._set_invocation_status(inv, InvocationStatus.INVALID)
            self.session.flush()
            raise QuotaError(
                "snapshot_changed",
                f"invocation {invocation_id} payload changed; approval is void",
            )

        # 1b. Policy-version check: if the tree's policy changed after this
        # invocation was approved, the approval no longer describes the terms
        # the user agreed to (AGENT_HUB_PLAN §4.1 "快照变化，审批立即失效").
        # This also closes the race where an update commits between the
        # worker's pick and its consume.
        root = self.session.get(TaskRow, task.root_task_id)
        if root is not None:
            snapshot_policy_version = int(inv.snapshot.get("policy_version") or 0)
            if int(root.policy_version or 0) != snapshot_policy_version:
                self._set_invocation_status(inv, InvocationStatus.INVALID)
                self.session.flush()
                raise QuotaError(
                    "policy_changed",
                    f"invocation {invocation_id} was approved under policy "
                    f"v{snapshot_policy_version} but the tree is now at "
                    f"v{root.policy_version}; approval is void",
                )

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

        # 2b. An approval the scheduler gave under a stage's rules (P4.6) holds
        # only while that stage is active: pausing, stopping or ending the
        # stage stops its approved calls from starting, here at the gate.
        grant = self.session.get(StageGrantRow, appr.id)
        if grant is not None:
            stage = self.session.get(StageRunRow, grant.stage_id)
            if (stage is None or stage.status not in STAGE_RUNNABLE
                    or (stage.expires_at is not None and stage.expires_at < now())):
                raise QuotaError("stage_not_active",
                                 f"approval {appr.id} was given under stage {grant.stage_id}, which is "
                                 f"{stage.status if stage is not None else 'missing'}")
            from agenthub.stage import rules_of
            rules = rules_of(stage.rules)
            if (rules.rules_hash() != stage.rules_hash or grant.invocation_id != inv.id
                    or stage.root_task_id != task.root_task_id
                    or inv.snapshot.get("agent_id") not in rules.agents
                    or inv.snapshot.get("execution_mode", "strict") != stage.execution_mode):
                raise QuotaError("stage_rule_mismatch", "call is outside the authorized stage rules")

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

        # 5. Stop / pause state
        if self._tree_stopped(task.root_task_id):
            raise QuotaError("tree_stopped", f"tree {task.root_task_id} is stopped")
        if self._tree_paused(task.root_task_id):
            raise QuotaError("tree_paused", f"tree {task.root_task_id} is paused")

        # 5b. The invocation must be in the queued state. An approval is what
        # moves it there, so this is defence in depth against any path that
        # approved without queueing (or replayed an old row).
        if inv.status not in (
            InvocationStatus.QUEUED.value,
            InvocationStatus.AWAITING_APPROVAL.value,
        ):
            raise QuotaError(
                "not_queued",
                f"invocation {invocation_id} is {inv.status}, not queued for execution",
            )

        # 6. Atomic quota reservation.
        #
        # A read-then-write check races: with several distinct invocations in
        # flight, every worker can read the same "0 launched / 0 running" state
        # and then all proceed, breaking the whole-tree cap. We therefore take
        # the slot with a single conditional UPDATE (compare-and-swap) whose
        # WHERE clause encodes the limits. Exactly one racer can win the last
        # slot; losers see rowcount 0 and are refused.
        policy = self._quotas(inv)
        if task.depth > 0 and not policy.collaboration_enabled:
            raise QuotaError("collaboration_disabled", "Hub 子任务协作扩展未启用。")
        is_subagent = task.depth >= 1
        if not self._reserve_quota_atomic(
            root_task_id=task.root_task_id,
            is_subagent=is_subagent,
            policy=policy,
        ):
            # Re-read for a precise refusal reason (diagnostics only).
            quota = self.evaluate_quota(inv)
            if policy.max_agent_invocations and quota["total_invocations"] >= policy.max_agent_invocations:
                raise QuotaError(
                    "max_agent_invocations",
                    f"total invocations {quota['total_invocations']} at limit "
                    f"{policy.max_agent_invocations}",
                )
            if is_subagent and policy.max_subagents_total and quota["launched_subagents"] >= policy.max_subagents_total:
                raise QuotaError(
                    "max_subagents_total",
                    f"sub-agent total {quota['launched_subagents']} already at "
                    f"limit {policy.max_subagents_total}",
                )
            if is_subagent and quota["concurrent"] >= policy.max_subagents_concurrent:
                raise QuotaError(
                    "max_subagents_concurrent",
                    f"concurrent {quota['concurrent']} already at limit "
                    f"{policy.max_subagents_concurrent}",
                )
            # Lost an optimisation race; report the binding limit generically.
            raise QuotaError(
                "quota_exceeded",
                f"quota reservation failed for tree {task.root_task_id}",
            )

        # 6b. Token budget (T-1c): decide and reserve with a CAS of its own.
        # The slot above is already reserved in this transaction, and the
        # Worker commits after a QuotaError to keep denial side effects - so a
        # budget refusal rolls back first: no slot, no reservation, approval
        # untouched. Nothing before this point needs to persist.
        decision = admit_budget(self.session, inv, task.root_task_id, policy, metered=metered)
        if decision is not None and not decision.allowed:
            self.session.rollback()
            raise QuotaError(decision.code, decision.message or decision.code)
        from agenthub_kernel.model_budget import admit as admit_model_budget
        decision = admit_model_budget(self.session, inv, task.root_task_id, policy, metered=metered)
        if decision is not None and not decision.allowed:
            self.session.rollback()
            raise QuotaError(decision.code, decision.message or decision.code)

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
        self._set_invocation_status(inv, InvocationStatus.STARTING)
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
            # Proof that THIS call consumed an approved approval under a reserved
            # quota slot. Adapters that launch a real process can require it, so
            # reaching a launch without the gate becomes a loud error.
            "gate_token": f"consumed:{appr.id}:{invocation_id}",
        }

    # ------------------------------------------------------------------ #
    # Release a concurrent slot when an invocation finishes
    # ------------------------------------------------------------------ #

    def release(self, invocation_id: str, status: InvocationStatus) -> None:
        """Mark an invocation terminal and release its concurrent slot.

        Idempotent: the concurrent counter is decremented only for reservation
        rows that THIS call atomically claimed (compare-and-swap on
        ``released_at IS NULL``). Calling release twice, or two workers racing
        to release the same invocation, therefore frees exactly one slot.
        """
        inv = self.session.get(InvocationRow, invocation_id)
        if inv is None:
            raise QuotaError("no_invocation", f"invocation {invocation_id} not found")
        task = self.session.get(TaskRow, inv.task_id)
        if inv.status not in (
            InvocationStatus.SUCCEEDED.value,
            InvocationStatus.FAILED.value,
            InvocationStatus.CANCELED.value,
            InvocationStatus.TIMED_OUT.value,
        ):
            self._set_invocation_status(inv, status)
        inv.finished_at = now()

        candidates = self.session.scalars(
            select(QuotaReservationRow.id).where(
                QuotaReservationRow.invocation_id == invocation_id,
                QuotaReservationRow.released_at.is_(None),
            )
        ).all()

        released = 0
        for res_id in candidates:
            cas = self.session.execute(
                update(QuotaReservationRow)
                .where(
                    QuotaReservationRow.id == res_id,
                    QuotaReservationRow.released_at.is_(None),
                )
                .values(released_at=now())
                .execution_options(synchronize_session=False)
            )
            released += int(cas.rowcount or 0)

        # One decrement per reservation actually claimed; cumulative
        # launched_total is intentionally left untouched.
        if task is not None and task.depth >= 1:
            for _ in range(released):
                self._release_quota_atomic(task.root_task_id)
        self.session.flush()

    def mark_unknown(self, invocation_id: str, *, reason: str = "") -> None:
        """Record that an invocation's true state could not be confirmed.

        The concurrent slot is deliberately NOT released: we cannot prove the
        external call has stopped, and releasing it would let another call take
        a slot while the old one may still be running (AGENT_HUB_PLAN §4.1
        "执行状态未知...不能盲目重试").
        """
        inv = self.session.get(InvocationRow, invocation_id)
        if inv is None:
            raise QuotaError("no_invocation", f"invocation {invocation_id} not found")
        self._set_invocation_status(inv, InvocationStatus.UNKNOWN)
        inv.error = reason or inv.error or (
            "state could not be confirmed after recovery; not restarted"
        )
        self.session.flush()

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #

    def _reserve_quota_atomic(
        self,
        *,
        root_task_id: str,
        is_subagent: bool,
        policy: Policy,
    ) -> bool:
        """Reserve a quota slot with a compare-and-swap UPDATE.

        The WHERE clause encodes every limit, so the reservation is atomic with
        respect to the check: no two racers can both win the last slot.

        * ``total_invocations`` is always incremented (counts main + sub).
        * ``launched_total`` / ``concurrent`` are incremented only for sub-agents;
          ``launched_total`` is cumulative and never returned.

        Returns True when the slot was reserved, False when a limit blocked it.
        """
        row = self.session.get(TreeQuotaRow, root_task_id)
        if row is None:
            # Trees created before this table existed: create lazily.
            row = TreeQuotaRow(
                root_task_id=root_task_id,
                launched_total=0,
                concurrent=0,
                total_invocations=0,
            )
            self.session.add(row)
            self.session.flush()

        conditions = [TreeQuotaRow.root_task_id == root_task_id]
        if policy.max_agent_invocations:
            conditions.append(TreeQuotaRow.total_invocations < policy.max_agent_invocations)
        values: dict[str, Any] = {
            "total_invocations": TreeQuotaRow.total_invocations + 1
        }
        if is_subagent:
            if policy.max_subagents_total:
                conditions.append(TreeQuotaRow.launched_total < policy.max_subagents_total)
            conditions.append(
                TreeQuotaRow.concurrent < policy.max_subagents_concurrent
            )
            values["launched_total"] = TreeQuotaRow.launched_total + 1
            values["concurrent"] = TreeQuotaRow.concurrent + 1

        result = self.session.execute(
            update(TreeQuotaRow)
            .where(*conditions)
            .values(**values)
            .execution_options(synchronize_session=False)
        )
        self.session.flush()
        return result.rowcount == 1

    def _release_quota_atomic(self, root_task_id: str) -> None:
        """Decrement the concurrent counter (cumulative total is preserved)."""
        self.session.execute(
            update(TreeQuotaRow)
            .where(TreeQuotaRow.root_task_id == root_task_id)
            .values(concurrent=func.max(TreeQuotaRow.concurrent - 1, 0))
            .execution_options(synchronize_session=False)
        )
        self.session.flush()

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
        """Deprecated no-op.

        Policy-version invalidation is now enforced directly in :meth:`consume`
        (step 1b) so that the check cannot be forgotten by a caller.
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

    def request_stop(
        self, root_task_id: str, *, requested_by: str = "local-user"
    ) -> dict[str, Any]:
        """Persist a stop request for a tree and report what is running.

        Persisting the request is what makes the stop durable: a Worker that
        restarts afterwards still sees the tree as stopped and will not start
        anything new. Actually cancelling in-flight calls is the Worker's job
        (it owns the adapter), and is driven from the returned running list.
        """
        if self.session.get(TaskRow, root_task_id) is None:
            raise QuotaError("no_task", f"root task {root_task_id} not found")

        already = self._tree_stopped(root_task_id)
        self.session.add(
            EventRow(
                root_task_id=root_task_id,
                seq=self._next_seq(root_task_id),
                type="tree_stop",
                payload={"requested_by": requested_by, "repeat": already},
            )
        )
        self.session.flush()

        running = self.session.scalars(
            select(InvocationRow)
            .join(TaskRow, TaskRow.id == InvocationRow.task_id)
            .where(
                TaskRow.root_task_id == root_task_id,
                InvocationRow.status.in_(
                    [InvocationStatus.STARTING.value, InvocationStatus.RUNNING.value]
                ),
            )
        ).all()
        return {
            "root_task_id": root_task_id,
            "already_stopped": already,
            "running": [
                {"invocation_id": r.id, "status": r.status} for r in running
            ],
        }

    def list_recoverable(self) -> list[dict[str, Any]]:
        """Invocation rows whose state must be reconciled after a restart.
        ``starting`` and ``running`` are the states where the kernel had (or may
        have had) handed work to an adapter. Everything else is already settled
        and is left alone.
        """
        rows = self.session.scalars(
            select(InvocationRow).where(
                InvocationRow.status.in_(
                    [InvocationStatus.STARTING.value, InvocationStatus.RUNNING.value]
                )
            )
        ).all()
        out = []
        for r in rows:
            task = self.session.get(TaskRow, r.task_id)
            out.append(
                {
                    "invocation_id": r.id,
                    "status": r.status,
                    "adapter_invocation_id": r.adapter_invocation_id,
                    "root_task_id": task.root_task_id if task is not None else None,
                    "depth": task.depth if task is not None else None,
                    "task_id": r.task_id,
                }
            )
        return out

    # ------------------------------------------------------------------ #
    # Pause / resume (blocks new scheduling without cancelling anything)
    # ------------------------------------------------------------------ #

    def pause_tree(self, root_task_id: str, *, requested_by: str = "local-user") -> dict[str, Any]:
        """Block further scheduling for a tree. Running calls are NOT touched."""
        if self.session.get(TaskRow, root_task_id) is None:
            raise QuotaError("no_task", f"root task {root_task_id} not found")
        self.session.add(
            EventRow(
                root_task_id=root_task_id,
                seq=self._next_seq(root_task_id),
                type="tree_pause",
                payload={"requested_by": requested_by},
            )
        )
        self.session.flush()
        return {"root_task_id": root_task_id, "paused": True}

    def resume_tree(self, root_task_id: str, *, requested_by: str = "local-user") -> dict[str, Any]:
        """Lift a pause. Does not resurrect expired/revoked approvals."""
        if self.session.get(TaskRow, root_task_id) is None:
            raise QuotaError("no_task", f"root task {root_task_id} not found")
        self.session.add(
            EventRow(
                root_task_id=root_task_id,
                seq=self._next_seq(root_task_id),
                type="tree_resume",
                payload={"requested_by": requested_by},
            )
        )
        self.session.flush()
        return {"root_task_id": root_task_id, "paused": False}

    def _tree_paused(self, root_task_id: str) -> bool:
        """Paused when the most recent pause/resume event is a pause."""
        event = self.session.scalars(
            select(EventRow)
            .where(
                EventRow.root_task_id == root_task_id,
                EventRow.type.in_(["tree_pause", "tree_resume"]),
            )
            .order_by(EventRow.seq.desc())
            .limit(1)
        ).first()
        return event is not None and event.type == "tree_pause"

    # ------------------------------------------------------------------ #
    # Run-time limit enforcement (timed_out)
    # ------------------------------------------------------------------ #

    def list_overdue(
        self, *, now_ts: Optional[dt.datetime] = None
    ) -> list[dict[str, Any]]:
        """Running/starting invocations that have exceeded the run-time limit.

        The limit comes from the tree's effective policy
        (``run_time_limit_seconds``). Returns rows the Worker should time out.
        """
        current = now_ts or now()
        rows = self.session.scalars(
            select(InvocationRow).where(
                InvocationRow.status.in_(
                    [InvocationStatus.STARTING.value, InvocationStatus.RUNNING.value]
                )
            )
        ).all()
        overdue = []
        for r in rows:
            if r.started_at is None:
                continue
            task = self.session.get(TaskRow, r.task_id)
            if task is None:
                continue
            policy = self._quotas(r)
            from agenthub.workspace import execution_timeout
            limit = execution_timeout(InvocationSnapshot(**r.snapshot), policy.run_time_limit_seconds)
            if limit == 0:
                continue
            deadline = r.started_at + dt.timedelta(seconds=limit)
            if current >= deadline:
                overdue.append(
                    {
                        "invocation_id": r.id,
                        "root_task_id": task.root_task_id,
                        "started_at": r.started_at,
                        "deadline": deadline,
                        "limit_seconds": limit,
                    }
                )
        return overdue

    def mark_timed_out(self, invocation_id: str) -> None:
        """Settle an overdue invocation as timed_out and free its slot once."""
        self.release(invocation_id, InvocationStatus.TIMED_OUT)
        inv = self.session.get(InvocationRow, invocation_id)
        if inv is not None:
            inv.error = "exceeded run_time_limit_seconds"

    # ------------------------------------------------------------------ #
    # Unknown reconciliation (user adjudication)
    # ------------------------------------------------------------------ #

    def reconcile_unknown(
        self,
        invocation_id: str,
        *,
        action: str,
        outcome: Optional[str] = None,
        actor: str = "local-user",
        confirmed_stopped: bool = False,
        stop_evidence: str = "",
    ) -> dict[str, Any]:
        """Let the USER settle or retry an ``unknown`` invocation.

        ``unknown`` means the kernel could not confirm the true state, so the
        slot is held deliberately. Only a human may resolve it:

        * ``action="settle"``: the user states what actually happened. The
          invocation is moved to ``outcome`` and its slot is released
          (idempotently — releasing twice frees one slot).
        * ``action="retry"``: the call is retried. A retry is a NEW call, so it
          needs a NEW approval. Crucially, the old attempt is **not** silently
          written off: ``confirmed_stopped`` must be true (the caller has
          established that the previous execution is no longer running). Until
          then the retry is REFUSED with ``not_stopped`` and the old call keeps
          holding its quota slot — otherwise a second call could start while the
          first may still be running.
        """
        inv = self.session.get(InvocationRow, invocation_id)
        if inv is None:
            raise QuotaError("no_invocation", f"invocation {invocation_id} not found")
        if inv.status != InvocationStatus.UNKNOWN.value:
            raise QuotaError(
                "not_unknown",
                f"invocation {invocation_id} is {inv.status}, not unknown",
            )

        if action == "settle":
            allowed = {
                "succeeded": InvocationStatus.SUCCEEDED,
                "failed": InvocationStatus.FAILED,
                "canceled": InvocationStatus.CANCELED,
                "timed_out": InvocationStatus.TIMED_OUT,
            }
            if outcome not in allowed:
                raise QuotaError(
                    "bad_outcome",
                    f"outcome must be one of {sorted(allowed)}, got {outcome!r}",
                )
            # A human statement about the outcome IS a confirmation that the
            # execution is over, so the slot may be released here.
            self.release(invocation_id, allowed[outcome])
            self.session.add(
                EventRow(
                    root_task_id=self._root_of(inv),
                    seq=self._next_seq(self._root_of(inv)),
                    type="unknown_settled",
                    payload={
                        "invocation_id": invocation_id,
                        "outcome": outcome,
                        "actor": actor,
                    },
                )
            )
            self.session.flush()
            return {
                "invocation_id": invocation_id,
                "action": "settle",
                "outcome": outcome,
                "retry_invocation_id": None,
                "retry_approval_id": None,
            }

        if action == "retry":
            if not confirmed_stopped:
                # Do NOT touch the old invocation and do NOT release its slot.
                raise QuotaError(
                    "not_stopped",
                    "cannot retry before the previous execution is confirmed "
                    f"stopped ({stop_evidence or 'no confirmation available'}); "
                    "settle it first if you know the outcome",
                )

            task = self.session.get(TaskRow, inv.task_id)
            if task is None:
                raise QuotaError("no_task", f"task {inv.task_id} not found")
            root_task_id = task.root_task_id

            # Now, and only now, settle the old attempt and free its slot once.
            self.release(invocation_id, InvocationStatus.FAILED)
            inv.error = f"superseded by user-initiated retry ({stop_evidence})"

            # A retry is a NEW invocation with a NEW pending approval. The user
            # must approve it again; nothing is auto-started.
            root = self.session.get(TaskRow, root_task_id)
            policy_version = int(
                (root.policy_version if root is not None else task.policy_version) or 1
            )
            # A task can now contain several independent messages/agents.
            # Retrying task.goal would resend the FIRST message, not this call.
            snapshot = InvocationSnapshot(**{
                **inv.snapshot, "policy_version": policy_version,
            })
            new_inv = InvocationRow(
                id=new_id("inv"),
                task_id=task.id,
                payload_hash=snapshot.payload_hash(),
                snapshot=snapshot.to_dict(),
                status=InvocationStatus.AWAITING_APPROVAL.value,
            )
            self.session.add(new_inv)
            self.session.flush()

            appr = ApprovalRow(
                id=new_id("appr"),
                invocation_id=new_inv.id,
                payload_hash=new_inv.payload_hash,
                user_id=actor,
                status=ApprovalStatus.PENDING.value,
                policy_version=policy_version,
                expires_at=now() + dt.timedelta(
                    seconds=max(self._quotas(new_inv).run_time_limit_seconds, 300)
                ),
            )
            self.session.add(appr)
            source_message = self.session.scalar(select(ChatMessageRow).where(
                ChatMessageRow.invocation_id == invocation_id,
                ChatMessageRow.role == "user",
            ))
            if source_message is not None:
                self.session.add(ChatMessageRow(
                    conversation_id=source_message.conversation_id, role="user",
                    text=snapshot.task_input, invocation_id=new_inv.id,
                ))
            self.sync_task_status(task.id)
            self.session.add(
                EventRow(
                    root_task_id=root_task_id,
                    seq=self._next_seq(root_task_id),
                    type="retry_requested",
                    payload={
                        "superseded_invocation_id": invocation_id,
                        "retry_invocation_id": new_inv.id,
                        "retry_approval_id": appr.id,
                        "actor": actor,
                        "stop_evidence": stop_evidence,
                    },
                )
            )
            self.session.flush()
            return {
                "invocation_id": invocation_id,
                "action": "retry",
                "outcome": None,
                "retry_invocation_id": new_inv.id,
                "retry_approval_id": appr.id,
                "requires_approval": True,
            }

        raise QuotaError(
            "bad_action", f"action must be 'settle' or 'retry', got {action!r}"
        )

    def _root_of(self, inv: InvocationRow) -> str:
        task = self.session.get(TaskRow, inv.task_id)
        return task.root_task_id if task is not None else "unknown"

    # ------------------------------------------------------------------ #
    # Policy updates (persisted, versioned, invalidating)
    # ------------------------------------------------------------------ #

    def update_policy(
        self,
        root_task_id: str,
        new_policy: Policy,
        *,
        updated_by: str = "local-user",
    ) -> dict[str, Any]:
        """Persist a new policy version for a whole root task tree.

        Guarantees required by AGENT_HUB_PLAN §4.2:

        * The policy applies to the **entire tree** (stored on the root row).
        * The version **increments** on every update; the previous version is
          recorded as inactive so the audit trail is preserved.
        * Outstanding **unconsumed** approvals become void, because the terms the
          user agreed to no longer hold. Already-consumed approvals and running
          calls are left alone.
        * Lowering a limit **never** resets the cumulative launched-sub-agent
          count and **never** auto-cancels a running call.

        ``new_policy`` is validated by :class:`Policy` before anything is
        written; an invalid policy raises ``ValueError`` and the caller's
        transaction rolls back with no partial write.
        """
        root = self.session.get(TaskRow, root_task_id)
        if root is None:
            raise QuotaError("no_task", f"root task {root_task_id} not found")
        if root.parent_task_id is not None or root.depth != 0:
            # A policy lives on the root; refuse to treat a subtree node as one.
            root = self.session.get(TaskRow, root.root_task_id)
            if root is None:
                raise QuotaError("no_task", f"root task {root_task_id} not found")

        new_version = int(root.policy_version or 0) + 1

        # Record the new version; deactivate the previous active version.
        for prev in self.session.scalars(
            select(PolicyVersionRow).where(
                PolicyVersionRow.root_task_id == root.root_task_id,
                PolicyVersionRow.active.is_(True),
            )
        ).all():
            prev.active = False

        payload = new_policy.as_dict()
        payload["version"] = new_version
        self.session.add(
            PolicyVersionRow(
                id=new_id("pol"),
                version=new_version,
                root_task_id=root.root_task_id,
                payload=payload,
                active=True,
            )
        )

        # Effective policy + version on the root row (what the worker reads).
        root.policy = payload
        root.policy_version = new_version

        # Void outstanding unconsumed approvals for this tree.
        revoked = self._revoke_unconsumed_approvals(root.root_task_id)

        self.session.add(
            EventRow(
                root_task_id=root.root_task_id,
                seq=self._next_seq(root.root_task_id),
                type="policy_updated",
                payload={
                    "version": new_version,
                    "updated_by": updated_by,
                    "revoked_approvals": revoked,
                    "policy": payload,
                },
            )
        )
        self.session.flush()
        return {
            "root_task_id": root.root_task_id,
            "version": new_version,
            "policy": payload,
            "revoked_approvals": revoked,
        }

    def _revoke_unconsumed_approvals(self, root_task_id: str) -> int:
        """Revoke pending/approved approvals in a tree. Returns the count.

        Only approvals that have NOT been consumed are touched, so a running
        call is never disturbed and a consumed approval stays consumed.
        """
        sub_task_ids = [
            t.id
            for t in self.session.scalars(
                select(TaskRow).where(TaskRow.root_task_id == root_task_id)
            ).all()
        ]
        if not sub_task_ids:
            return 0
        inv_ids = [
            i.id
            for i in self.session.scalars(
                select(InvocationRow).where(InvocationRow.task_id.in_(sub_task_ids))
            ).all()
        ]
        if not inv_ids:
            return 0
        targets = self.session.scalars(
            select(ApprovalRow).where(
                ApprovalRow.invocation_id.in_(inv_ids),
                ApprovalRow.status.in_(
                    [ApprovalStatus.PENDING.value, ApprovalStatus.APPROVED.value]
                ),
            )
        ).all()
        for appr in targets:
            appr.status = ApprovalStatus.REVOKED.value
            # The invocation's snapshot carries the OLD policy version, so it can
            # never be consumed again; mark it invalid rather than draft.
            inv = self.session.get(InvocationRow, appr.invocation_id)
            if inv is not None and inv.status in (
                InvocationStatus.DRAFT.value,
                InvocationStatus.AWAITING_APPROVAL.value,
                InvocationStatus.QUEUED.value,
            ):
                self._set_invocation_status(inv, InvocationStatus.INVALID)
        self.session.flush()
        return len(targets)
