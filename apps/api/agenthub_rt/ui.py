"""Read-model + single-page UI for the Agent Hub Mock console.

The plan (§5) suggests React + TypeScript for the console. That needs a Node
toolchain and a package install; this environment has no npm and an unreliable
network, so P3 ships a **zero-build single-page UI** served by FastAPI instead.
It is a deliberate, documented deviation: same contract, no build step. See
docs/p3-ui.md.

Everything the page needs comes from two read endpoints, so the UI never asks
the user to copy an id by hand:

    GET /api/tasks            -> list of root tasks (id, goal, counts)
    GET /api/tasks/{id}/view  -> full tree view: tasks, invocations, approvals,
                                 quota, policy, stop/pause state, events

The page keeps task_id / invocation_id / approval_id in memory and passes them
to the existing action endpoints.
"""

from __future__ import annotations

import json
import os
from typing import Any, Optional

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from sqlalchemy import select

from agenthub.contracts import ApprovalStatus, InvocationStatus
from agenthub_kernel.db import (
    ApprovalRow,
    Database,
    EventRow,
    InvocationRow,
    PolicyVersionRow,
    QuotaReservationRow,
    TaskRow,
    TreeQuotaRow,
)

from agenthub_rt.auth import Principal, authenticate

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
INDEX_HTML = os.path.join(STATIC_DIR, "index.html")


def _task_view(db: Database, root_task_id: str) -> dict[str, Any]:
    with db.session() as s:
        root = s.get(TaskRow, root_task_id)
        if root is None:
            raise HTTPException(status_code=404, detail="task not found")

        tasks = s.scalars(
            select(TaskRow).where(TaskRow.root_task_id == root.root_task_id)
        ).all()
        task_ids = [t.id for t in tasks]

        invocations = (
            s.scalars(
                select(InvocationRow)
                .where(InvocationRow.task_id.in_(task_ids))
                .order_by(InvocationRow.created_at)
            ).all()
            if task_ids
            else []
        )
        inv_ids = [i.id for i in invocations]

        approvals = (
            s.scalars(
                select(ApprovalRow)
                .where(ApprovalRow.invocation_id.in_(inv_ids))
                .order_by(ApprovalRow.created_at)
            ).all()
            if inv_ids
            else []
        )

        quota = s.get(TreeQuotaRow, root.root_task_id)
        reservations_open = (
            s.scalar(
                select(QuotaReservationRow)
                .where(
                    QuotaReservationRow.root_task_id == root.root_task_id,
                    QuotaReservationRow.released_at.is_(None),
                )
                .limit(1)
            )
            is not None
        )
        open_reservations = len(
            [
                r
                for r in s.scalars(
                    select(QuotaReservationRow).where(
                        QuotaReservationRow.root_task_id == root.root_task_id,
                        QuotaReservationRow.released_at.is_(None),
                    )
                ).all()
            ]
        )
        versions = s.scalars(
            select(PolicyVersionRow)
            .where(PolicyVersionRow.root_task_id == root.root_task_id)
            .order_by(PolicyVersionRow.version)
        ).all()
        events = s.scalars(
            select(EventRow)
            .where(EventRow.root_task_id == root.root_task_id)
            .order_by(EventRow.seq.desc())
            .limit(50)
        ).all()

        policy = dict(root.policy or {})
        stopped_event = s.scalars(
            select(EventRow)
            .where(
                EventRow.root_task_id == root.root_task_id,
                EventRow.type == "tree_stop",
            )
            .limit(1)
        ).first()
        pause_events = s.scalars(
            select(EventRow)
            .where(
                EventRow.root_task_id == root.root_task_id,
                EventRow.type.in_(["tree_pause", "tree_resume"]),
            )
            .order_by(EventRow.seq.desc())
            .limit(1)
        ).all()
        paused = bool(pause_events) and pause_events[0].type == "tree_pause"

        return {
            "root": {
                "id": root.id,
                "goal": root.goal,
                "agent_id": root.agent_id,
                "policy_version": root.policy_version,
            },
            "tasks": [
                {
                    "id": t.id,
                    "parent_task_id": t.parent_task_id,
                    "depth": t.depth,
                    "kind": t.kind,
                    "goal": t.goal,
                    "agent_id": t.agent_id,
                    "status": t.status,
                }
                for t in tasks
            ],
            "invocations": [
                {
                    "id": i.id,
                    "task_id": i.task_id,
                    "status": i.status,
                    "payload_hash": i.payload_hash,
                    "adapter_invocation_id": i.adapter_invocation_id,
                    "started_at": i.started_at.isoformat() if i.started_at else None,
                    "finished_at": i.finished_at.isoformat() if i.finished_at else None,
                    "error": i.error,
                }
                for i in invocations
            ],
            "approvals": [
                {
                    "id": a.id,
                    "invocation_id": a.invocation_id,
                    "status": a.status,
                    "created_at": a.created_at.isoformat() if a.created_at else None,
                    "expires_at": a.expires_at.isoformat() if a.expires_at else None,
                }
                for a in approvals
            ],
            "quota": {
                "launched_total": quota.launched_total if quota else 0,
                "concurrent": quota.concurrent if quota else 0,
                "total_invocations": quota.total_invocations if quota else 0,
                "open_reservations": open_reservations,
            },
            "policy": policy,
            "policy_versions": [v.version for v in versions],
            "stopped": stopped_event is not None,
            "paused": paused,
            "events": [
                {"seq": e.seq, "type": e.type, "payload": e.payload} for e in events
            ],
        }


