"""FastAPI application for the Agent Hub approval & quota kernel (P2).

Exposes the endpoints from AGENT_HUB_PLAN §5 (a pragmatic subset):

    POST   /api/agents                     register an agent
    GET    /api/agents                     list agents
    POST   /api/conversations/{id}/messages  save a message without a call
    POST   /api/conversations/{id}/invocations  save a message and pending approval
    GET    /api/conversations/{id}/events   ordered events after a seq cursor
    GET    /api/conversations/{id}/events/stream  SSE replay + follow (Last-Event-ID)
    POST   /api/invocations/{id}/run        claim the named approved call; 202, runs in background
    GET    /api/conversations/{id}/tool-approvals  tool approval cards of a conversation
    POST   /api/tool-approvals/{id}/approve  user approves ONE tool request (card_hash required)
    POST   /api/tool-approvals/{id}/decline  user declines one tool request
    POST   /api/tasks/{id}/delegations     propose a subtask (does not execute)
    POST   /api/invocations                create an awaiting-approval invocation
    POST   /api/approvals/{id}/approve     user approves
    POST   /api/approvals/{id}/reject      user rejects
    POST   /api/approvals/{id}/revoke      revoke an unconsumed approval
    POST   /api/tasks/{id}/policy          update policy (new version)
    GET    /api/tasks/{id}/events          list events (SSE in a real build)
    POST   /api/tasks/{id}/stop            stop the whole tree
    POST   /api/worker/drain               run one worker pass (dev/demo)

The approval endpoints are intentionally the ONLY way to approve. Nothing in
the agent / worker / model path can approve; this is the backend-enforced gate.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import Any, Optional, Literal

from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel, StrictBool, StrictInt, Field

from agenthub.contracts import InvocationSnapshot, Policy
from agenthub_kernel import Worker
from agenthub_kernel.adapters import adapter_binding
from agenthub_kernel.db import Database, TaskRow
from agenthub_kernel.service import PolicyService, QuotaError

from agenthub_adapters.mock import MockAdapter
from agenthub_rt.auth import Principal, authenticate, require_approver


class AgentIn(BaseModel):
    id: str
    name: str
    version: str = "unknown"
    capabilities: dict[str, Any] = {}


class DelegationIn(BaseModel):
    agent_id: str
    goal: str
    parent_task_id: str
    kind: str = "user"


class InvocationIn(BaseModel):
    task_id: str
    agent_id: str
    task_input: str
    parent_task_id: Optional[str] = None
    depth: int = 0
    permissions: dict[str, Any] = {}
    resources: dict[str, Any] = {}
    execution_mode: Literal["strict", "supervised"] = "strict"
    model: Optional[str] = Field(default=None, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/@-]{0,79}$")
    reasoning_effort: Optional[str] = Field(default=None, pattern=r"^[a-z][a-z0-9_-]{0,31}$")


class ApprovalIn(BaseModel):
    accept_unmanaged_native_agents: StrictBool = False


class PolicyIn(BaseModel):
    model_config = {"protected_namespaces": ()}
    max_subagents_total: int = 3
    max_subagents_concurrent: int = 2
    max_depth: int = 1
    max_agent_invocations: int = 10
    collaboration_enabled: StrictBool = False
    # Token budgets (T-1c). A full replacement like the fields above: leaving
    # them out clears the budget.
    token_budget_total: Optional[StrictInt] = None
    token_budget_per_call: Optional[StrictInt] = None
    token_budget_warn_percent: StrictInt = 80
    token_budget_mode: Literal["strict", "soft"] = "strict"
    model_budgets: dict = {}


class ReconcileIn(BaseModel):
    """User adjudication of an ``unknown`` invocation."""

    action: str  # "settle" | "retry"
    outcome: Optional[str] = None  # required when action == "settle"


def create_app(
    db: Optional[Database] = None,
    db_url: Optional[str] = None,
    adapter: Optional[Any] = None,
    adapter_kind: Optional[str] = None,
    files_root: Optional[str] = None,
    autopilot_interval: Optional[float] = None,
) -> FastAPI:
    """Build the FastAPI app.

    By default the app opens the **shared** runtime database (a file whose path
    is anchored to the repo root, not the working directory), so the API process
    and a separately launched Worker process see the same data and it survives a
    restart. Tests may pass an explicit ``db`` or ``db_url``.

    Adapter selection: defaults to the inert **Mock**. Passing
    ``adapter_kind="codex"`` (or setting ``AGENT_HUB_ADAPTER=codex``) opts into
    the real Codex CLI, which launches a real process.
    """
    from agenthub_kernel import config
    from agenthub_kernel.adapters import build_adapter

    if db is None:
        url = db_url or config.default_db_url()
        if url.startswith("sqlite") and ":memory:" not in url:
            config.ensure_data_dir()
        db = Database(url)
    db.create_all()

    @asynccontextmanager
    async def lifespan(app_: FastAPI):
        # P4.6: stages move on by themselves only when the server runs the loop
        # (normal server entry points); tests can call app.state.autopilot.tick() instead.
        if autopilot_interval:
            # Reconcile persisted calls before scheduling; unknown calls are never relaunched.
            await asyncio.to_thread(app_.state.worker.recover)
            app_.state.autopilot.start_background(autopilot_interval)
        yield
        app_.state.autopilot.stop_background()
        # Wait for calls the pool is executing; never kill them. Anything still
        # running afterwards is reconciled by the next recover().
        await asyncio.to_thread(app_.state.worker.shutdown, 30)

    app = FastAPI(title="Agent Hub", version="0.1.0", lifespan=lifespan)
    app.state.db = db
    if adapter is None:
        adapter = build_adapter(adapter_kind)
    app.state.adapter = adapter
    app.state.worker = Worker(db, adapter)
    # The only tree a message's file references may read (None: refused).
    app.state.files_root = files_root or config.files_root()
    from agenthub_kernel.autopilot import Autopilot

    app.state.autopilot = Autopilot(db, app.state.worker, files_root=lambda: app.state.files_root)

    # Read-model endpoints + the zero-build console page (see ui.py).
    from agenthub_rt.ui import register_ui

    register_ui(app, db)
    from agenthub_rt.conversations import register_conversations
    from agenthub_rt.tool_approvals import register_tool_approvals

    from agenthub_rt.stages import register_stages

    register_conversations(app, db)
    register_tool_approvals(app, db)
    register_stages(app, db)
    from agenthub_rt.models import register_models
    register_models(app, db)

    @app.get("/api/agents")
    def list_agents() -> list[dict[str, Any]]:
        return [{"id": "mock", "name": "Mock Agent", "version": "0.1.0"}]

    @app.post("/api/agents")
    def register_agent(agent: AgentIn) -> dict[str, Any]:
        return agent.model_dump()

    @app.post("/api/tasks/{task_id}/delegations")
    def propose_delegation(task_id: str, delegation: DelegationIn) -> dict[str, Any]:
        with db.session() as session:
            svc = PolicyService(session)
            try:
                child = svc.create_task(
                    agent_id=delegation.agent_id,
                    goal=delegation.goal,
                    kind=delegation.kind,
                    parent_task_id=delegation.parent_task_id,
                )
                snapshot = InvocationSnapshot(
                    agent_id=delegation.agent_id,
                    task_input=delegation.goal,
                    parent_task_id=str(child.parent_task_id),
                    depth=child.depth,
                    policy_version=child.policy_version,
                    adapter_version=adapter_binding(app.state.adapter, delegation.agent_id),
                )
                inv = svc.create_invocation(task_id=child.id, snapshot=snapshot)
                approval = svc.create_approval(invocation_id=inv.id)
                session.commit()
                return {
                    "task_id": child.id,
                    "depth": child.depth,
                    "invocation_id": inv.id,
                    "approval_id": approval.id,
                    "status": "awaiting_approval",
                }
            except QuotaError as exc:
                session.rollback()
                raise HTTPException(status_code=409, detail={"code": exc.code, "message": exc.message})

    @app.post("/api/invocations")
    def create_invocation(body: InvocationIn) -> dict[str, Any]:
        with db.session() as session:
            svc = PolicyService(session)
            try:
                task = svc.create_task(
                    agent_id=body.agent_id,
                    goal=body.task_input,
                    kind="user",
                    parent_task_id=body.parent_task_id,
                )
                snapshot = InvocationSnapshot(
                    agent_id=body.agent_id,
                    task_input=body.task_input,
                    parent_task_id=body.parent_task_id,
                    depth=task.depth,
                    permissions=body.permissions,
                    resources=body.resources, model=body.model, reasoning_effort=body.reasoning_effort,
                    execution_mode=body.execution_mode,
                    # Must carry the tree's CURRENT policy version, otherwise the
                    # consume-time policy check would (correctly) reject this
                    # invocation as approved under a stale policy.
                    policy_version=task.policy_version,
                    # The approval binds the identity of the adapter serving this agent (D11).
                    adapter_version=adapter_binding(app.state.adapter, body.agent_id),
                )
                inv = svc.create_invocation(task_id=task.id, snapshot=snapshot)
                # Plan §5: "POST /api/invocations — 创建待审批调用". Creating the
                # invocation also raises the pending approval, otherwise a call
                # made through the API could never be approved.
                approval = svc.create_approval(invocation_id=inv.id)
                session.commit()
                served = sorted(getattr(app.state.adapter, "AGENT_IDS", ()) or ())
                # Do not reject the request: the invocation is valid and the
                # adapter refuses it later, loudly and terminally. But say so up
                # front, because a call created for an agent the adapter does not
                # serve can never launch, and silently creating it wastes an
                # approval and a slot reservation.
                warning = None
                if served and body.agent_id not in served:
                    warning = (
                        f"agent_id {body.agent_id!r} is not served by the current "
                        f"adapter (accepts {served}); this call would be refused at "
                        f"launch time."
                    )
                return {
                    "task_id": task.id,
                    "depth": task.depth,
                    "invocation_id": inv.id,
                    "approval_id": approval.id,
                    "payload_hash": inv.payload_hash,
                    "status": "awaiting_approval",
                    "adapter_agent_ids": served,
                    "adapter_warning": warning,
                }
            except QuotaError as exc:
                session.rollback()
                raise HTTPException(status_code=409, detail={"code": exc.code, "message": exc.message})

    @app.post("/api/approvals/{approval_id}/approve")
    def approve(
        approval_id: str,
        body: Optional[ApprovalIn] = None,
        principal: Principal = Depends(authenticate),
    ) -> dict[str, Any]:
        require_approver(principal)
        with db.session() as session:
            svc = PolicyService(session)
            try:
                appr = svc.approve(approval_id, supervised_consent=bool(body and body.accept_unmanaged_native_agents))
                session.commit()
                return {
                    "approval_id": appr.id,
                    "status": appr.status,
                    "approved_by": principal.identity,
                }
            except QuotaError as exc:
                session.rollback()
                raise HTTPException(status_code=409, detail={"code": exc.code, "message": exc.message})

    @app.post("/api/approvals/{approval_id}/reject")
    def reject(
        approval_id: str,
        principal: Principal = Depends(authenticate),
    ) -> dict[str, Any]:
        require_approver(principal)
        with db.session() as session:
            svc = PolicyService(session)
            try:
                appr = svc.reject(approval_id)
                session.commit()
                return {"approval_id": appr.id, "status": appr.status}
            except QuotaError as exc:
                session.rollback()
                raise HTTPException(status_code=409, detail={"code": exc.code, "message": exc.message})

    @app.post("/api/approvals/{approval_id}/revoke")
    def revoke(
        approval_id: str,
        principal: Principal = Depends(authenticate),
    ) -> dict[str, Any]:
        require_approver(principal)
        with db.session() as session:
            svc = PolicyService(session)
            try:
                appr = svc.revoke(approval_id)
                session.commit()
                return {"approval_id": appr.id, "status": appr.status}
            except QuotaError as exc:
                session.rollback()
                raise HTTPException(status_code=409, detail={"code": exc.code, "message": exc.message})

    @app.patch("/api/tasks/{task_id}/policy")
    @app.post("/api/tasks/{task_id}/policy")
    def update_policy(
        task_id: str,
        body: PolicyIn,
        principal: Principal = Depends(authenticate),
    ) -> dict[str, Any]:
        require_approver(principal)
        # Validate BEFORE opening a transaction: an illegal policy must not
        # write anything (no version bump, no revocation).
        try:
            policy = Policy(
                max_subagents_total=body.max_subagents_total,
                max_subagents_concurrent=body.max_subagents_concurrent,
                max_depth=body.max_depth,
                max_agent_invocations=body.max_agent_invocations,
                collaboration_enabled=body.collaboration_enabled,
                token_budget_total=body.token_budget_total,
                token_budget_per_call=body.token_budget_per_call,
                token_budget_warn_percent=body.token_budget_warn_percent,
                token_budget_mode=body.token_budget_mode,
                model_budgets=body.model_budgets,
            )
        except ValueError as exc:
            raise HTTPException(
                status_code=422,
                detail={"code": "invalid_policy", "message": str(exc)},
            )
        with db.session() as session:
            task = session.get(TaskRow, task_id)
            if task is None:
                raise HTTPException(status_code=404, detail="task not found")
            svc = PolicyService(session)
            try:
                result = svc.update_policy(
                    task.root_task_id, policy, updated_by=principal.identity
                )
            except QuotaError as exc:
                session.rollback()
                raise HTTPException(
                    status_code=404 if exc.code == "no_task" else 409,
                    detail={"code": exc.code, "message": exc.message},
                )
            except ValueError as exc:
                session.rollback()
                raise HTTPException(
                    status_code=422,
                    detail={"code": "invalid_policy", "message": str(exc)},
                )
            return {
                "task_id": task_id,
                "root_task_id": result["root_task_id"],
                "version": result["version"],
                "policy": result["policy"],
                "revoked_approvals": result["revoked_approvals"],
            }

    @app.get("/api/tasks/{task_id}/events")
    def list_events(task_id: str) -> list[dict[str, Any]]:
        with db.session() as session:
            task = session.get(TaskRow, task_id)
            if task is None:
                raise HTTPException(status_code=404, detail="task not found")
            from sqlalchemy import select

            from agenthub_kernel.db import EventRow

            rows = session.scalars(
                select(EventRow).where(EventRow.root_task_id == task.root_task_id)
            ).all()
            return [
                {"seq": r.seq, "type": r.type, "payload": r.payload} for r in rows
            ]

    @app.post("/api/tasks/{task_id}/stop")
    def stop_task(
        task_id: str,
        principal: Principal = Depends(authenticate),
    ) -> dict[str, Any]:
        require_approver(principal)
        with db.session() as session:
            task = session.get(TaskRow, task_id)
            if task is None:
                raise HTTPException(status_code=404, detail="task not found")
            root_task_id = task.root_task_id
        # Persist the stop first, then cancel in-flight calls (the worker owns
        # the adapter). If the process dies between the two, the persisted stop
        # still prevents any new scheduling.
        worker: Worker = app.state.worker
        result = worker.cancel_tree(root_task_id, requested_by=principal.identity)
        return {
            "stopped": True,
            "root_task_id": result["root_task_id"],
            "already_stopped": result["already_stopped"],
            "cancelled": result["attempted"],
            # `attempted` is not `stopped`: the adapter may be unable to cancel at
            # all (Codex today), in which case those calls stay unknown and keep
            # their slots. Both counts are returned so no caller has to guess.
            "confirmed": result.get("confirmed", 0),
            "unconfirmed": result.get("unconfirmed", 0),
            "outcomes": result["outcomes"],
        }

    @app.post("/api/invocations/{invocation_id}/stop-local")
    def stop_local_invocation(
        invocation_id: str,
        principal: Principal = Depends(authenticate),
    ) -> dict[str, Any]:
        require_approver(principal)
        try:
            return app.state.worker.stop_local(invocation_id, requested_by=principal.identity)
        except QuotaError as exc:
            raise HTTPException(status_code=404 if exc.code == "no_invocation" else 409,
                                detail={"code": exc.code, "message": exc.message})

    @app.post("/api/tasks/{task_id}/pause")
    def pause_task(
        task_id: str,
        principal: Principal = Depends(authenticate),
    ) -> dict[str, Any]:
        """Block further scheduling for the tree. Running calls keep running."""
        require_approver(principal)
        with db.session() as session:
            task = session.get(TaskRow, task_id)
            if task is None:
                raise HTTPException(status_code=404, detail="task not found")
            result = PolicyService(session).pause_tree(
                task.root_task_id, requested_by=principal.identity
            )
            return result

    @app.post("/api/tasks/{task_id}/resume")
    def resume_task(
        task_id: str,
        principal: Principal = Depends(authenticate),
    ) -> dict[str, Any]:
        require_approver(principal)
        with db.session() as session:
            task = session.get(TaskRow, task_id)
            if task is None:
                raise HTTPException(status_code=404, detail="task not found")
            result = PolicyService(session).resume_tree(
                task.root_task_id, requested_by=principal.identity
            )
            return result

    @app.post("/api/invocations/{invocation_id}/reconcile")
    def reconcile_invocation(
        invocation_id: str,
        body: ReconcileIn,
        principal: Principal = Depends(authenticate),
    ) -> dict[str, Any]:
        """User-only resolution of an ``unknown`` invocation.

        ``settle`` states what actually happened; ``retry`` supersedes the call
        and creates a NEW pending approval, because a retry is a new call.
        """
        require_approver(principal)
        if body.action not in ("settle", "retry"):
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "bad_action",
                    "message": "action must be 'settle' or 'retry'",
                },
            )
        worker: Worker = app.state.worker
        try:
            return worker.reconcile(
                invocation_id,
                action=body.action,
                outcome=body.outcome,
                actor=principal.identity,
            )
        except QuotaError as exc:
            status = 404 if exc.code in ("no_invocation", "no_task") else 409
            raise HTTPException(
                status_code=status,
                detail={"code": exc.code, "message": exc.message},
            )

    @app.post("/api/worker/reap")
    def reap(
        principal: Principal = Depends(authenticate),
    ) -> dict[str, Any]:
        """Time out invocations that exceeded the run-time limit."""
        require_approver(principal)
        worker: Worker = app.state.worker
        return worker.reap_timeouts()

    @app.post("/api/dev/shutdown")
    def shutdown(
        principal: Principal = Depends(authenticate),
    ) -> dict[str, Any]:
        """MOCK/DEV-ONLY: ask the server to stop gracefully.

        Exists because some environments forbid terminating the process from
        outside (a restricted sandbox denies os.kill / TerminateProcess), which
        leaves a PID-based stop tool unable to finish the job. User-token
        protected, localhost only; a real deployment would not expose this.
        """
        require_approver(principal)
        hook = getattr(app.state, "request_shutdown", None)
        if hook is None:
            return {
                "stopping": False,
                "reason": "no shutdown hook (start via tools/demo/serve.py)",
            }
        hook()
        return {"stopping": True}

    @app.get("/api/adapter")
    def adapter_info(
        principal: Principal = Depends(authenticate),
    ) -> dict[str, Any]:
        """Which adapter is live, and (for Codex) its pinned identity/limits.

        Read-only: lets the console show 'Mock mode' vs 'REAL Codex' and surface
        the safety posture (requested sandbox/disable flag, launch admission)
        without the operator having to trust the page's own text.
        """
        from agenthub_kernel.adapters import describe

        adapter = app.state.adapter
        info = describe(adapter)
        caps = getattr(adapter, "capabilities", None)
        if callable(caps):
            info["capabilities"] = caps()
        return info

    @app.get("/api/invocations/{invocation_id}/adapter-result")
    def adapter_result(
        invocation_id: str,
        principal: Principal = Depends(authenticate),
    ) -> dict[str, Any]:
        """The adapter's own record for one invocation.

        For Codex this is where the parsed thread_id, final reply, real usage and
        the workdir delta come from - the console renders them on the call card.
        """
        adapter = app.state.adapter
        lookup = getattr(adapter, "lookup_by_invocation", None)
        if lookup is None:
            return {"available": False, "reason": "adapter has no execution records"}
        record = lookup(invocation_id)
        if record is None:
            return {"available": False, "reason": "no execution record for this invocation"}

        last = getattr(adapter, "last_result", None)
        summary = last(invocation_id) if callable(last) else None
        payload = record.get("payload") or {}
        events = payload.get("events") or []
        # Derived, non-content fields. A capability probe (and the console) needs to
        # know how many agent sessions a run announced and which item types it
        # produced - never the text inside them.
        thread_ids: list[str] = []
        item_types: dict[str, int] = {}
        for event in events:
            if str(event.get("type")) == "thread.started":
                tid = str(event.get("thread_id") or "")
                if tid and tid not in thread_ids:
                    thread_ids.append(tid)
            elif str(event.get("type")) == "item.completed":
                item_type = str((event.get("item") or {}).get("type") or "")
                if item_type:
                    item_types[item_type] = item_types.get(item_type, 0) + 1
        return {
            "available": True,
            "status": record.get("status"),
            "detail": record.get("detail", ""),
            "summary": summary,
            # Launch evidence. An execution record only exists once the adapter is
            # past its pre-launch refusals, so `launch_attempted` means "a process
            # was asked for"; the fields below say whether one really produced
            # output. The prompt/argv is deliberately NOT echoed back here.
            "launch_attempted": True,
            "evidence": {
                "exit_code": payload.get("exit_code"),
                "timed_out": payload.get("timed_out"),
                "verdict": payload.get("verdict"),
                "event_count": len(payload.get("events") or []),
                "event_types": payload.get("event_types") or {},
                "elapsed": payload.get("elapsed"),
                # Counts, never the messages themselves: a driver error can quote
                # the prompt, and prompts are user content.
                "error_count": len(payload.get("errors") or []),
                "unparsed_count": len(payload.get("unparsed_lines") or []),
                # One entry per announced agent session: more than one means an
                # additional agent session existed. Item types are how a sub-agent
                # tool call or an intercepted command would show up.
                "thread_ids": thread_ids,
                "item_types": item_types,
            },
        }

    @app.get("/api/dev/mock-activity")
    def mock_activity(
        principal: Principal = Depends(authenticate),
    ) -> dict[str, Any]:
        """MOCK-ONLY introspection: what the Mock adapter was asked to run.

        This exists purely so a local trial can SEE the safety valve working
        ("unapproved => start_count stayed 0"). A real adapter would not expose
        this; it is not part of the product contract.
        """
        require_approver(principal)
        worker: Worker = app.state.worker
        adapter = worker.adapter
        store = getattr(adapter, "store", None)
        return {
            "mock_only": True,
            "start_count": getattr(adapter, "start_count", None),
            "started_invocations": sorted(getattr(adapter, "started_invocations", [])),
            "executions": store.all_records() if store is not None else [],
        }

    @app.post("/api/worker/drain")
    def drain(
        principal: Principal = Depends(authenticate),
    ) -> list[dict[str, Any]]:
        """Run one worker pass. User-only: it can start approved calls.

        In a real deployment the Worker is a separate process that polls on its
        own; this endpoint exists so a local trial can step the scheduler.
        """
        require_approver(principal)
        worker: Worker = app.state.worker
        return worker.drain_once()

    @app.post("/api/worker/recover")
    def recover(
        principal: Principal = Depends(authenticate),
    ) -> dict[str, Any]:
        """Reconcile starting/running invocations after a restart.

        Requires the user role: recovery can settle calls and free quota slots,
        so it is not something an agent or worker identity may trigger.
        """
        require_approver(principal)
        worker: Worker = app.state.worker
        return worker.recover()

    return app


def main() -> None:
    import uvicorn

    uvicorn.run(create_app(autopilot_interval=2.0), host="127.0.0.1", port=8080)


app = create_app(autopilot_interval=2.0)
