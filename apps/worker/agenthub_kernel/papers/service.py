"""Research tasks: preflight, the user's one authorization, re-authorization and views.

Creating a task is the user's authorization of one paper stage: the request,
roles/models, call limit, token budget and resource rules are shown by
:func:`preflight` first and then bound into the stage rules hash. The same
P4.6 preconditions apply as for any stage (a manual success per agent and
mode, supervised consent, one open stage per tree). Limits are what the user
entered; nothing here raises them on its own.
"""

from __future__ import annotations

import datetime as dt
import os
from typing import Any, Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from agenthub.contracts import Policy
from agenthub.models import validate_selection
from agenthub.papers import (
    LANGUAGES, MISSING_REASONS, PHASES, ResearchRequest, ResourceRules, SOURCE_HOSTS, SUP_OPEN, SUP_VALIDATING,
    TASK_BLOCKED, TASK_COMPLETED, TASK_PAUSED, TASK_RUNNING, TASK_STOPPED, TASK_WAITING_INPUT, URL_SUPPLEMENT_OFF,
    URL_SUPPLEMENT_PUBLIC, estimate_calls,
)
from agenthub.stage import (
    MAX_STAGE_CALLS, PLAN_PAPER, STAGE_ACTIVE, STAGE_BLOCKED, STAGE_COMPLETED, STAGE_ENDED, STAGE_PAUSED,
    STAGE_STOPPED, StageRules, ToolRules,
)
from agenthub_kernel.adapters import adapter_binding
from agenthub_kernel.archive import archived_ids
from agenthub_kernel.autopilot import _event, open_stage, proven_modes, served_agents, stage_view
from agenthub_kernel.db import ConversationRow, InvocationRow, StageRunRow, TaskRow
from agenthub_kernel.papers import supplement
from agenthub_kernel.papers.export import usage_of
from agenthub_kernel.papers.db import (
    ArtifactRow, EvidenceRow, PaperChunkRow, PaperDocumentRow, PaperRow, PaperStepRow, ResearchTaskRow, SectionRow,
    ensure_schema,
)
from agenthub_kernel.papers.parse import extractor_version
from agenthub_kernel.papers.templates import TEMPLATES
from agenthub_kernel.service import PolicyService, QuotaError, new_id

RETRY_LIMIT = 2


def _roles(data: dict, served: tuple) -> dict:
    main = data.get("main_agent")
    review = data.get("review_agent") or None
    if not main or main not in served:
        raise ValueError("请选择一个已接入的主 Agent（阅读、分析与写作）")
    if review is not None and review not in served:
        raise ValueError("审阅 Agent 未接入")
    agents = [main] + ([review] if review and review != main else [])
    models = {}
    for agent, sel in (data.get("models") or {}).items():
        if agent not in agents:
            raise ValueError("models must be selections for the chosen agents")
        if not isinstance(sel, dict) or set(sel) - {"model", "reasoning_effort"}:
            raise ValueError("invalid model selection")
        sel = {k: v for k, v in sel.items() if v}
        validate_selection(**sel)
        if sel:
            models[agent] = sel
    return {"main_agent": main, "review_agent": review, "agents": agents, "models": models}


def _resources(data: dict, req: ResearchRequest, agents: list) -> ResourceRules:
    roots = []
    for root in data.get("local_roots") or []:
        if not isinstance(root, str) or not os.path.isabs(root):
            raise ValueError("本地目录必须是绝对路径")
        real = os.path.realpath(root)
        if not os.path.isdir(real):
            raise ValueError(f"本地目录不存在：{root}")
        roots.append(real)
    export_dir = (data.get("export_dir") or "").strip()
    if export_dir:
        if not os.path.isabs(export_dir):
            raise ValueError("导出目录必须是绝对路径")
        export_dir = os.path.realpath(export_dir)
        if not os.path.isdir(export_dir):
            raise ValueError(f"导出目录不存在：{export_dir}")
        if not os.access(export_dir, os.W_OK):
            raise ValueError(f"导出目录不可写：{export_dir}")
    return ResourceRules(export_dir=export_dir,sources=req.sources, allow_pdf_download=data.get("allow_pdf_download", True) is not False,
                         max_download_bytes=int(data.get("max_download_mb", 20)) * 1024 * 1024,
                         allow_upload=data.get("allow_upload", True) is not False, local_roots=tuple(roots),
                         url_supplement=URL_SUPPLEMENT_PUBLIC if data.get("allow_url_supplement", True) is not False
                         else URL_SUPPLEMENT_OFF, send_to=tuple(agents))


