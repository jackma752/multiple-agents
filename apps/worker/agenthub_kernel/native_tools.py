"""Claude hook -> existing per-request approval and whole-tree quota gates.

The HTTP adapter authenticates the call and pins session/cwd. This layer trusts
only its kernel-bound workdir, the stored immutable snapshot, and user cards.
No shell/MCP/unknown tools; no native resume, background runs or custom agents.
"""
from __future__ import annotations

import json
import os
import time

from sqlalchemy import select, text

from agenthub.contracts import InvocationSnapshot, TOOL_APPROVAL_DETAIL_CHARS
from agenthub.native import (CLAUDE_NATIVE_TYPES, CLAUDE_PRE_TOOL, CLAUDE_STRICT_TOOLS,
                             NATIVE_APPROVED, native_profile)
from agenthub.workspace import approved_workspace
from agenthub_kernel.db import (InvocationRow, NativeAgentRow, StageGrantRow, StageRunRow,
                                 TaskRow, ToolApprovalRow, ToolGateClosureRow)
from agenthub_kernel.service import PolicyService, new_id, now


def deny(reason="request is not authorized"):
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse",
            "permissionDecision": "deny", "permissionDecisionReason": reason}}


def launch_preflight(session, inv):
    """Refuse impossible native requests BEFORE the main model is launched."""
    permissions = inv.snapshot.get("permissions") or {}
    if native_profile(permissions) != NATIVE_APPROVED:
        return None
    if inv.snapshot.get("agent_id") != "claude-code" or inv.snapshot.get("execution_mode", "strict") != "strict":
        return "native_mode_unavailable", "原生子 Agent 逐次审批仅用于 Claude 严格模式。"
    svc = PolicyService(session)
    policy = svc._quotas(inv)
    task = session.get(TaskRow, inv.task_id)
    if not policy.collaboration_enabled or policy.max_subagents_concurrent < 1 or task.depth >= policy.max_depth:
        return "native_collaboration_disabled", "请先启用本会话协作并设置子 Agent 数量、并发和深度，再重新提交调用。"
    if policy.token_budget_total is not None or policy.model_budgets:
        return "native_budget_unmetered", "原生子 Agent 尚不支持独立 Token 预算预留；请关闭原生子 Agent，或使用 Hub 子任务。"
    return None


def _active(session, invocation_id):
    inv = session.get(InvocationRow, invocation_id)
    if inv is None or inv.status not in ("starting", "running"):
        raise ValueError("call is not active")
    if session.get(ToolGateClosureRow, invocation_id) is not None:
        raise ValueError("tool decisions are closed")
    snap = InvocationSnapshot(**inv.snapshot)
    if snap.execution_mode != "strict" or snap.payload_hash() != inv.payload_hash:
        raise ValueError("strict snapshot changed")
    task = session.get(TaskRow, inv.task_id)
    root = session.get(TaskRow, task.root_task_id) if task else None
    if root is None or root.policy_version != snap.policy_version:
        raise ValueError("policy changed")
    svc = PolicyService(session)
    if svc._tree_stopped(root.id) or svc._tree_paused(root.id):
        raise ValueError("task tree is stopped or paused")
    grant = session.scalar(select(StageGrantRow).where(StageGrantRow.invocation_id == inv.id))
    if grant is not None:
        from agenthub.stage import STAGE_RUNNABLE, rules_of
        stage = session.get(StageRunRow, grant.stage_id)
        if (stage is None or stage.status not in STAGE_RUNNABLE
                or (stage.expires_at is not None and stage.expires_at <= now())
                or rules_of(stage.rules).rules_hash() != stage.rules_hash):
            raise ValueError("stage is not active")
    return inv, snap, task, svc._quotas(inv), svc


def _inside(path, root):
    try:
        return os.path.commonpath([os.path.normcase(path), os.path.normcase(root)]) == os.path.normcase(root)
    except ValueError:
        return False


