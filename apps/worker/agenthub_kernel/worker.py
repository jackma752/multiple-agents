"""The Agent Hub Worker loop.

The Worker's job is: pick a queued/approved invocation, and before starting the
adapter, run :meth:`PolicyService.consume` inside a transaction. Because
``consume`` enforces approval validity, stop state and quotas atomically, an
adapter is only ever started for a genuinely approved + in-quota call.

After starting the adapter, the Worker tracks the invocation as ``running`` and,
on completion, calls :meth:`PolicyService.release` to free the concurrent slot.
"""

from __future__ import annotations

import time
from typing import Any, Optional

from sqlalchemy import select

from agenthub.contracts import AdapterContract, InvocationStatus
from agenthub_kernel.db import Database, InvocationRow, TaskRow
from agenthub_kernel.service import PolicyService, QuotaError


class Worker:
    def __init__(self, db: Database, adapter: AdapterContract) -> None:
        self.db = db
        self.adapter = adapter
        self._stopped = False

    def stop(self) -> None:
        self._stopped = True

    def drain_once(self, max_calls: int = 100) -> list[dict[str, Any]]:
        """Process up to ``max_calls`` ready invocations. Returns diagnostics."""
        outcomes: list[dict[str, Any]] = []
        for _ in range(max_calls):
            outcome = self._process_one()
            if outcome is None:
                break
            outcomes.append(outcome)
        return outcomes

    def _process_one(self) -> Optional[dict[str, Any]]:
        """Find one consumable invocation, commit the gate, then start the adapter.

        Ordering is deliberate and load-bearing (AGENT_HUB_PLAN §4.1): the
        approval is consumed and the quota slot reserved in a transaction that
        is COMMITTED before the adapter is started. If that commit fails, the
        adapter is never started — so a failed consume transaction can never
        leave a call running without an approval.

        Returns None when there is nothing ready, else a diagnostic dict.
        """
        snapshot_data: dict[str, Any]
        invocation_id: str

        # ---- Phase 1: atomic consume + reservation, committed first ----
        with self.db.session() as session:
            svc = PolicyService(session)
            inv = self._pick_ready(session)
            if inv is None:
                return None
            invocation_id = inv.id

            try:
                plan = svc.consume(invocation_id)
            except QuotaError as exc:
                # Persist denial side effects (e.g. expired/invalid marking)
                # and stop: the adapter must not be started.
                session.commit()
                return {
                    "invocation_id": invocation_id,
                    "ok": False,
                    "code": exc.code,
                    "error": exc.message,
                }
            snapshot_data = plan["snapshot"]
            # Exiting this block COMMITS the consumed approval + reservation.
            # Any failure here propagates and the adapter is never started.

        # ---- Phase 2: only now start the adapter, outside that transaction ----
        start = self.adapter.start(invocation_id, _snapshot_from_dict(snapshot_data))

        # ---- Phase 3: record the outcome ----
        with self.db.session() as session:
            svc = PolicyService(session)
            if start.ok:
                svc.transition_invocation(invocation_id, InvocationStatus.RUNNING)
            else:
                svc.release(invocation_id, InvocationStatus.FAILED)

        return {
            "invocation_id": invocation_id,
            "ok": start.ok,
            "adapter_invocation_id": start.adapter_invocation_id,
        }

    def _pick_ready(self, session: Any) -> Optional[InvocationRow]:
        """Pick an invocation that has a consumable approval and is not terminal.

        We deliberately select by a consumable approval join so that an
        invocation awaiting user approval is never picked.
        """
        from sqlalchemy.orm import aliased

        from agenthub_kernel.db import ApprovalRow

        stmt = (
            select(InvocationRow)
            .join(ApprovalRow, ApprovalRow.invocation_id == InvocationRow.id)
            .where(
                # SAFETY: only a user-APPROVED approval makes an invocation
                # ready. A merely pending approval must never be picked, and
                # ``consume`` re-checks this independently.
                ApprovalRow.status == "approved",
                InvocationRow.status.notin_(
                    [
                        InvocationStatus.SUCCEEDED.value,
                        InvocationStatus.FAILED.value,
                        InvocationStatus.CANCELED.value,
                        InvocationStatus.TIMED_OUT.value,
                        InvocationStatus.UNKNOWN.value,
                        InvocationStatus.REJECTED.value,
                        InvocationStatus.INVALID.value,
                    ]
                ),
            )
            .order_by(InvocationRow.created_at)
            .limit(1)
        )
        return session.scalar(stmt)


def _snapshot_from_dict(data: dict[str, Any]):
    from agenthub.contracts import InvocationSnapshot

    return InvocationSnapshot(**data)


def main() -> None:
    """Standalone worker loop (dev/demo). Drains once per second until stopped.

    Uses the shared runtime configuration so this process and the API process
    open the SAME SQLite file, regardless of the working directory either was
    launched from.
    """
    import time

    from agenthub_adapters.mock import MockAdapter

    from agenthub_kernel import config

    url = config.default_db_url()
    if url.startswith("sqlite") and ":memory:" not in url:
        config.ensure_data_dir()
    db = Database(url)
    db.create_all()
    worker = Worker(db, MockAdapter())
    print(f"worker started on {url}; Ctrl-C to stop")
    try:
        while True:
            drained = worker.drain_once()
            if drained:
                print(f"drained {len(drained)} invocation(s)")
            time.sleep(1.0)
    except KeyboardInterrupt:
        worker.stop()
        print("worker stopped")