def preflight(session: Session, adapter: Any, user_id: str, body: dict, *, store_root: str) -> dict:
    """What the user is about to authorize, and what blocks it (no state change)."""
    served = served_agents(adapter)
    blockers, warnings = [], []
    try:
        req = ResearchRequest(**(body.get("request") or {}))
    except (TypeError, ValueError) as exc:
        return {"ok": False, "blockers": [{"code": "invalid_request", "message": str(exc)}]}
    try:
        roles = _roles(body.get("roles") or {}, served)
        resources = _resources(body.get("resources") or {}, req, roles["agents"])
    except (TypeError, ValueError) as exc:
        return {"ok": False, "blockers": [{"code": "invalid_roles", "message": str(exc)}], "agents": list(served)}
    estimate = estimate_calls(req, review=bool(roles["review_agent"]))
    mode = body.get("execution_mode") or "strict"
    proven = proven_modes(session, adapter, user_id)
    for agent in roles["agents"]:
        if mode not in proven.get(agent, []):
            blockers.append({"code": "stage_not_proven", "agent": agent,
                             "message": f"{agent} 需先在聊天页以{'用户监督' if mode == 'supervised' else '严格'}模式"
                                        "手动成功调用一次，才能授权自动研究阶段"})
    max_calls = body.get("max_calls")
    if max_calls is None:  # not entered: a cap from the estimate, never "unlimited" by default
        max_calls = min(MAX_STAGE_CALLS, estimate["worst_case"])
    if max_calls == 0:
        warnings.append("你选择了不限调用次数（0）：仍受 Token 预算、审批规则和修复上限约束，但不会因次数停下")
    elif estimate["expected"] > MAX_STAGE_CALLS:
        blockers.append({"code": "too_many_calls", "message": f"预计 {estimate['expected']} 次调用超过单阶段上限 "
                         f"{MAX_STAGE_CALLS}：请减少篇数或章节"})
    elif max_calls < estimate["expected"]:
        blockers.append({"code": "max_calls_low", "message": f"调用上限 {max_calls} 低于预计 {estimate['expected']} 次"})
    elif max_calls < estimate["worst_case"]:
        warnings.append(f"调用上限 {max_calls} 低于最坏情况 {estimate['worst_case']} 次（含修订与格式修复），"
                        "用完后会停下并说明继续条件")
    if not roles["review_agent"]:
        warnings.append("未选择审阅 Agent：成品的质量状态将是“审阅不可用”，不会标为通过")
    if extractor_version().endswith("(missing)"):
        warnings.append("PDF 解析组件缺失：只能使用摘要和文本资料")
    return {
        "ok": not blockers, "blockers": blockers, "warnings": warnings, "request": req.as_dict(),
        "roles": roles, "execution_mode": mode, "estimate": estimate, "max_calls": max_calls,
        "token_budget_total": body.get("token_budget_total"),
        "resources": {**resources.as_dict(), "hosts": list(resources.hosts()),
                      "sent": f"论文题名、摘要和选中的全文片段会发送给：{', '.join(roles['agents'])}",
                      "downloads": "仅 arXiv 开放 PDF（" + ", ".join(h for s in req.sources for h in SOURCE_HOSTS[s]) + "）"
                                   if resources.allow_pdf_download else "不下载全文"},
        "output": {"location": os.path.join(store_root, "<任务ID>", "exports"),
                   "copy_to": os.path.join(resources.export_dir, "<任务ID>") if resources.export_dir else None,
                   "files": (["review.md", "references.json", "evidence.json", "research-report.json"] if req.mode=="review" else ["study.md", "research-plan.md", "research-report.json"] + (["paper-project.md", "project.json"] if req.mode=="project" else []))},
        "fulltext_policy": req.fulltext_policy, "language": LANGUAGES[req.language],
        "templates": [t.meta() for t in TEMPLATES.values()], "agents": list(served),
        "proven": proven,
    }


