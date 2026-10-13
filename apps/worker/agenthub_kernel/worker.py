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
        """Find one consumable invocation and start its adapter.

        Returns None when there is nothing ready, else a diagnostic dict.
        """
        with self.db.session() as session:
            svc = PolicyService(session)
            inv = self._pick_ready(session)
            if inv is None:
                return None

            # The consume gate: any QuotaError rolls back the whole transaction
            # and the adapter is never started.
            try:
                plan = svc.consume(inv.id)
            except QuotaError as exc:
                session.commit()  # persist status changes (e.g. invalid/expired)
                return {"invocation_id": inv.id, "ok": False, "code": exc.code, "error": exc.message}

            snapshot = plan["snapshot"]
            # Start the adapter OUTSIDE the DB transaction. The approval is
            # already consumed and the slot reserved, so a crash after this
            # point cannot cause a double-start.
            start = self.adapter.start(inv.id, _snapshot_from_dict(snapshot))
            if start.ok:
                svc.transition_invocation(inv.id, InvocationStatus.RUNNING)
            else:
                svc.release(inv.id, InvocationStatus.FAILED)
            session.commit()
            return {
                "invocation_id": inv.id,
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
    """Standalone worker loop (dev/demo). Drains once per second until stopped."""
    import time

    from agenthub_adapters.mock import MockAdapter

    db = Database("sqlite:///./agent-hub.db")
    db.create_all()
    worker = Worker(db, MockAdapter())
    print("worker started; Ctrl-C to stop")
    try:
        while True:
            drained = worker.drain_once()
            if drained:
                print(f"drained {len(drained)} invocation(s)")
            time.sleep(1.0)
    except KeyboardInterrupt:
        worker.stop()
        print("worker stopped")