def register_ui(app: FastAPI, db: Database) -> None:
    """Attach the read-model endpoints and the single-page UI."""

    @app.get("/api/tasks")
    def list_tasks(principal: Principal = Depends(authenticate)) -> list[dict[str, Any]]:
        """Root tasks, newest first, with the counts the sidebar shows."""
        with db.session() as s:
            roots = s.scalars(
                select(TaskRow)
                .where(TaskRow.depth == 0)
                .order_by(TaskRow.created_at.desc())
                .limit(50)
            ).all()
            out = []
            for r in roots:
                tasks = s.scalars(
                    select(TaskRow).where(TaskRow.root_task_id == r.root_task_id)
                ).all()
                tids = [t.id for t in tasks]
                invs = (
                    s.scalars(
                        select(InvocationRow).where(InvocationRow.task_id.in_(tids))
                    ).all()
                    if tids
                    else []
                )
                iids = [i.id for i in invs]
                pending = (
                    s.scalars(
                        select(ApprovalRow).where(
                            ApprovalRow.invocation_id.in_(iids),
                            ApprovalRow.status == ApprovalStatus.PENDING.value,
                        )
                    ).all()
                    if iids
                    else []
                )
                q = s.get(TreeQuotaRow, r.root_task_id)
                stopped = (
                    s.scalars(
                        select(EventRow)
                        .where(
                            EventRow.root_task_id == r.root_task_id,
                            EventRow.type == "tree_stop",
                        )
                        .limit(1)
                    ).first()
                    is not None
                )
                out.append(
                    {
                        "id": r.id,
                        "goal": r.goal,
                        "agent_id": r.agent_id,
                        "depths": max((t.depth for t in tasks), default=0),
                        "invocations": len(invs),
                        "pending_approvals": len(pending),
                        "running": len(
                            [
                                i
                                for i in invs
                                if i.status
                                in (
                                    InvocationStatus.STARTING.value,
                                    InvocationStatus.RUNNING.value,
                                )
                            ]
                        ),
                        "quota": {
                            "launched_total": q.launched_total if q else 0,
                            "concurrent": q.concurrent if q else 0,
                        },
                        "stopped": stopped,
                    }
                )
            return out

    @app.get("/api/tasks/{task_id}/view")
    def task_view(
        task_id: str, principal: Principal = Depends(authenticate)
    ) -> dict[str, Any]:
        """Everything the console renders for one tree, in a single call."""
        with db.session() as s:
            task = s.get(TaskRow, task_id)
            if task is None:
                raise HTTPException(status_code=404, detail="task not found")
            root_id = task.root_task_id
        return _task_view(db, root_id)

    @app.get("/ui", response_class=HTMLResponse)
    def ui() -> HTMLResponse:
        """Serve the zero-build single-page console.

        It is static HTML/JS: it carries no secrets and needs no auth to *load*,
        but every API call it makes requires the bearer token, which the user
        enters once in the page.
        """
        try:
            with open(INDEX_HTML, "r", encoding="utf-8") as fh:
                return HTMLResponse(fh.read())
        except OSError:
            raise HTTPException(status_code=500, detail="UI asset missing")