def create_task(session: Session, adapter: Any, user_id: str, body: dict, *, store_root: str) -> ResearchTaskRow:
    """The user's authorization. Raises ValueError (bad input) or QuotaError (refused)."""
    ensure_schema(session)
    key = body.get("client_request_id")
    if not isinstance(key, str) or not 1 <= len(key) <= 100:
        raise ValueError("client_request_id is required")
    check = preflight(session, adapter, user_id, body, store_root=store_root)
    existing = session.scalar(select(ResearchTaskRow).where(ResearchTaskRow.user_id == user_id,
                                                            ResearchTaskRow.client_request_id == key))
    if existing is not None:
        if check.get("request") and ResearchRequest(**check["request"]).request_hash() != existing.request_hash:
            raise QuotaError("idempotency_conflict", "request key already used with different content")
        return existing
    if not check["ok"]:
        first = check["blockers"][0]
        if first["code"] in ("invalid_request", "invalid_roles", "max_calls_low", "too_many_calls"):
            raise ValueError(first["message"])
        raise QuotaError(first["code"], first["message"])
    mode = check["execution_mode"]
    if mode not in ("strict", "supervised"):
        raise ValueError("execution_mode must be strict or supervised")
    consent = body.get("supervised_consent") is True
    if mode == "supervised" and not consent:
        raise QuotaError("boundary_consent_required", "请明确同意用户监督边界：原生派生与远端停止不保证受控。")
    req = ResearchRequest(**check["request"])
    roles = check["roles"]
    resources = _resources(body.get("resources") or {}, req, roles["agents"])
    max_calls = check["max_calls"]
    if type(max_calls) is not int or not 0 <= max_calls <= MAX_STAGE_CALLS:
        raise ValueError(f"max_calls must be 0..{MAX_STAGE_CALLS} (0 = unlimited, chosen by you)")
    budget = body.get("token_budget_total")
    policy = Policy(max_agent_invocations=max_calls, token_budget_total=budget if budget else None)

    conversation = ConversationRow(id=new_id("conv"), user_id=user_id, title=("论文·" + req.topic)[:40])
    session.add(conversation)
    session.flush()
    svc = PolicyService(session, default_policy=policy)
    root = svc.create_task(agent_id=roles["main_agent"], goal="论文研究：" + req.topic)
    conversation.root_task_id = root.id
    task = ResearchTaskRow(
        id=new_id("rt"), user_id=user_id, client_request_id=key, conversation_id=conversation.id,
        root_task_id=root.id, request=req.as_dict(), request_hash=req.request_hash(), input_version=1,
        roles=roles, resource_rules=resources.as_dict(), resource_rules_hash=resources.rules_hash(),
        phase="plan", execution_status=TASK_RUNNING, quality_status="pending", outcome={})
    session.add(task)
    session.flush()
    stage = _new_stage(session, adapter, task, user_id, mode=mode, consent=consent, max_calls=max_calls)
    task.stage_id = stage.id
    session.flush()
    return task


def _new_stage(session: Session, adapter: Any, task: ResearchTaskRow, user_id: str, *, mode: str, consent: bool,
               max_calls: int) -> StageRunRow:
    roles = task.roles
    if open_stage(session, task.root_task_id) is not None:
        raise QuotaError("stage_in_progress", "这个研究任务已有进行中的阶段")
    if PolicyService(session)._tree_stopped(task.root_task_id):
        raise QuotaError("tree_stopped", "任务树已被停止")
    rules = StageRules(agents=tuple(roles["agents"]), main_agent=roles["main_agent"], plan=PLAN_PAPER,
                       review_agent=roles.get("review_agent"), collect_agent=None, max_calls=max_calls,
                       tool=ToolRules(), retry_limit=RETRY_LIMIT, auto_resume=True, quota_retry_seconds=300,
                       models=roles.get("models") or {},
                       paper={"task_id": task.id, "request_hash": task.request_hash,
                              "resource_rules": task.resource_rules, "resource_rules_hash": task.resource_rules_hash})
    stage = StageRunRow(
        id=new_id("stage"), conversation_id=task.conversation_id, root_task_id=task.root_task_id,
        status=STAGE_ACTIVE, execution_mode=mode, goal=("论文研究：" + task.request["topic"])[:4000],
        criteria="按批准的检索、阅读、写作与资源规则完成可追溯综述", rules=rules.as_dict(),
        rules_hash=rules.rules_hash(), supervised_consent=consent, granted_by=user_id,
        checkpoint={"phase": "paper", "bindings": {a: adapter_binding(adapter, a) for a in rules.agents}})
    session.add(stage)
    session.flush()
    _event(session, stage, "started", rules_hash=stage.rules_hash, granted_by=user_id, paper_task=task.id)
    return stage


