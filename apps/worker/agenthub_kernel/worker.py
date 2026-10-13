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

#: External run states the Mock adapter records (see agenthub_adapters.mock).
MOCK_RUNNING_STATE = "running"


class Worker:
    def __init__(self, db: Database, adapter: AdapterContract) -> None:
        self.db = db
        self.adapter = adapter
        self._stopped = False

    def stop(self) -> None:
        self._stopped = True

    # ------------------------------------------------------------------ #
    # Stop / cancel
    # ------------------------------------------------------------------ #

    def cancel_tree(self, root_task_id: str, *, requested_by: str = "local-user") -> dict[str, Any]:
        """Persist a stop request, then cancel the calls that are in flight.

        Ordering matters: the stop is persisted FIRST, so even if cancellation
        fails or the process dies mid-way, no new call can be scheduled for this
        tree. Cancellation then asks the adapter to stop each running invocation
        and marks it ``canceled``, releasing its slot idempotently.
        """
        with self.db.session() as session:
            plan = PolicyService(session).request_stop(
                root_task_id, requested_by=requested_by
            )

        outcomes: list[dict[str, Any]] = []
        for item in plan["running"]:
            invocation_id = item["invocation_id"]
            cancel_result = self._cancel_one(invocation_id)
            outcomes.append(
                {"invocation_id": invocation_id, "cancel": cancel_result}
            )

        return {
            "root_task_id": root_task_id,
            "already_stopped": plan["already_stopped"],
            "attempted": len(plan["running"]),
            "outcomes": outcomes,
        }

    def _cancel_one(self, invocation_id: str) -> dict[str, Any]:
        """Cancel a single invocation via the adapter, then settle its slot."""
        cancel_fn = getattr(self.adapter, "cancel_invocation", None)
        if cancel_fn is None:
            # An adapter that cannot cancel must say so, not pretend.
            return {"ok": False, "reason": "unsupported"}
        try:
            result = cancel_fn(invocation_id)
        except Exception as exc:  # noqa: BLE001
            result = {"ok": False, "reason": f"error:{type(exc).__name__}"}

        with self.db.session() as session:
            svc = PolicyService(session)
            inv = session.get(InvocationRow, invocation_id)
            if inv is not None and inv.status not in (
                InvocationStatus.SUCCEEDED.value,
                InvocationStatus.FAILED.value,
            ):
                # Do not stomp a call that finished concurrently.
                svc.release(invocation_id, InvocationStatus.CANCELED)
            else:
                result = {**result, "note": "already_terminal"}
        return result

    # ------------------------------------------------------------------ #
    # Run-time limit enforcement
    # ------------------------------------------------------------------ #

    def reap_timeouts(self) -> dict[str, Any]:
        """Time out invocations that exceeded the tree's run-time limit.

        Ordering: ask the adapter to cancel FIRST (best effort), then settle the
        invocation as ``timed_out`` and release its slot. If cancellation is
        unsupported we still time the invocation out locally but say so, rather
        than claiming the remote side stopped.
        """
        with self.db.session() as session:
            overdue = PolicyService(session).list_overdue()

        timed_out = []
        for item in overdue:
            invocation_id = item["invocation_id"]
            cancel_result: dict[str, Any] = {"ok": False, "reason": "unsupported"}
            cancel_fn = getattr(self.adapter, "cancel_invocation", None)
            if cancel_fn is not None:
                try:
                    cancel_result = cancel_fn(invocation_id)
                except Exception as exc:  # noqa: BLE001
                    cancel_result = {"ok": False, "reason": f"error:{type(exc).__name__}"}
            with self.db.session() as session:
                PolicyService(session).mark_timed_out(invocation_id)
            timed_out.append(
                {
                    "invocation_id": invocation_id,
                    "limit_seconds": item["limit_seconds"],
                    "cancel": cancel_result,
                    "remote_stop_confirmed": bool(cancel_result.get("ok")),
                }
            )
        return {"examined": len(overdue), "timed_out": timed_out}

    # ------------------------------------------------------------------ #
    # Unknown reconciliation (user adjudication)
    # ------------------------------------------------------------------ #

    def reconcile(
        self,
        invocation_id: str,
        *,
        action: str,
        outcome: Optional[str] = None,
        actor: str = "local-user",
    ) -> dict[str, Any]:
        """Resolve an ``unknown`` invocation: settle it or retry with re-approval.

        Adapter evidence is gathered first and reported, so the user's decision
        is informed — but the decision itself is the user's, never automatic.
        """
        evidence = None
        lookup = getattr(self.adapter, "lookup_by_invocation", None)
        if lookup is not None:
            try:
                evidence = lookup(invocation_id)
            except Exception:  # noqa: BLE001
                evidence = None

        with self.db.session() as session:
            result = PolicyService(session).reconcile_unknown(
                invocation_id, action=action, outcome=outcome, actor=actor
            )
        return {**result, "adapter_evidence": evidence}

    # ------------------------------------------------------------------ #
    # Crash recovery
    # ------------------------------------------------------------------ #

    def recover(self) -> dict[str, Any]:
        """Reconcile ``starting``/``running`` invocations after a restart.

        Rules (AGENT_HUB_PLAN §4.1):

        * Ask the **external side** (adapter) what it knows, per invocation.
        * Confirmed running  -> keep as ``running``; the slot stays held.
        * Confirmed terminal -> settle the invocation and free the slot once.
        * **Unconfirmable**  -> ``unknown``; NOT restarted, and the slot is NOT
          released early, because we cannot prove the call has stopped.
        * Never silently restart a call.
        """
        with self.db.session() as session:
            candidates = PolicyService(session).list_recoverable()

        summary = {
            "examined": len(candidates),
            "confirmed_running": [],
            "settled": [],
            "unknown": [],
            "restarted": [],  # always empty by design
        }

        for item in candidates:
            invocation_id = item["invocation_id"]
            record = None
            lookup = getattr(self.adapter, "lookup_by_invocation", None)
            if lookup is not None:
                try:
                    record = lookup(invocation_id)
                except Exception:  # noqa: BLE001
                    record = None

            if record is None:
                # Cannot confirm: mark unknown, keep the slot, do not restart.
                with self.db.session() as session:
                    PolicyService(session).mark_unknown(invocation_id)
                summary["unknown"].append(invocation_id)
                continue

            external_status = str(record.get("status") or "")
            adapter_id = record.get("adapter_invocation_id")

            if external_status == MOCK_RUNNING_STATE:
                with self.db.session() as session:
                    svc = PolicyService(session)
                    inv = session.get(InvocationRow, invocation_id)
                    if inv is not None:
                        inv.status = InvocationStatus.RUNNING.value
                        inv.adapter_invocation_id = (
                            inv.adapter_invocation_id or adapter_id
                        )
                    svc.session.flush()
                summary["confirmed_running"].append(invocation_id)
                continue

            if external_status == "cancelled":
                term = InvocationStatus.CANCELED
            elif external_status == "finished":
                term = InvocationStatus.SUCCEEDED
            elif external_status == "failed":
                term = InvocationStatus.FAILED
            else:
                # An unrecognised external state must not be guessed at.
                with self.db.session() as session:
                    PolicyService(session).mark_unknown(invocation_id)
                summary["unknown"].append(invocation_id)
                continue

            with self.db.session() as session:
                svc = PolicyService(session)
                inv = session.get(InvocationRow, invocation_id)
                if inv is not None:
                    inv.adapter_invocation_id = inv.adapter_invocation_id or adapter_id
                svc.release(invocation_id, term)
            summary["settled"].append({"invocation_id": invocation_id, "status": term.value})

        return summary

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
                # The invocation must be QUEUED: that is the state an approval
                # puts it in. Anything terminal, or not yet approved, is skipped.
                InvocationRow.status == InvocationStatus.QUEUED.value,
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
