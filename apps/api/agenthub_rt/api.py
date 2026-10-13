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
                )
                inv = svc.create_invocation(task_id=task.id, snapshot=snapshot)
                session.commit()
                return {
                    "task_id": task.id,
                    "depth": task.depth,
                    "invocation_id": inv.id,
                    "payload_hash": inv.payload_hash,
                    "status": "draft",
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
            svc = PolicyService(session)
            svc.stop_tree(task.root_task_id)
            session.commit()
            return {"stopped": True, "root_task_id": task.root_task_id}

    @app.post("/api/worker/drain")
    def drain() -> list[dict[str, Any]]:
        worker: Worker = app.state.worker
        return worker.drain_once()

    return app


def main() -> None:
    import uvicorn

    uvicorn.run(create_app(), host="127.0.0.1", port=8080)


app = create_app()