def owned_task(session: Session, task_id: str, user_id: str) -> ResearchTaskRow:
    task = session.get(ResearchTaskRow, task_id)
    if task is None or task.user_id != user_id:
        raise QuotaError("task_not_found", "research task not found")
    return task


def reauthorize(session: Session, adapter: Any, user_id: str, task_id: str, body: dict) -> ResearchTaskRow:
    """A new stage for the same task and tree after a block or stop; finished steps are reused."""
    task = owned_task(session, task_id, user_id)
    old = session.get(StageRunRow, task.stage_id) if task.stage_id else None
    if task.phase == "done" or (old is not None and old.status == STAGE_COMPLETED):
        raise QuotaError("task_completed", "研究任务已完成")
    if old is not None and old.status not in STAGE_ENDED:
        raise QuotaError("stage_in_progress", "当前阶段仍在进行；先暂停/停止或等待它结束")
    if PolicyService(session)._tree_stopped(task.root_task_id):
        raise QuotaError("tree_stopped", "这项研究的任务树已停止（停止本地进程），不能在原任务继续；可用“接续这项研究”复用已完成资料")
    unknown = session.scalar(select(InvocationRow.id).join(TaskRow, TaskRow.id == InvocationRow.task_id).where(
        TaskRow.root_task_id == task.root_task_id, InvocationRow.status == "unknown"))
    if unknown:
        raise QuotaError("status_unknown", f"调用 {unknown} 状态未知：先在会话页人工核对，不会自动重放")
    mode = body.get("execution_mode") or (old.execution_mode if old else "strict")
    consent = body.get("supervised_consent") is True
    if mode == "supervised" and not consent:
        raise QuotaError("boundary_consent_required", "请明确同意用户监督边界")
    proven = proven_modes(session, adapter, user_id)
    missing = [a for a in task.roles["agents"] if mode not in proven.get(a, [])]
    if missing:
        raise QuotaError("stage_not_proven", f"先手动跑通这些 Agent 各一次：{missing}")
    max_calls = body.get("max_calls")
    if max_calls is None:
        max_calls = old.rules.get("max_calls", 20) if old else 20
    if type(max_calls) is not int or not 0 <= max_calls <= MAX_STAGE_CALLS:
        raise ValueError(f"max_calls must be 0..{MAX_STAGE_CALLS} (0 = unlimited)")
    root = session.get(TaskRow, task.root_task_id)
    current = Policy(**{k: v for k, v in (root.policy or {}).items() if k != "version"})
    wanted = body.get("max_agent_invocations")
    if wanted is not None:
        # The user's explicit new tree limit (the whole tree keeps counting; nothing resets).
        # 0 is unlimited, so it is the highest value; a finite value may not go below the current one.
        have = current.max_agent_invocations
        if type(wanted) is not int or wanted < 0 or (wanted != 0 and (have == 0 or wanted < have)):
            raise ValueError("新的总调用上限只能由你提高，且不能低于当前值")
        PolicyService(session).update_policy(root.id, Policy(**{**{k: v for k, v in current.as_dict().items()
                                                                    if k != "version"},
                                                                 "max_agent_invocations": wanted}),
                                             updated_by=user_id)
    stage = _new_stage(session, adapter, task, user_id, mode=mode, consent=consent, max_calls=max_calls)
    task.stage_id, task.execution_status = stage.id, TASK_RUNNING
    session.flush()
    return task


def reexport(session: Session, autopilot: Any, user_id: str, task_id: str) -> ResearchTaskRow:
    """Rebuild the four export files of a finished task from what is stored. No model call.

    Same export code and checks as the pipeline's last step; quality is
    recomputed from the stored sections and reviews, never raised by hand.
    """
    from types import SimpleNamespace

    from agenthub.papers import ACTION_EXPORT, resource_decision, resource_rules_of
    from agenthub.stage import rules_of
    from agenthub_kernel.papers.export import export

    task = owned_task(session, task_id, user_id)
    if task.phase != "done":
        raise QuotaError("not_finished", "只有已完成的研究可以重新导出")
    stage = session.get(StageRunRow, task.stage_id) if task.stage_id else None
    resource = resource_rules_of(task.resource_rules)
    if resource.rules_hash() != task.resource_rules_hash or stage is None:
        raise QuotaError("plan_outside_rules", "资源规则与批准时不一致，不能导出")
    ok, why = resource_decision(resource, ACTION_EXPORT)
    if not ok:
        raise QuotaError("plan_outside_rules", why)
    ctx = SimpleNamespace(session=session, task=task, request=ResearchRequest(**task.request), ap=autopilot,
                          rules=rules_of(stage.rules), resource=resource, now=autopilot.clock())
    export(ctx)
    task.outcome = {**(task.outcome or {}), "reexported_at": _iso(autopilot.clock())}
    session.flush()
    return task


