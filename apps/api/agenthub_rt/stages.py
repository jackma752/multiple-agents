"""Stage endpoints (P4.6): the user starts, pauses, resumes or stops automatic execution.

Only a signed-in user (``require_approver``) can authorize a stage or change
its state; the scheduler (``app.state.autopilot``) does the rest within the
stage's rules. See ``docs/p46-autopilot.md``.
"""

from __future__ import annotations

from typing import Literal, Optional

from fastapi import Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt
from sqlalchemy import text

from agenthub_kernel.autopilot import latest_stage, proven_modes, served_agents, set_status, stage_view, start_stage
from agenthub_kernel.conversations import owned_conversation
from agenthub_kernel.service import QuotaError
from agenthub_rt.auth import Principal, authenticate, require_approver


class StageIn(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    goal: str = Field(min_length=1, max_length=4000)
    criteria: str = Field(default="", max_length=4000)
    execution_mode: Literal["strict", "supervised"]
    supervised_consent: StrictBool = False
    plan: Literal["none", "current", "agent"] = "none"
    agents: list[str] = Field(min_length=1, max_length=10)
    main_agent: str
    review_agent: Optional[str] = None
    collect_agent: Optional[str] = None
    max_calls: StrictInt = 8
    command_prefixes: list[str] = Field(default_factory=list, max_length=20)
    allow_file_changes: StrictBool = False
    retry_limit: StrictInt = 3
    auto_resume: StrictBool = True
    quota_retry_seconds: StrictInt = 300


def register_stages(app, db) -> None:
    def refused(exc: QuotaError) -> HTTPException:
        status = 404 if exc.code in ("stage_not_found", "no_conversation") else 409
        return HTTPException(status, detail={"code": exc.code, "message": exc.message})

    @app.get("/api/conversations/{cid}/stage")
    def get_stage(cid: str, principal: Principal = Depends(authenticate)):
        require_approver(principal)
        adapter = app.state.worker.adapter
        with db.session() as session:
            try:
                owned_conversation(session, cid, principal.identity)
            except QuotaError:
                raise HTTPException(404, detail="conversation not found")
            stage = latest_stage(session, cid)
            return {"stage": stage_view(session, stage) if stage is not None else None,
                    "proven": proven_modes(session, adapter, principal.identity), "agents": list(served_agents(adapter))}

    @app.post("/api/conversations/{cid}/stages")
    def create_stage(cid: str, body: StageIn, principal: Principal = Depends(authenticate)):
        """The user's one authorization for a stage of automatic execution."""
        require_approver(principal)
        with db.session() as session:
            session.execute(text("BEGIN IMMEDIATE"))
            try:
                stage = start_stage(session, cid, principal.identity, body.model_dump(),
                                    adapter=app.state.worker.adapter)
            except ValueError as exc:
                raise HTTPException(422, detail={"code": "invalid_stage", "message": str(exc)})
            except QuotaError as exc:
                raise refused(exc)
            view = stage_view(session, stage)
        app.state.autopilot.kick()
        return view

    @app.post("/api/stages/{stage_id}/{action}")
    def change_stage(stage_id: str, action: Literal["pause", "resume", "stop"],
                     principal: Principal = Depends(authenticate)):
        require_approver(principal)
        with db.session() as session:
            session.execute(text("BEGIN IMMEDIATE"))
            try:
                stage = set_status(session, stage_id, principal.identity, action)
            except QuotaError as exc:
                raise refused(exc)
            view = stage_view(session, stage)
        if action == "stop":
            # Persist the stop before contacting providers. This never stops unrelated calls.
            view["stop_requests"] = app.state.autopilot.interrupt_stage(stage_id)
        app.state.autopilot.kick()
        return view
