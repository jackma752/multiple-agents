"""Shared pytest fixtures and helpers for the Agent Hub kernel tests."""

from __future__ import annotations

from agenthub.contracts import InvocationSnapshot, Policy
from agenthub_adapters.mock import MockAdapter
from agenthub_kernel.db import Database, InvocationRow, TaskRow
from agenthub_kernel.service import PolicyService, QuotaError
from agenthub_kernel.worker import Worker

DEFAULT_POLICY = Policy()


def _store_path_for(url: str) -> str:
    """Where the Mock adapter keeps its execution log.

    Uses the same file as the kernel DB when the kernel is file-backed (so
    records survive a simulated restart), otherwise a shared-cache in-memory
    database.
    """
    if url.startswith("sqlite:///") and ":memory:" not in url:
        return url.replace("sqlite:///", "", 1)
    return ":memory:"


class KernelHarness:
    """High-level driver around a fresh Database + MockAdapter + Worker.

    Each operation opens its own transaction, mirroring how the API and worker
    would operate against the persistent store.
    """

    def __init__(
        self,
        policy: Policy | None = None,
        url: str = "sqlite:///:memory:",
        adapter=None,
    ):
        self.policy = policy or DEFAULT_POLICY
        self.db = Database(url)
        self.db.create_all()
        # The Mock adapter keeps its own execution log. For a file-backed kernel
        # DB we point the mock store at the same file (its own table) so records
        # survive a simulated restart; for in-memory we use a shared-cache
        # in-memory store.
        self.store_path = _store_path_for(url)
        # `adapter` lets a test substitute a real adapter (e.g. the Codex adapter
        # driven by a fake runner) while everything else - approval, snapshot and
        # quota gates - stays exactly as in production.
        self.adapter = adapter if adapter is not None else MockAdapter(store_path=self.store_path)
        self.worker = Worker(self.db, self.adapter)

    def new_adapter(self) -> MockAdapter:
        """A fresh adapter (and fresh in-memory counters) over the same store.

        Used to simulate a process restart: counters reset, persisted execution
        records remain, which is exactly the situation recovery must handle.
        """
        return MockAdapter(store_path=self.store_path)

    def new_worker(self):
        """A fresh Worker + fresh adapter over the same database and store."""
        from agenthub_kernel.worker import Worker as _Worker

        adapter = self.new_adapter()
        return _Worker(self.db, adapter), adapter

    # ---- service helpers (each opens a fresh session) ----

    def svc(self) -> PolicyService:
        # Return a service bound to a short-lived session; callers commit via
        # the context-managed session they open themselves. For convenience we
        # open a session and leave it open for the test body (tests are small).
        cm = self.db.session()
        session = cm.__enter__()
        # stash a way to close later
        self._pending_cm = cm
        return PolicyService(session, self.policy)

    def close(self):
        if getattr(self, "_pending_cm", None) is not None:
            self._pending_cm.__exit__(None, None, None)

    def open_session(self):
        return self.db.session()

    def create_root(self, goal: str = "build login", agent_id: str = "main") -> TaskRow:
        with self.open_session() as session:
            svc = PolicyService(session, self.policy)
            return svc.create_task(agent_id=agent_id, goal=goal, kind="user")

    def create_subtask(self, parent_task_id: str, goal: str, agent_id: str = "sub") -> TaskRow:
        with self.open_session() as session:
            svc = PolicyService(session, self.policy)
            return svc.create_task(
                agent_id=agent_id, goal=goal, kind="user", parent_task_id=parent_task_id
            )

    def create_invocation(self, task_id: str, *, permissions=None, resources=None) -> InvocationRow:
        with self.open_session() as session:
            svc = PolicyService(session, self.policy)
            task = session.get(TaskRow, task_id)
            # Use the tree's CURRENT policy version so a call created after a
            # policy update is not instantly invalidated by that update.
            root = session.get(TaskRow, task.root_task_id)
            policy_version = int(
                (root.policy_version if root is not None else task.policy_version) or 1
            )
            snapshot = InvocationSnapshot(
                agent_id=task.agent_id,
                task_input=task.goal,
                parent_task_id=str(task.parent_task_id),
                depth=task.depth,
                permissions=permissions or {},
                resources=resources or {},
                policy_version=policy_version,
            )
            return svc.create_invocation(task_id=task_id, snapshot=snapshot)

    def create_approval(self, invocation_id: str, ttl_seconds: int = 300):
        with self.open_session() as session:
            svc = PolicyService(session, self.policy)
            return svc.create_approval(invocation_id=invocation_id, ttl_seconds=ttl_seconds)

    def approve(self, approval_id: str):
        with self.open_session() as session:
            svc = PolicyService(session, self.policy)
            svc.approve(approval_id)
            session.commit()

    def reject(self, approval_id: str):
        with self.open_session() as session:
            svc = PolicyService(session, self.policy)
            svc.reject(approval_id)
            session.commit()

    def revoke(self, approval_id: str):
        with self.open_session() as session:
            svc = PolicyService(session, self.policy)
            svc.revoke(approval_id)
            session.commit()

    def expire_stale(self) -> int:
        with self.open_session() as session:
            svc = PolicyService(session, self.policy)
            n = svc.expire_stale()
            session.commit()
            return n

    def make_older_than(self, approval_id: str):
        """Force an approval's expires_at into the past (test-only)."""
        import datetime as dt

        from agenthub_kernel.db import ApprovalRow

        with self.open_session() as session:
            appr = session.get(ApprovalRow, approval_id)
            # naive UTC to match how SQLite stores/returns DateTime values
            appr.expires_at = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None) - dt.timedelta(seconds=1)
            session.commit()

    def stop_tree(self, root_task_id: str):
        with self.open_session() as session:
            svc = PolicyService(session, self.policy)
            svc.stop_tree(root_task_id)
            session.commit()

    def update_policy(self, root_task_id: str, policy: Policy, updated_by: str = "local-user"):
        with self.open_session() as session:
            svc = PolicyService(session, self.policy)
            result = svc.update_policy(root_task_id, policy, updated_by=updated_by)
            session.commit()
            return result

    def pause(self, root_task_id: str, requested_by: str = "local-user"):
        with self.open_session() as session:
            result = PolicyService(session, self.policy).pause_tree(
                root_task_id, requested_by=requested_by
            )
            session.commit()
            return result

    def resume(self, root_task_id: str, requested_by: str = "local-user"):
        with self.open_session() as session:
            result = PolicyService(session, self.policy).resume_tree(
                root_task_id, requested_by=requested_by
            )
            session.commit()
            return result

    def mark_running_old(self, invocation_id: str, seconds: int):
        """Backdate started_at so the run-time limit is exceeded (test-only)."""
        import datetime as dt

        with self.open_session() as session:
            row = session.get(InvocationRow, invocation_id)
            row.started_at = dt.datetime.now(dt.timezone.utc).replace(
                tzinfo=None
            ) - dt.timedelta(seconds=seconds)
            session.commit()

    def drain(self) -> list:
        return self.worker.drain_once()


def _snapshot_from_inv(inv: InvocationRow) -> InvocationSnapshot:
    return InvocationSnapshot(**inv.snapshot)