# --------------------------------------------------------------------------- #
# views
# --------------------------------------------------------------------------- #


def task_status(session: Session, task: ResearchTaskRow, stage: Optional[StageRunRow]) -> str:
    if task.phase == "done":
        return TASK_COMPLETED
    if stage is None:
        return TASK_BLOCKED
    if stage.status == STAGE_PAUSED:
        return TASK_PAUSED
    if stage.status == STAGE_STOPPED:
        return TASK_STOPPED
    if stage.status == STAGE_BLOCKED:
        return TASK_BLOCKED
    if stage.status == STAGE_COMPLETED:
        return TASK_COMPLETED
    req = supplement.current_request(session, task)
    if task.phase == "fulltext_gate" and req is not None and req.status in SUP_OPEN and req.status != SUP_VALIDATING:
        return TASK_WAITING_INPUT
    return TASK_RUNNING


def _iso(value: Optional[dt.datetime]) -> Optional[str]:
    return value.isoformat() + "Z" if value else None


def task_list(session: Session, user_id: str, *, archived: bool = False) -> list[dict]:
    hidden = archived_ids(session, user_id, "research")
    out = []
    for task in session.scalars(select(ResearchTaskRow).where(ResearchTaskRow.user_id == user_id)
                                .order_by(ResearchTaskRow.created_at.desc())):
        if (task.id in hidden) != archived:
            continue
        stage = session.get(StageRunRow, task.stage_id) if task.stage_id else None
        out.append({"task_id": task.id, "topic": task.request["topic"], "status": task_status(session, task, stage),
                    "phase": task.phase, "quality_status": task.quality_status, "created_at": _iso(task.created_at)})
    return out


