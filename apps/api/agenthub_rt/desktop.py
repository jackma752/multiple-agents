"""Desktop-only local pairing and authenticated machine configuration."""
import hashlib
from pathlib import Path
import secrets
import time

from fastapi import Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select, text

from agenthub_rt.auth import authenticate, require_approver
from agenthub_kernel import desktop


def register_desktop(app):
    if not desktop.enabled():
        return
    app.state.desktop_ticket = secrets.token_urlsafe(32)
    app.state.desktop_ticket_deadline = time.monotonic() + 180
    app.state.desktop_session = None

    @app.middleware("http")
    async def loopback_only(request, call_next):
        if request.url.hostname not in {"127.0.0.1", "localhost", "[::1]", "::1"}:
            from fastapi.responses import JSONResponse
            return JSONResponse({"detail": "local desktop access only"}, status_code=403)
        return await call_next(request)

    @app.post("/api/desktop/session")
    def pair(request: Request, body: dict):
        from fastapi.responses import JSONResponse
        origin = request.headers.get("origin")
        if origin and origin != str(request.base_url).rstrip("/"):
            raise HTTPException(403, "origin mismatch")
        expected = app.state.desktop_ticket
        if (not expected or time.monotonic() > app.state.desktop_ticket_deadline
                or not secrets.compare_digest(str(body.get("ticket", "")), expected)):
            raise HTTPException(401, "请从开始菜单重新打开 Agent Hub 配对")
        app.state.desktop_ticket = None
        token = secrets.token_urlsafe(32)
        app.state.desktop_session = hashlib.sha256(token.encode()).hexdigest()
        response = JSONResponse({"ok": True})
        response.set_cookie("agenthub_session", token, httponly=True, samesite="strict", path="/")
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/settings", response_class=HTMLResponse)
    def settings_page():
        return (Path(__file__).parent / "static/settings.html").read_text(encoding="utf-8")

    @app.get("/api/desktop/settings")
    def settings(principal=Depends(authenticate)):
        require_approver(principal)
        result = desktop.load()
        result["data_directory"] = str(desktop.home())
        return result

    @app.post("/api/desktop/discover")
    def discover(principal=Depends(authenticate)):
        require_approver(principal)
        return {"candidates": desktop.discover()}

    @app.post("/api/desktop/settings")
    def save(body: dict, principal=Depends(authenticate)):
        require_approver(principal)
        from agenthub_kernel.db import InvocationRow, StageRunRow, ApprovalRow
        # Hold the same writer transaction that Worker claims use. Reconfiguration
        # must not race with a new process claim or an automatic stage tick.
        with app.state.db.session() as session:
            session.execute(text("BEGIN IMMEDIATE"))
            active = session.scalar(select(InvocationRow.id).where(
                InvocationRow.status.in_(["running", "starting", "unknown", "queued"])).limit(1))
            stage = session.scalar(select(StageRunRow.id).where(
                StageRunRow.status.in_(["active", "repairing", "waiting_quota", "paused"])).limit(1))
            if active or stage:
                raise HTTPException(409, "请先停止自动阶段并结束正在运行或状态未知的调用，再修改设置")
            try:
                result = desktop.save_configuration(body)
                adapter = desktop.build_router()
            except (ValueError, OSError, TimeoutError) as exc:
                raise HTTPException(422, str(exc)) from exc
            app.state.adapter = adapter
            app.state.worker.adapter = adapter
            app.state.files_root = result["workspace"]
            for approval in session.scalars(select(ApprovalRow).where(
                    ApprovalRow.status.in_(["pending", "approved"]))):
                approval.status = "revoked"
                inv = session.get(InvocationRow, approval.invocation_id)
                if inv and inv.status in {"draft", "awaiting_approval", "queued"}:
                    inv.status = "revoked"
        return {"ok": True, "enabled_agents": list(result["agents"])}

    @app.post("/api/desktop/shutdown")
    def shutdown(principal=Depends(authenticate)):
        require_approver(principal)
        callback = getattr(app.state, "desktop_shutdown", None)
        if callback:
            callback()
        return {"stopping": bool(callback)}
