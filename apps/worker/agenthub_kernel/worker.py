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

from sqlalchemy import select, text

from agenthub.contracts import AdapterContract, InvocationStatus, execution_admission
from agenthub_kernel.db import Database, EventRow, InvocationRow, TaskRow
from agenthub_kernel.service import PolicyService, QuotaError
from agenthub_kernel.conversations import capture_reply, restore_replies

#: External run states the Mock adapter records (see agenthub_adapters.mock).
MOCK_RUNNING_STATE = "running"


class Worker:
    def __init__(self, db: Database, adapter: AdapterContract) -> None:
        self.db = db
        self.adapter = adapter
        self._stopped = False

    def stop(self) -> None:
        self._stopped = True

    def stop_local(self, invocation_id: str, *, requested_by: str = "local-user") -> dict[str, Any]:
        """Freeze scheduling, then terminate only the runner-owned entry process.

        A local exit is not proof that remote work or native children stopped.
        The unknown state is committed before signalling so a racing completion
        cannot silently release the reservation.
        """
        with self.db.session() as session:
            # Acquire SQLite's writer lock before reading status. The completion
            # transaction uses the same ordering, including across API workers.
            session.execute(text("BEGIN IMMEDIATE"))
            inv = session.get(InvocationRow, invocation_id)
            if inv is None:
                raise QuotaError("no_invocation", "invocation not found")
            if inv.status not in {"starting", "running", "unknown"}:
                raise QuotaError("not_active", "invocation is not active")
            task = session.get(TaskRow, inv.task_id)
            svc = PolicyService(session)
            svc.request_stop(task.root_task_id, requested_by=requested_by)
            svc.mark_unknown(invocation_id, reason="local_stop_requested: remote/native child stop unconfirmed; quota retained")
            session.add(EventRow(root_task_id=task.root_task_id,
                seq=svc._next_seq(task.root_task_id), type="local_stop_requested",
                payload={"invocation_id": invocation_id, "actor": requested_by}))
        stop_fn = getattr(self.adapter, "stop_local_invocation", None)
        try:
            result = stop_fn(invocation_id) if stop_fn else {"reason": "unsupported"}
        except Exception as exc:
            result = {"reason": type(exc).__name__}
        return {
            "invocation_id": invocation_id,
            "local_stop_requested": bool(result.get("local_stop_requested")),
            "local_process_exited": bool(result.get("local_process_exited")),
            "remote_stop_confirmed": False, "status": "unknown",
            "scheduling_stopped": True, "quota_retained": True,
            "reason": result.get("reason", "unconfirmed"),
        }

    # ------------------------------------------------------------------ #
    # Stop / cancel
    # ------------------------------------------------------------------ #

    def cancel_tree(self, root_task_id: str, *, requested_by: str = "local-user") -> dict[str, Any]:
        """Persist a stop request, then cancel the calls that are in flight.

        Ordering matters: the stop is persisted FIRST, so even if cancellation
        fails or the process dies mid-way, no new call can be scheduled for this
        tree. Cancellation then asks the adapter to stop each running invocation.

        Only a CONFIRMED stop settles an invocation as ``canceled``. The response
        separates ``confirmed`` from ``unconfirmed`` so a caller cannot read
        "attempted" as "stopped".
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

        confirmed = sum(1 for o in outcomes if o["cancel"].get("remote_stop_confirmed"))
        return {
            "root_task_id": root_task_id,
            "already_stopped": plan["already_stopped"],
            "attempted": len(plan["running"]),
            "confirmed": confirmed,
            "unconfirmed": len(outcomes) - confirmed,
            "outcomes": outcomes,
        }

    def _cancel_one(self, invocation_id: str) -> dict[str, Any]:
        """Cancel one invocation, settling it ONLY when the stop is confirmed.

        An adapter that cannot cancel (Codex today) or whose cancel raised does
        NOT prove the remote work stopped. Settling it as ``canceled`` and freeing
        its slot would let a second call take a slot the first one may still be
        occupying - the same mistake the timeout path already guards against. So
        an unconfirmed stop marks the call ``unknown`` and KEEPS the slot until a
        human resolves it.
        """
        cancel_fn = getattr(self.adapter, "cancel_invocation", None)
        if cancel_fn is None:
            # An adapter that cannot cancel must say so, not pretend.
            result: dict[str, Any] = {"ok": False, "reason": "unsupported"}
        else:
            try:
                result = cancel_fn(invocation_id)
            except Exception as exc:  # noqa: BLE001
                result = {"ok": False, "reason": f"error:{type(exc).__name__}"}

        confirmed = bool(result.get("ok"))
        with self.db.session() as session:
            svc = PolicyService(session)
            inv = session.get(InvocationRow, invocation_id)
            if inv is not None and inv.status not in (
                InvocationStatus.SUCCEEDED.value,
                InvocationStatus.FAILED.value,
            ):
                if confirmed:
                    svc.release(invocation_id, InvocationStatus.CANCELED)
                else:
                    svc.mark_unknown(
                        invocation_id,
                        reason=(
                            "stop requested, but cancellation could not be confirmed "
                            f"(adapter cancel: {result.get('reason') or 'not ok'}); "
                            "remote work may still be running, so the quota slot is "
                            "deliberately kept"
                        ),
                    )
            else:
                result = {**result, "note": "already_terminal"}
        return {**result, "remote_stop_confirmed": confirmed}

    # ------------------------------------------------------------------ #
    # Run-time limit enforcement
    # ------------------------------------------------------------------ #

    def reap_timeouts(self) -> dict[str, Any]:
        """Time out invocations that exceeded the tree's run-time limit.

        Crucial distinction:

        * Cancellation CONFIRMED -> the call really stopped, so settle it as
          ``timed_out`` and release its slot.
        * Cancellation NOT confirmed (unsupported, errored, or no evidence) ->
          we cannot prove the call stopped. Mark it ``unknown`` and KEEP its
          quota slot, so no second call can take the slot the first one may
          still be occupying.
        """
        with self.db.session() as session:
            overdue = PolicyService(session).list_overdue()

        timed_out = []
        for item in overdue:
            invocation_id = item["invocation_id"]
            confirmed, evidence, cancel_result = self._stop_evidence(invocation_id)
            with self.db.session() as session:
                svc = PolicyService(session)
                if confirmed:
                    svc.mark_timed_out(invocation_id)
                    outcome = "timed_out"
                else:
                    svc.mark_unknown(
                        invocation_id,
                        reason=(
                            "exceeded run_time_limit_seconds but cancellation "
                            f"could not be confirmed ({evidence})"
                        ),
                    )
                    outcome = "unknown"
            timed_out.append(
                {
                    "invocation_id": invocation_id,
                    "limit_seconds": item["limit_seconds"],
                    "cancel": cancel_result,
                    "remote_stop_confirmed": confirmed,
                    "outcome": outcome,
                    "slot_released": confirmed,
                    "evidence": evidence,
                }
            )
        return {"examined": len(overdue), "timed_out": timed_out}

    # ------------------------------------------------------------------ #
    # Stop confirmation (shared by reaping and retry)
    # ------------------------------------------------------------------ #

    def _stop_evidence(
        self, invocation_id: str, kernel_status: Optional[str] = None
    ) -> tuple[bool, str, dict[str, Any]]:
        """Try to CONFIRM that an execution is no longer running.

        Returns ``(confirmed_stopped, evidence, cancel_result)``.

        We first ask the adapter to cancel, then look at the adapter's own
        execution record. Confirmed means either:

        * the external side reports a terminal state (finished/failed/cancelled);
        * the external side has NO record at all AND the kernel has **no durable
          evidence that this call ever reached an adapter**. The Mock writes its
          record synchronously before ``start()`` returns, so for a call that was
          never started an absent record positively means nothing is running.

        The "ever started" test uses durable columns
        (``started_at`` / ``adapter_invocation_id``), NOT the current status.
        Status is not durable evidence: a running call can be moved to
        ``unknown`` (e.g. a timeout whose cancellation could not be confirmed),
        which would otherwise erase the only trace that it started and let us
        free a slot that is still occupied.

        A missing record for a call we know started is *lost evidence*, not proof
        of stopping, so it is NOT confirmation.

        Anything else (record still running, unrecognised state, unreadable
        store, adapter without cancel) is also NOT confirmation, and callers
        must withhold both the slot release and any retry.
        """
        ever_started = False
        with self.db.session() as session:
            prior_stops = session.scalars(select(EventRow).where(EventRow.type == "local_stop_requested")).all()
            if any(e.payload.get("invocation_id") == invocation_id for e in prior_stops):
                return False, "local stop is not remote/native-child stop evidence", {"ok": False, "reason": "unconfirmed"}
        if kernel_status is None:
            with self.db.session() as session:
                row = session.get(InvocationRow, invocation_id)
                if row is not None:
                    kernel_status = row.status
                    ever_started = bool(row.started_at) or bool(row.adapter_invocation_id)
                else:
                    kernel_status = ""
        else:
            with self.db.session() as session:
                row = session.get(InvocationRow, invocation_id)
                if row is not None:
                    ever_started = bool(row.started_at) or bool(row.adapter_invocation_id)

        cancel_result: dict[str, Any] = {"ok": False, "reason": "unsupported"}
        cancel_fn = getattr(self.adapter, "cancel_invocation", None)
        if cancel_fn is not None:
            try:
                cancel_result = cancel_fn(invocation_id)
            except Exception as exc:  # noqa: BLE001
                cancel_result = {"ok": False, "reason": f"error:{type(exc).__name__}"}

        lookup = getattr(self.adapter, "lookup_by_invocation", None)
        if lookup is None:
            return False, "adapter cannot report execution status", cancel_result
        try:
            record = lookup(invocation_id)
        except Exception as exc:  # noqa: BLE001
            return False, f"status lookup failed: {type(exc).__name__}", cancel_result

        if record is None:
            if ever_started:
                return (
                    False,
                    "no execution record, but the kernel has durable evidence "
                    "this call started (started_at/adapter_invocation_id): "
                    "evidence lost, cannot confirm it stopped",
                    cancel_result,
                )
            if kernel_status == InvocationStatus.RUNNING.value:
                return (
                    False,
                    "no execution record while the kernel believes the call is "
                    "running: evidence lost, cannot confirm it stopped",
                    cancel_result,
                )
            return (
                True,
                "no execution record and no evidence the call ever started: "
                "it never reached the adapter",
                cancel_result,
            )

        status = str(record.get("status") or "")
        if status in ("finished", "failed", "cancelled"):
            return True, f"external status is terminal ({status})", cancel_result
        return False, f"external status is not terminal ({status or 'empty'})", cancel_result

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

        For ``retry`` we first try to CONFIRM that the previous execution is no
        longer running. Without that confirmation the retry is refused
        (``not_stopped``) and the old invocation keeps its quota slot, so a
        second call can never start on top of a possibly-still-running first.
        """
        evidence = None
        lookup = getattr(self.adapter, "lookup_by_invocation", None)
        if lookup is not None:
            try:
                evidence = lookup(invocation_id)
            except Exception:  # noqa: BLE001
                evidence = None

        confirmed_stopped = False
        stop_evidence = "not evaluated"
        cancel_result: dict[str, Any] = {"ok": False, "reason": "not attempted"}
        if action == "retry":
            confirmed_stopped, stop_evidence, cancel_result = self._stop_evidence(
                invocation_id
            )

        with self.db.session() as session:
            session.execute(text("BEGIN IMMEDIATE"))
            result = PolicyService(session).reconcile_unknown(
                invocation_id,
                action=action,
                outcome=outcome,
                actor=actor,
                confirmed_stopped=confirmed_stopped,
                stop_evidence=stop_evidence,
            )
        return {
            **result,
            "adapter_evidence": evidence,
            "remote_stop_confirmed": confirmed_stopped if action == "retry" else None,
            "stop_evidence": stop_evidence if action == "retry" else None,
            "cancel": cancel_result if action == "retry" else None,
        }

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
                session.execute(text("BEGIN IMMEDIATE"))
                svc = PolicyService(session)
                inv = session.get(InvocationRow, invocation_id)
                if inv is None or inv.status not in {"starting", "running"}:
                    continue  # A concurrent stop/settlement takes precedence.
                if inv is not None:
                    inv.adapter_invocation_id = inv.adapter_invocation_id or adapter_id
                svc.release(invocation_id, term)
                capture_reply(session, self.adapter, invocation_id)
            summary["settled"].append({"invocation_id": invocation_id, "status": term.value})

        summary["replies_restored"] = restore_replies(self.db, self.adapter)
        return summary

    def drain_once(self, max_calls: int = 100) -> list[dict[str, Any]]:
        """Process ready invocations, at most once each per pass.

        An invocation that is REFUSED by the consume gate usually keeps its
        state (e.g. it is over quota, or the tree is stopped), so a naive loop
        would re-pick and re-refuse it until ``max_calls`` is exhausted — 100
        pointless round-trips per drain. We therefore remember what we already
        looked at in this pass and stop once nothing new is available.

        Returns diagnostics for every invocation examined.
        """
        outcomes: list[dict[str, Any]] = []
        seen: set[str] = set()
        for _ in range(max_calls):
            outcome = self._process_one(exclude=seen)
            if outcome is None:
                break
            outcomes.append(outcome)
            seen.add(outcome["invocation_id"])
        return outcomes

    def run_one(self, invocation_id: str) -> dict[str, Any]:
        """Execute only this approved call; never fall through to another task."""
        if self._stopped:
            return {"invocation_id": invocation_id, "ok": False,
                    "code": "worker_stopped", "launch_attempted": False}
        outcome = self._process_one(target_id=invocation_id)
        if outcome is None:
            return {"invocation_id": invocation_id, "ok": False,
                    "code": "not_ready", "launch_attempted": False}
        outcome.setdefault("adapter_start_attempted", False)
        return outcome

    def _process_one(
        self, exclude: Optional[set[str]] = None, *, target_id: Optional[str] = None,
    ) -> Optional[dict[str, Any]]:
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
            session.execute(text("BEGIN IMMEDIATE"))
            svc = PolicyService(session)
            inv = self._pick_ready(session, exclude=exclude, target_id=target_id)
            if inv is None:
                return None
            invocation_id = inv.id

            # Eligibility precedes consumption: an approval cannot compensate
            # for an adapter that bypasses native-child approval/quota controls.
            # Keep the call queued and its approval untouched. This is NOT an
            # unknown execution: nothing has started or reserved a slot.
            admission = execution_admission(self.adapter, inv.snapshot.get("execution_mode", "strict"))
            if not admission.allowed:
                return {
                    "invocation_id": invocation_id,
                    "ok": False,
                    "code": admission.code,
                    "error": admission.reason,
                    "launch_attempted": False,
                }

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
                    "launch_attempted": False,
                }
            snapshot_data = plan["snapshot"]
            gate_token = plan.get("gate_token", "")
            # Exiting this block COMMITS the consumed approval + reservation.
            # Any failure here propagates and the adapter is never started.

        # ---- Phase 2: only now start the adapter, outside that transaction ----
        # The gate token proves an approval was consumed and a slot reserved; an
        # adapter that launches a real process requires it.
        try:
            start = self.adapter.start(
                invocation_id,
                _snapshot_from_dict(snapshot_data),
                gate_token=gate_token,
            )
        except Exception as exc:  # noqa: BLE001
            # Last-resort guard. By now the approval is consumed and the slot
            # reserved, so letting the exception escape would leave a call stuck
            # in `starting` holding that slot forever, with no explanation. An
            # exception tells us nothing about whether remote work began, so the
            # honest outcome is `unknown`: keep the slot, record why, and let a
            # human resolve it. (Adapters are expected to handle their own
            # pre-launch errors; this is the backstop.)
            reason = f"adapter start raised {type(exc).__name__}: {exc}"
            with self.db.session() as session:
                PolicyService(session).mark_unknown(invocation_id, reason=reason[:400])
            return {
                "invocation_id": invocation_id,
                "ok": False,
                "terminal": False,
                "adapter_invocation_id": "",
                "error": reason,
                "adapter_start_attempted": True,
            }

        # ---- Phase 3: record the outcome ----
        with self.db.session() as session:
            session.execute(text("BEGIN IMMEDIATE"))
            svc = PolicyService(session)
            inv = session.get(InvocationRow, invocation_id)
            # Persist the adapter's own id in BOTH branches. Together with
            # ``started_at`` this is the durable "this call really reached the
            # adapter" evidence that recovery and reaping depend on. Without it,
            # a crash between the adapter start and the status update would be
            # indistinguishable from a call that never started at all - and we
            # could then wrongly free a slot that is still occupied.
            if inv is not None and start.adapter_invocation_id:
                inv.adapter_invocation_id = start.adapter_invocation_id
            if inv is not None and inv.status == InvocationStatus.UNKNOWN.value:
                # A concurrent stop/reaper already recorded uncertainty.
                return {"invocation_id": invocation_id, "ok": False,
                        "terminal": False, "adapter_invocation_id": inv.adapter_invocation_id,
                        "adapter_start_attempted": True,
                        "error": inv.error}
            if start.ok:
                # Set started_at first (durable evidence the call reached the
                # adapter), then either settle a completed run or leave it
                # running. A synchronous adapter that already finished reports
                # terminal=True; leaving it 'running' would leak its slot.
                svc.transition_invocation(invocation_id, InvocationStatus.RUNNING)
                if getattr(start, "terminal", False):
                    svc.release(invocation_id, InvocationStatus.SUCCEEDED)
            elif getattr(start, "terminal", True):
                # Definitive failure: settle it and free the slot once.
                svc.release(invocation_id, InvocationStatus.FAILED)
            else:
                # NOT confirmed (timeout, or a cancellation the adapter cannot
                # perform). The remote work may still be running, so the slot is
                # deliberately KEPT and the call becomes unknown until a human
                # resolves it. Freeing here would let a second call take a slot
                # the first one may still occupy.
                svc.mark_unknown(
                    invocation_id,
                    reason=f"adapter outcome unconfirmed: {start.detail}",
                )
            capture_reply(session, self.adapter, invocation_id)

        return {
            "invocation_id": invocation_id,
            "ok": start.ok,
            "terminal": getattr(start, "terminal", True),
            "adapter_invocation_id": start.adapter_invocation_id,
            "adapter_start_attempted": True,
        }

    def _pick_ready(
        self, session: Any, exclude: Optional[set[str]] = None,
        *, target_id: Optional[str] = None,
    ) -> Optional[InvocationRow]:
        """Pick a QUEUED invocation with an approved approval.

        ``exclude`` holds ids already handled in this drain pass; they are
        filtered out so a refused invocation is not retried in a tight loop.
        """
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
        )
        if target_id is not None:
            stmt = stmt.where(InvocationRow.id == target_id)
        stmt = stmt.order_by(InvocationRow.created_at).limit(50)
        skip = exclude or set()
        for candidate in session.scalars(stmt).all():
            if candidate.id not in skip:
                return candidate
        return None


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
