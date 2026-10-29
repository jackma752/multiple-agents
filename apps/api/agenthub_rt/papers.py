"""Paper research endpoints (P6) and the ``/papers`` workbench page.

Only the signed-in user (``require_approver``) can authorize a research task,
answer a supplement question, add material or change the stage state; each
call checks that the task is theirs. The scheduler does the rest within the
approved stage and resource rules. Downloads of exports read only files the
task recorded, inside the task's own directory.
"""

from __future__ import annotations

import os
from typing import Literal, Optional

from fastapi import Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt
from sqlalchemy import select, text

from agenthub.papers import ACTION_FETCH_URL, ACTION_READ_LOCAL, ACTION_UPLOAD, check_url, resource_decision, resource_rules_of
from agenthub_kernel.autopilot import set_status
from agenthub_kernel.papers import service, supplement
from agenthub_kernel.papers.db import ArtifactRow, SupplementFileRow, SupplementRequestRow
from agenthub_kernel.service import QuotaError
from agenthub_rt.auth import Principal, authenticate, require_approver

STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "papers.html")
SAFE_SUFFIX = {".pdf": ".pdf", ".txt": ".txt", ".md": ".txt"}


class ResearchIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    client_request_id: str = Field(default="preflight", min_length=1, max_length=100)
    request: dict
    roles: dict
    resources: dict = Field(default_factory=dict)
    execution_mode: Literal["strict", "supervised"] = "strict"
    supervised_consent: StrictBool = False
    max_calls: Optional[StrictInt] = None
    token_budget_total: Optional[StrictInt] = Field(default=None, ge=1)


class ReauthorizeIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    execution_mode: Optional[Literal["strict", "supervised"]] = None
    supervised_consent: StrictBool = False
    max_calls: Optional[StrictInt] = None
    max_agent_invocations: Optional[StrictInt] = None


class ContinueIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    client_request_id: Optional[str] = Field(default=None, max_length=100)
    execution_mode: Optional[Literal["strict", "supervised"]] = None
    supervised_consent: StrictBool = False


class ChoiceIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: str
    choice: Literal["supplement", "continue"]


class BeginIn(BaseModel):
    request_id: str


class RefIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: str
    kind: Literal["local", "url"]
    value: str = Field(min_length=1, max_length=2000)
    target_paper_id: Optional[str] = None


class SubmitIn(BaseModel):
    request_id: str
    batch_id: str