def task_view(session: Session, task: ResearchTaskRow, now: dt.datetime) -> dict:
    stage = session.get(StageRunRow, task.stage_id) if task.stage_id else None
    papers = list(session.scalars(select(PaperRow).where(PaperRow.task_id == task.id).order_by(PaperRow.rank)))
    evidence_counts = dict(session.execute(select(EvidenceRow.paper_id, func.count()).where(
        EvidenceRow.task_id == task.id).group_by(EvidenceRow.paper_id)).all())
    steps = list(session.scalars(select(PaperStepRow).where(PaperStepRow.task_id == task.id).order_by(PaperStepRow.id)))
    sections = list(session.scalars(select(SectionRow).where(SectionRow.task_id == task.id).order_by(SectionRow.idx)))
    artifacts = list(session.scalars(select(ArtifactRow).where(ArtifactRow.task_id == task.id)
                                     .order_by(ArtifactRow.version.desc(), ArtifactRow.name)))
    usage = usage_of(session, [i for st in steps for i in (st.invocation_ids or [])])
    req = supplement.current_request(session, task)
    stage_info = stage_view(session, stage) if stage is not None else None
    if stage_info is not None:
        stage_info = {k: stage_info[k] for k in ("stage_id", "status", "summary", "calls_used", "rules_hash",
                                                 "execution_mode")} | {"max_calls": stage.rules.get("max_calls")}
    docs = {d.id: d for d in session.scalars(select(PaperDocumentRow).where(PaperDocumentRow.task_id == task.id))}
    return {
        "task_id": task.id, "conversation_id": task.conversation_id, "root_task_id": task.root_task_id,
        "request": task.request, "roles": task.roles, "resource_rules": task.resource_rules,
        "resource_rules_hash": task.resource_rules_hash, "status": task_status(session, task, stage),
        "phase": task.phase, "phases": (list(PHASES) if task.request.get("mode", "review")=="review" else ["plan","search","screen","fetch","fulltext_gate","read","related_read","directions","export","done"]), "execution_status": "completed" if task.phase == "done" else
        task_status(session, task, stage), "quality_status": task.quality_status,
        "outcome": {k: v for k, v in (task.outcome or {}).items() if k in (
            "plan", "screening", "insufficient", "supplement", "synthesis", "outline", "abstract", "quality_reasons",
            "sample", "missing_citations", "export_copy", "reexported_at", "studies", "directions", "quality_dimensions")},
        "stage": stage_info, "server_time": _iso(now), "usage": usage,
        "usage_by_step": {name: usage_of(session, [i for st in steps if st.name==name for i in (st.invocation_ids or [])])
                          for name in sorted({st.name for st in steps if st.kind=="model"})},
        "papers": [{"paper_id": p.id, "title": p.title, "authors": p.authors, "year": p.year, "url": p.url,
                    "doi": p.doi, "source_id": p.source_id, "abstract_available": bool(p.abstract),
                    "screen": p.screen, "screen_reason": p.screen_reason, "included": p.included,
                    "fulltext_status": p.fulltext_status, "fulltext_reason": p.fulltext_reason,
                    "fulltext_reason_text": MISSING_REASONS.get(p.fulltext_reason or "", p.fulltext_reason),
                    "document": ({"origin": docs[p.document_id].origin, "pages": docs[p.document_id].pages,
                                  "extractor": docs[p.document_id].extractor}
                                 if p.document_id in docs else None),
                    "read_depth": p.read_depth, "read_status": p.read_status, "merged": p.merged,
                    "evidence": evidence_counts.get(p.id, 0)} for p in papers],
        "supplement": supplement.request_view(session, req, now),
        "sections": [{"section_id": s.id, "idx": s.idx, "title": s.title, "goal": s.goal, "status": s.status,
                      "review_status": s.review_status, "revisions": s.revisions, "evidence_ids": s.evidence_ids,
                      "issues": s.issues, "review": s.review, "text": s.text} for s in sections],
        "steps": [{"key": s.key, "kind": s.kind, "name": s.name, "target": s.target, "status": s.status,
                   "attempts": s.attempts, "invocation_ids": s.invocation_ids, "template": s.template,
                   "error": s.error, "finished_at": _iso(s.finished_at)} for s in steps],
        "artifacts": [{"name": a.name, "version": a.version, "sha256": a.sha256, "size": a.size, "final": a.final}
                      for a in artifacts],
        "created_at": _iso(task.created_at),
        # What the page needs to offer the right way forward after a stop or a crash.
        "unknown_calls": list(session.scalars(select(InvocationRow.id).join(TaskRow, TaskRow.id == InvocationRow.task_id)
                                              .where(TaskRow.root_task_id == task.root_task_id,
                                                     InvocationRow.status == "unknown"))),
        "tree_stopped": PolicyService(session)._tree_stopped(task.root_task_id),
        "continued_from": ((task.outcome or {}).get("continued_from") or {}).get("task_id"),
        "continued_to": (task.outcome or {}).get("continued_to"),
        "archived": task.id in archived_ids(session, task.user_id, "research"),
    }


def evidence_list(session: Session, task: ResearchTaskRow, paper_id: Optional[str] = None) -> list[dict]:
    q = select(EvidenceRow).where(EvidenceRow.task_id == task.id)
    if paper_id:
        q = q.where(EvidenceRow.paper_id == paper_id)
    titles = {p.id: p.title for p in session.scalars(select(PaperRow).where(PaperRow.task_id == task.id))}
    return [{"evidence_id": e.id, "paper_id": e.paper_id, "paper_title": titles.get(e.paper_id), "level": e.level,
             "kind": e.kind, "attribution": e.attribution, "statement": e.statement, "quote": e.quote,
             "location": e.location, "verified": e.verified, "chunk_id": e.chunk_id, "invocation_id": e.invocation_id}
            for e in session.scalars(q.order_by(EvidenceRow.paper_id, EvidenceRow.id))]


def chunk_view(session: Session, task: ResearchTaskRow, chunk_id: str) -> dict:
    row = session.get(PaperChunkRow, chunk_id)
    doc = session.get(PaperDocumentRow, row.document_id) if row is not None else None
    if row is None or doc is None or doc.task_id != task.id:
        raise QuotaError("chunk_not_found", "chunk not found")
    return {"chunk_id": row.id, "paper_id": row.paper_id, "pdf_page": row.page, "char_start": row.char_start,
            "char_end": row.char_end, "text": row.text, "document": {"origin": doc.origin, "sha256": doc.sha256,
                                                                    "extractor": doc.extractor}}