def _path(value, root):
    if not isinstance(value, str) or not os.path.isabs(value) or "\0" in value:
        raise ValueError("tool requires an absolute approved path")
    absolute = os.path.abspath(value)
    resolved = os.path.realpath(absolute)
    if os.path.normcase(absolute) != os.path.normcase(resolved) or not _inside(resolved, root):
        raise ValueError("path is outside the approved directory or uses a link")
    parts = os.path.relpath(resolved, root).replace("\\", "/").lower().split("/")
    if os.name == "nt" and any(p != p.rstrip(" .") or ":" in p or
            p.split(".")[0] in {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)),
                                *(f"lpt{i}" for i in range(1, 10))} for p in parts):
        raise ValueError("Windows device paths and path aliases are unavailable")
    if any(p in (".git", ".claude", ".codex", ".agents") for p in parts):
        raise ValueError("execution configuration is protected")
    if os.path.isfile(resolved) and os.stat(resolved).st_nlink > 1:
        raise ValueError("hard-linked files are unavailable in the strict profile")
    return resolved


def _scope(session, invocation_id, event, workdir):
    inv, snap, task, policy, svc = _active(session, invocation_id)
    if snap.agent_id != "claude-code" or not workdir:
        raise ValueError("native bridge is not bound to this call")
    root = approved_workspace(snap) or os.path.realpath(workdir)
    name, args = event.get("tool_name"), event.get("tool_input")
    if name not in CLAUDE_STRICT_TOOLS or not isinstance(args, dict):
        raise ValueError("tool is not in the strict profile")
    if len(json.dumps(event, ensure_ascii=False)) > TOOL_APPROVAL_DETAIL_CHARS:
        raise ValueError("request is too large for a complete approval preview")
    agent_id = event.get("agent_id") or ""
    if not isinstance(agent_id, str) or len(agent_id) > 200:
        raise ValueError("invalid native agent id")
    parent = None
    if agent_id:
        parent = session.scalar(select(NativeAgentRow).where(
            NativeAgentRow.invocation_id == invocation_id,
            NativeAgentRow.provider_agent_id == agent_id,
            NativeAgentRow.state == "running"))
        if parent is None:
            raise ValueError("native caller has no authorized launch")
    depth = (parent.depth if parent else task.depth) + 1
    if name == "Agent":
        if native_profile(snap.permissions) != NATIVE_APPROVED or not policy.collaboration_enabled:
            raise ValueError("native subagents are disabled")
        if args.get("subagent_type") not in CLAUDE_NATIVE_TYPES or not isinstance(args.get("prompt"), str):
            raise ValueError("only built-in new foreground subagents are supported")
        if set(args) - {"prompt", "description", "subagent_type", "model"}:
            raise ValueError("native resume, forks, isolation and background runs are unavailable")
        if parent is not None or depth > policy.max_depth:
            raise ValueError("native depth limit reached")
        if policy.token_budget_total is not None or policy.model_budgets:
            raise ValueError("native subagents cannot yet reserve an independently metered token budget")
        # SubagentStart has no parent tool_use_id. Serialize the short unbound
        # interval, so parallel starts can never be guessed onto the wrong card.
        if session.scalar(select(NativeAgentRow.id).where(
                NativeAgentRow.invocation_id == invocation_id,
                NativeAgentRow.provider_agent_id.is_(None),
                NativeAgentRow.state.in_(("reserved", "unknown")))):
            raise ValueError("previous native launch has not been identified")
    elif name in ("Read", "Write", "Edit"):
        _path(args.get("file_path"), root)
        if name in ("Write", "Edit") and snap.permissions.get("workspace_access") != "workspace-write":
            raise ValueError("workspace is read-only")
    else:
        _path(args.get("path") or os.path.realpath(workdir), root)
        glob = args.get("pattern") if name == "Glob" else args.get("glob")
        if glob and (not isinstance(glob, str) or os.path.isabs(glob)
                     or ".." in glob.replace("\\", "/").split("/")):
            raise ValueError("glob must stay inside the approved search directory")
    return task, policy, svc, agent_id, depth


def claim_native(session, row, workdir):
    """Called INSIDE ToolGate.claim's write transaction, before any allow."""
    event = (row.details or {}).get("native_hook")
    if not isinstance(event, dict):
        return False
    try:
        task, policy, svc, parent, depth = _scope(session, row.invocation_id, event, workdir)
        if event["tool_name"] == "Agent":
            if not svc._reserve_quota_atomic(root_task_id=task.root_task_id, is_subagent=True, policy=policy):
                return False
            session.add(NativeAgentRow(id=new_id("native"), invocation_id=row.invocation_id,
                card_id=row.id, root_task_id=task.root_task_id, parent_agent_id=parent,
                depth=depth, agent_type=event["tool_input"]["subagent_type"], state="reserved"))
            session.flush()
        return True
    except (ValueError, TypeError, KeyError):
        return False