def register_papers(app, db) -> None:
    def refused(exc: QuotaError) -> HTTPException:
        status = 404 if exc.code.endswith("_not_found") else 409
        return HTTPException(status, detail={"code": exc.code, "message": exc.message})

    def clock():
        return app.state.autopilot.clock()

    def store():
        return app.state.autopilot.papers.store

    def request_of(session, task, request_id: str) -> SupplementRequestRow:
        req = session.get(SupplementRequestRow, request_id)
        if req is None or req.task_id != task.id:
            raise QuotaError("request_not_found", "supplement request not found")
        return req

    @app.get("/papers", response_class=HTMLResponse)
    def papers_page() -> HTMLResponse:
        try:
            with open(STATIC, "r", encoding="utf-8") as fh:
                return HTMLResponse(fh.read(), headers={"Cache-Control": "no-store"})
        except OSError:
            raise HTTPException(500, detail="UI asset missing")

    @app.post("/api/papers/preflight")
    def preflight(body: ResearchIn, principal: Principal = Depends(authenticate)):
        require_approver(principal)
        with db.session() as session:
            return service.preflight(session, app.state.worker.adapter, principal.identity, body.model_dump(),
                                     store_root=store().root)

    @app.post("/api/papers")
    def create(body: ResearchIn, principal: Principal = Depends(authenticate)):
        """The user's one authorization of a research stage (after reviewing the preflight)."""
        require_approver(principal)
        if body.client_request_id == "preflight":
            raise HTTPException(422, detail={"code": "invalid_request", "message": "client_request_id is required"})
        with db.session() as session:
            session.execute(text("BEGIN IMMEDIATE"))
            try:
                task = service.create_task(session, app.state.worker.adapter, principal.identity, body.model_dump(),
                                           store_root=store().root)
            except ValueError as exc:
                raise HTTPException(422, detail={"code": "invalid_research", "message": str(exc)})
            except QuotaError as exc:
                raise refused(exc)
            view = service.task_view(session, task, clock())
        app.state.autopilot.kick()
        return view

    @app.get("/api/papers")
    def list_tasks(archived: bool = False, principal: Principal = Depends(authenticate)):
        """Active research by default; ``archived=true`` lists only the archived ones."""
        require_approver(principal)
        with db.session() as session:
            return {"tasks": service.task_list(session, principal.identity, archived=archived)}

    def set_archived(task_id: str, principal: Principal, action: str):
        from agenthub_kernel import archive

        require_approver(principal)
        with db.session() as session:
            session.execute(text("BEGIN IMMEDIATE"))
            try:
                task = service.owned_task(session, task_id, principal.identity)
                if action == "archive":
                    archive.require_idle(session, task.root_task_id)
                    changed = archive.archive(session, principal.identity, "research", task.id)
                    # Its conversation holds the same calls; it goes with it.
                    archive.archive(session, principal.identity, "conversation", task.conversation_id)
                else:
                    changed = archive.unarchive(session, "research", task.id)
                    archive.unarchive(session, "conversation", task.conversation_id)
            except QuotaError as exc:
                raise refused(exc)
            return {"task_id": task_id, "archived": action == "archive", "changed": changed}

    @app.post("/api/papers/{task_id}/archive")
    def archive_task(task_id: str, principal: Principal = Depends(authenticate)):
        return set_archived(task_id, principal, "archive")

    @app.post("/api/papers/{task_id}/unarchive")
    def unarchive_task(task_id: str, principal: Principal = Depends(authenticate)):
        return set_archived(task_id, principal, "unarchive")

    @app.get("/api/papers/{task_id}")
    def get_task(task_id: str, principal: Principal = Depends(authenticate)):
        require_approver(principal)
        with db.session() as session:
            try:
                task = service.owned_task(session, task_id, principal.identity)
            except QuotaError as exc:
                raise refused(exc)
            return service.task_view(session, task, clock())

    @app.post("/api/papers/{task_id}/stage/{action}")
    def change(task_id: str, action: Literal["pause", "resume", "stop"], principal: Principal = Depends(authenticate)):
        require_approver(principal)
        with db.session() as session:
            session.execute(text("BEGIN IMMEDIATE"))
            try:
                task = service.owned_task(session, task_id, principal.identity)
                if not task.stage_id:
                    raise QuotaError("stage_not_found", "no stage")
                set_status(session, task.stage_id, principal.identity, action)
            except QuotaError as exc:
                raise refused(exc)
            stage_id = task.stage_id
        if action == "stop":
            app.state.autopilot.interrupt_stage(stage_id)
        app.state.autopilot.kick()
        with db.session() as session:
            return service.task_view(session, service.owned_task(session, task_id, principal.identity), clock())

    @app.post("/api/papers/{task_id}/reauthorize")
    def reauthorize(task_id: str, body: ReauthorizeIn, principal: Principal = Depends(authenticate)):
        require_approver(principal)
        with db.session() as session:
            session.execute(text("BEGIN IMMEDIATE"))
            try:
                task = service.reauthorize(session, app.state.worker.adapter, principal.identity, task_id,
                                           body.model_dump(exclude_none=True))
            except ValueError as exc:
                raise HTTPException(422, detail={"code": "invalid_research", "message": str(exc)})
            except QuotaError as exc:
                raise refused(exc)
            view = service.task_view(session, task, clock())
        app.state.autopilot.kick()
        return view

    @app.post("/api/papers/{task_id}/continue")
    def continue_research(task_id: str, body: ContinueIn, principal: Principal = Depends(authenticate)):
        """After a tree stop: a new task and stage that reuse everything already finished."""
        from agenthub_kernel.papers.continuation import continue_task

        require_approver(principal)
        with db.session() as session:
            session.execute(text("BEGIN IMMEDIATE"))
            try:
                task = continue_task(session, app.state.worker.adapter, principal.identity, task_id,
                                     body.model_dump(exclude_none=True))
            except QuotaError as exc:
                raise refused(exc)
            view = service.task_view(session, task, clock())
        app.state.autopilot.kick()
        return view

    # -- full-text supplementation -------------------------------------------

    @app.post("/api/papers/{task_id}/supplement/choice")
    def choose(task_id: str, body: ChoiceIn, principal: Principal = Depends(authenticate)):
        require_approver(principal)
        with db.session() as session:
            session.execute(text("BEGIN IMMEDIATE"))
            try:
                task = service.owned_task(session, task_id, principal.identity)
                result = supplement.choose(session, request_of(session, task, body.request_id), body.choice,
                                           user=principal.identity, now=clock())
            except QuotaError as exc:
                raise refused(exc)
        app.state.autopilot.kick()
        return result

    @app.post("/api/papers/{task_id}/supplement/begin")
    def begin(task_id: str, body: BeginIn, principal: Principal = Depends(authenticate)):
        """Called before any file leaves the browser: the "supplement" choice is recorded first."""
        require_approver(principal)
        with db.session() as session:
            session.execute(text("BEGIN IMMEDIATE"))
            try:
                task = service.owned_task(session, task_id, principal.identity)
                batch = supplement.begin_supplement(session, request_of(session, task, body.request_id),
                                                    user=principal.identity, now=clock())
            except QuotaError as exc:
                raise refused(exc)
            return {"batch_id": batch.id, "round": batch.round}

    @app.post("/api/papers/{task_id}/supplement/files")
    async def upload(task_id: str, request_id: str = Form(...), files: list[UploadFile] = File(...),
                     targets: list[str] = Form(default=[]), principal: Principal = Depends(authenticate)):
        require_approver(principal)
        with db.session() as session:
            try:
                task = service.owned_task(session, task_id, principal.identity)
            except QuotaError as exc:
                raise refused(exc)
            rules = resource_rules_of(task.resource_rules)
        ok, why = resource_decision(rules, ACTION_UPLOAD)
        if not ok:
            raise HTTPException(403, detail={"code": why, "message": "本次授权不允许上传资料"})
        if not files or len(files) > supplement.MAX_BATCH_FILES:
            raise HTTPException(422, detail={"code": "bad_files", "message": "一次上传 1-30 个文件"})
        saved = []
        for n, upload_file in enumerate(files):
            data = await upload_file.read(rules.max_download_bytes + 1)
            name = os.path.basename(upload_file.filename or f"file-{n}")[:200]
            if len(data) > rules.max_download_bytes:
                raise HTTPException(413, detail={"code": "too_large", "message": f"{name} 超过允许大小"})
            suffix = SAFE_SUFFIX.get(os.path.splitext(name)[1].lower(), ".bin")
            rel, sha, size = store().save(task_id, "supplements", data, suffix)
            saved.append((name, rel, sha, size, (targets[n] if n < len(targets) else "") or None))
        with db.session() as session:
            session.execute(text("BEGIN IMMEDIATE"))
            try:
                task = service.owned_task(session, task_id, principal.identity)
                req = request_of(session, task, request_id)
                batch = supplement.begin_supplement(session, req, user=principal.identity, now=clock())
                rows = [supplement.add_file(session, req, batch, kind="upload", origin=name, target=target,
                                            rel_path=rel, sha256=sha, size=size)
                        for name, rel, sha, size, target in saved]
            except QuotaError as exc:
                raise refused(exc)
            return {"batch_id": batch.id, "files": [{"file_id": r.id, "origin": r.origin_ref, "status": r.status}
                                                    for r in rows]}

    @app.post("/api/papers/{task_id}/supplement/refs")
    def add_ref(task_id: str, body: RefIn, principal: Principal = Depends(authenticate)):
        require_approver(principal)
        with db.session() as session:
            session.execute(text("BEGIN IMMEDIATE"))
            try:
                task = service.owned_task(session, task_id, principal.identity)
                rules = resource_rules_of(task.resource_rules)
                if body.kind == "local":
                    ok, why = resource_decision(rules, ACTION_READ_LOCAL, body.value.strip())
                else:
                    ok, why = resource_decision(rules, ACTION_FETCH_URL, body.value.strip())
                    if ok:
                        ok, why = check_url(body.value.strip())
                if not ok:
                    raise QuotaError("resource_refused", f"不在本次授权的资料范围内：{why}")
                req = request_of(session, task, body.request_id)
                batch = supplement.begin_supplement(session, req, user=principal.identity, now=clock())
                row = supplement.add_file(session, req, batch, kind=body.kind, origin=body.value.strip(),
                                          target=body.target_paper_id)
            except QuotaError as exc:
                raise refused(exc)
            return {"batch_id": batch.id, "file_id": row.id, "status": row.status}

    @app.delete("/api/papers/{task_id}/supplement/files/{file_id}")
    def remove_file(task_id: str, file_id: int, principal: Principal = Depends(authenticate)):
        require_approver(principal)
        with db.session() as session:
            session.execute(text("BEGIN IMMEDIATE"))
            try:
                task = service.owned_task(session, task_id, principal.identity)
                row = session.get(SupplementFileRow, file_id)
                req = supplement.current_request(session, task)
                batch = supplement.open_batch(session, req) if req is not None else None
                if row is None or batch is None or row.batch_id != batch.id:
                    raise QuotaError("file_not_found", "只能移除尚未提交批次中的资料")
                session.delete(row)
            except QuotaError as exc:
                raise refused(exc)
            return {"removed": file_id}

    @app.post("/api/papers/{task_id}/supplement/submit")
    def submit(task_id: str, body: SubmitIn, principal: Principal = Depends(authenticate)):
        require_approver(principal)
        with db.session() as session:
            session.execute(text("BEGIN IMMEDIATE"))
            try:
                task = service.owned_task(session, task_id, principal.identity)
                result = supplement.submit(session, request_of(session, task, body.request_id), body.batch_id,
                                           user=principal.identity, now=clock())
            except QuotaError as exc:
                raise refused(exc)
        app.state.autopilot.kick()
        return result

    @app.get("/api/papers/{task_id}/supplement/references.txt")
    def missing_references(task_id: str, all_items: bool = False, principal: Principal = Depends(authenticate)):
        require_approver(principal)
        with db.session() as session:
            try:
                task = service.owned_task(session, task_id, principal.identity)
            except QuotaError as exc:
                raise refused(exc)
            req = supplement.current_request(session, task)
            body = supplement.references_text(session, req, only_missing=not all_items) if req else ""
        return Response(body, media_type="text/plain; charset=utf-8",
                        headers={"Content-Disposition": f'attachment; filename="missing-fulltext-{task_id}.txt"'})

    # -- material and results ------------------------------------------------

    @app.get("/api/papers/{task_id}/evidence")
    def evidence(task_id: str, paper_id: Optional[str] = None, principal: Principal = Depends(authenticate)):
        require_approver(principal)
        with db.session() as session:
            try:
                task = service.owned_task(session, task_id, principal.identity)
            except QuotaError as exc:
                raise refused(exc)
            return {"evidence": service.evidence_list(session, task, paper_id)}

    @app.get("/api/papers/{task_id}/chunks/{chunk_id}")
    def chunk(task_id: str, chunk_id: str, principal: Principal = Depends(authenticate)):
        require_approver(principal)
        with db.session() as session:
            try:
                task = service.owned_task(session, task_id, principal.identity)
                return service.chunk_view(session, task, chunk_id)
            except QuotaError as exc:
                raise refused(exc)

    @app.get("/api/papers/{task_id}/artifacts/{name}")
    def artifact(task_id: str, name: str, version: Optional[int] = None, principal: Principal = Depends(authenticate)):
        require_approver(principal)
        with db.session() as session:
            try:
                task = service.owned_task(session, task_id, principal.identity)
            except QuotaError as exc:
                raise refused(exc)
            q = select(ArtifactRow).where(ArtifactRow.task_id == task.id, ArtifactRow.name == name)
            if version is not None:
                q = q.where(ArtifactRow.version == version)
            row = session.scalar(q.order_by(ArtifactRow.version.desc()).limit(1))
            if row is None:
                raise HTTPException(404, detail={"code": "artifact_not_found", "message": "artifact not found"})
            rel, sha = row.rel_path, row.sha256
        try:
            data = store().read(rel, sha)
        except (OSError, ValueError):
            raise HTTPException(410, detail={"code": "artifact_changed", "message": "导出文件已被移动或修改"})
        media = "text/markdown; charset=utf-8" if name.endswith(".md") else "application/json"
        return Response(data, media_type=media, headers={"Content-Disposition": f'attachment; filename="{name}"'})
