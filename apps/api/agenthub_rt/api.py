"""FastAPI application for the Agent Hub approval & quota kernel (P2).

Exposes the endpoints from AGENT_HUB_PLAN §5 (a pragmatic subset):

    POST   /api/agents                     register an agent
    GET    /api/agents                     list agents
    POST   /api/conversations/{id}/messages  save a message, produce a draft
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

from typing import Any, Optional

from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel

from agenthub.contracts import InvocationSnapshot, Policy
from agenthub_kernel import Worker
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


class PolicyIn(BaseModel):
    max_subagents_total: int = 3
    max_subagents_concurrent: int = 2
    max_depth: int = 1
    max_agent_invocations: int = 10


class MessageIn(BaseModel):
    text: str


class ReconcileIn(BaseModel):
    """User adjudication of an ``unknown`` invocation."""

    action: str  # "settle" | "retry"
    outcome: Optional[str] = None  # required when action == "settle"


def create_app(
    db: Optional[Database] = None,
    db_url: Optional[str] = None,
) -> FastAPI:
    """Build the FastAPI app.

    By default the app opens the **shared** runtime database (a file whose path
    is anchored to the repo root, not the working directory), so the API process
    and a separately launched Worker process see the same data and it survives a
    restart. Tests may pass an explicit ``db`` or ``db_url``.
    """
    from agenthub_kernel import config

    if db is None:
        url = db_url or config.default_db_url()
        if url.startswith("sqlite") and ":memory:" not in url:
            config.ensure_data_dir()
        db = Database(url)
    db.create_all()
    app = FastAPI(title="Agent Hub", version="0.1.0")
    app.state.db = db
    app.state.worker = Worker(db, MockAdapter())

    @app.get("/api/agents")
    def list_agents() -> list[dict[str, Any]]:
        return [{"id": "mock", "name": "Mock Agent", "version": "0.1.0"}]

    @app.post("/api/agents")
    def register_agent(agent: AgentIn) -> dict[str, Any]:
        return agent.model_dump()

    @app.post("/api/conversations")
    def create_conversation() -> dict[str, Any]:
        """Create a chat. P2 keeps conversations in-memory/stateless."""
        import uuid

        return {"conversation_id": f"conv-{uuid.uuid4().hex[:12]}"}

    @app.post("/api/conversations/{cid}/messages")
    def post_message(cid: str, msg: MessageIn) -> dict[str, Any]:
        # In P2 this just echoes; real wiring (message -> draft) is P3.
        return {"conversation_id": cid, "acked": True, "text": msg.text}

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
                    resources=body.resources,
                    # Must carry the tree's CURRENT policy version, otherwise the
                    # consume-time policy check would (correctly) reject this
                    # invocation as approved under a stale policy.
                    policy_version=task.policy_version,
                )
                inv = svc.create_invocation(task_id=task.id, snapshot=snapshot)
                # Plan §5: "POST /api/invocations — 创建待审批调用". Creating the
                # invocation also raises the pending approval, otherwise a call
                # made through the API could never be approved.
                approval = svc.create_approval(invocation_id=inv.id)
                session.commit()
                return {
                    "task_id": task.id,
                    "depth": task.depth,
                    "invocation_id": inv.id,
                    "approval_id": approval.id,
                    "payload_hash": inv.payload_hash,
                    "status": "awaiting_approval",
                }
            except QuotaError as exc:
                session.rollback()
                raise HTTPException(status_code=409, detail={"code": exc.code, "message": exc.message})

    @app.post("/api/approvals/{approval_id}/approve")
    def approve(
        approval_id: str,
        principal: Principal = Depends(authenticate),
    ) -> dict[str, Any]:
        require_approver(principal)
        with db.session() as session:
            svc = PolicyService(session)
            try:
                appr = svc.approve(approval_id)
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
            "outcomes": result["outcomes"],
        }

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

    uvicorn.run(create_app(), host="127.0.0.1", port=8080)


app = create_app()