def _end(session, row):
    if row.state != "finished":
        row.state, row.ended_at = "finished", now()
        PolicyService(session)._release_quota_atomic(row.root_task_id)


def observe(gate, event):
    """Only explicit matched start/end evidence updates reservations."""
    with gate.db.session() as session:
        session.execute(text("BEGIN IMMEDIATE"))
        kind = event["hook_event_name"]
        if kind == "SubagentStart":
            agent_id, agent_type = event.get("agent_id"), event.get("agent_type")
            if not isinstance(agent_id, str) or not agent_id or len(agent_id) > 200:
                return
            existing = session.scalar(select(NativeAgentRow).where(
                NativeAgentRow.invocation_id == gate.invocation_id,
                NativeAgentRow.provider_agent_id == agent_id))
            if existing is not None:
                return
            pending = session.scalars(select(NativeAgentRow).where(
                NativeAgentRow.invocation_id == gate.invocation_id,
                NativeAgentRow.provider_agent_id.is_(None), NativeAgentRow.state == "reserved",
                NativeAgentRow.agent_type == agent_type)).all()
            if len(pending) == 1:
                pending[0].provider_agent_id, pending[0].state = agent_id, "running"
            else:
                from agenthub_kernel.tool_approvals import close_decisions
                close_decisions(session, gate.invocation_id, reason="unmatched_native_start", actor="kernel")
                gate.native_violation = True
        elif kind == "SubagentStop":
            row = session.scalar(select(NativeAgentRow).where(
                NativeAgentRow.invocation_id == gate.invocation_id,
                NativeAgentRow.provider_agent_id == event.get("agent_id")))
            if row is not None:
                _end(session, row)
        elif event.get("tool_name") == "Agent":
            card = session.scalar(select(ToolApprovalRow).where(
                ToolApprovalRow.invocation_id == gate.invocation_id,
                ToolApprovalRow.request_key == "claude:" + str(event.get("tool_use_id"))))
            row = session.scalar(select(NativeAgentRow).where(NativeAgentRow.card_id == card.id)) if card else None
            result = event.get("tool_response")
            if row is not None:
                if (kind == "PostToolUse" and isinstance(result, dict) and result.get("status") == "completed"
                        and result.get("agentId") == row.provider_agent_id and row.provider_agent_id):
                    _end(session, row)
                elif row.state != "finished":
                    row.state = "unknown"  # failure/partial output doesn't prove no child ran


def handle(gate, event):
    if event.get("hook_event_name") != "PreToolUse":
        observe(gate, event)
        return {}
    key = event.get("tool_use_id")
    if not isinstance(key, str) or not key or len(key) > 150:
        return deny("invalid tool call identity")
    try:
        with gate.db.session() as session:
            _scope(session, gate.invocation_id, event, gate.native_workdir)
        # ToolApprovalRow has UNIQUE(invocation_id, request_key): a replay
        # cannot reuse its first approval, create another card or take a slot.
        card = gate.open(method=CLAUDE_PRE_TOOL, request_key="claude:" + key,
            thread_id=str(event.get("session_id", "")), turn_id=gate.invocation_id,
            item_id=key, details={"native_hook": event}, available_decisions=["accept", "decline"])
        if not card:
            return deny("request already seen or could not be recorded")
        deadline = time.monotonic() + gate.ttl_seconds
        while time.monotonic() < deadline:
            status = gate.poll(card)
            if status == "pending":
                if not gate.may_answer():
                    break
                time.sleep(0.05)
                continue
            answer = gate.claim(card)
            if answer is None:
                break
            return {"hookSpecificOutput": {"hookEventName": "PreToolUse",
                    "permissionDecision": "allow" if answer == "accept" else "deny"}}
        gate.void(card)
        return deny("request expired, paused or closed before authorization")
    except Exception:  # never reflect paths, prompts, credentials or database errors
        return deny("native authorization could not be verified")
