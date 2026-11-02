"""Continue a research task whose task tree was stopped (P6).

A tree stop is final for that tree: nothing in it is scheduled again. To go on,
the user starts a continuation: a new task tree and stage for the same approved
request and resource rules, carrying over what the stopped task already
finished - papers, stored documents, chunks, evidence, succeeded steps and the
supplement record - so finished searches, reading and writing are not run or
billed again. Unfinished steps (including a call whose outcome was unknown)
are run anew in the new tree; the old call stays recorded as it was.

The continuation gets only what the stopped task had left: its call cap and
token budget minus what the old tree used. It is a user action with the same
preconditions as any stage authorization; it never widens the old limits.
"""

from __future__ import annotations

import json
import re
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from agenthub.contracts import Policy
from agenthub.stage import STAGE_OPEN
from agenthub_kernel.autopilot import _event, proven_modes
from agenthub_kernel.db import (
    ConversationRow, InvocationRow, StageGrantRow, StageRunRow, TaskRow, TreeQuotaRow, UsageLedgerRow,
)
from agenthub_kernel.papers.db import (
    EvidenceRow, MissingItemRow, PaperChunkRow, PaperDocumentRow, PaperRow, PaperStepRow, ResearchTaskRow, SectionRow,
    SupplementBatchRow, SupplementFileRow, SupplementRequestRow,
)
from agenthub_kernel.service import PolicyService, QuotaError, new_id


def _remap(value: Any, ids: dict) -> Any:
    """Every old id inside a JSON value replaced by its new id (ids are unique random strings)."""
    if value is None or not ids:
        return value
    pattern = re.compile("|".join(re.escape(k) for k in sorted(ids, key=len, reverse=True)))
    return json.loads(pattern.sub(lambda m: ids[m.group(0)], json.dumps(value, ensure_ascii=False)))


def _remaining(session: Session, old: ResearchTaskRow, stage: StageRunRow) -> dict:
    root = session.get(TaskRow, old.root_task_id)
    policy = Policy(**{k: v for k, v in (root.policy or {}).items() if k != "version"})
    quota = session.get(TreeQuotaRow, old.root_task_id)
    used_calls = quota.total_invocations if quota is not None else 0
    used_tokens = session.scalar(select(func.coalesce(func.sum(UsageLedgerRow.input_total), 0)
                                        + func.coalesce(func.sum(UsageLedgerRow.output_total), 0))
                                 .where(UsageLedgerRow.root_task_id == old.root_task_id)) or 0
    stage_cap = int((stage.rules or {}).get("max_calls") or 0)
    stage_used = session.scalar(select(func.count()).select_from(StageGrantRow)
                                .where(StageGrantRow.stage_id == stage.id)) or 0
    return {
        "max_agent_invocations": (policy.max_agent_invocations - used_calls) if policy.max_agent_invocations else 0,
        "token_budget_total": (policy.token_budget_total - used_tokens) if policy.token_budget_total else None,
        "max_calls": (stage_cap - stage_used) if stage_cap else 0,
        "used_calls": used_calls, "used_tokens": used_tokens,
    }


def continue_task(session: Session, adapter: Any, user_id: str, task_id: str, body: dict) -> ResearchTaskRow:
    from agenthub_kernel.papers.service import _new_stage, owned_task

    old = owned_task(session, task_id, user_id)
    key = f"continue:{old.id}"
    existing = session.scalar(select(ResearchTaskRow).where(ResearchTaskRow.user_id == user_id,
                                                            ResearchTaskRow.client_request_id == key))
    if existing is not None:
        return existing  # one continuation per stopped task; a repeated click returns it
    if old.phase == "done":
        raise QuotaError("task_completed", "研究任务已完成")
    svc = PolicyService(session)
    if not svc._tree_stopped(old.root_task_id):
        raise QuotaError("not_stopped", "这项研究还能在原任务里继续：停止阶段后用“重新授权继续”即可")
    busy = session.scalar(select(InvocationRow.id).join(TaskRow, TaskRow.id == InvocationRow.task_id).where(
        TaskRow.root_task_id == old.root_task_id, InvocationRow.status.in_(("starting", "running"))))
    if busy:
        raise QuotaError("call_in_flight", f"原任务的调用 {busy} 仍在执行，先等它结束")
    stage = session.get(StageRunRow, old.stage_id)
    if stage is not None and stage.status in STAGE_OPEN:
        from agenthub_kernel.autopilot import set_status

        set_status(session, stage.id, user_id, "stop")  # a stopped tree cannot run it anyway
    mode = body.get("execution_mode") or (stage.execution_mode if stage else "strict")
    consent = body.get("supervised_consent") is True
    if mode == "supervised" and not consent:
        raise QuotaError("boundary_consent_required", "请明确同意用户监督边界：原生派生与远端停止不保证受控。")
    proven = proven_modes(session, adapter, user_id)
    missing = [a for a in old.roles["agents"] if mode not in proven.get(a, [])]
    if missing:
        raise QuotaError("stage_not_proven", f"先手动跑通这些 Agent 各一次：{missing}")
    left = _remaining(session, old, stage)
    old_policy = _old_policy(session, old)
    # 0 means unlimited only when the old limit was unlimited; a finite limit used up stays used up.
    used_up = ((stage is not None and (stage.rules or {}).get("max_calls") and left["max_calls"] < 1)
               or (_old_policy(session, old).max_agent_invocations and left["max_agent_invocations"] < 1)
               or (left["token_budget_total"] is not None and left["token_budget_total"] < 1)
               or (left["token_budget_total"] is not None and old_policy.token_budget_per_call is not None
                   and left["token_budget_total"] < old_policy.token_budget_per_call))
    if used_up:
        raise QuotaError("allowance_used_up", "原研究的调用次数或 Token 预算已用完，不能接续；如需继续请新建研究并重新设定上限")

    conversation = ConversationRow(id=new_id("conv"), user_id=user_id,
                                   title=("论文·" + old.request["topic"])[:36] + "（续）")
    session.add(conversation)
    session.flush()
    root = PolicyService(session, default_policy=Policy(
        max_agent_invocations=left["max_agent_invocations"], token_budget_total=left["token_budget_total"],
        token_budget_per_call=old_policy.token_budget_per_call)
    ).create_task(agent_id=old.roles["main_agent"], goal="论文研究（接续）：" + old.request["topic"])
    conversation.root_task_id = root.id
    new = ResearchTaskRow(
        id=new_id("rt"), user_id=user_id, client_request_id=key, conversation_id=conversation.id,
        root_task_id=root.id, request=old.request, request_hash=old.request_hash, input_version=old.input_version,
        roles=old.roles, resource_rules=old.resource_rules, resource_rules_hash=old.resource_rules_hash,
        phase=old.phase, execution_status="running", quality_status="pending", outcome={})
    session.add(new)
    session.flush()
    ids = _copy_material(session, old, new)
    new.outcome = {**(_remap(old.outcome, ids) or {}),
                   "continued_from": {"task_id": old.id, "used_calls": left["used_calls"],
                                      "used_tokens": left["used_tokens"]}}
    old.outcome = {**(old.outcome or {}), "continued_to": new.id}
    stage_new = _new_stage(session, adapter, new, user_id, mode=mode, consent=consent, max_calls=left["max_calls"])
    new.stage_id = stage_new.id
    _event(session, stage_new, "paper_continued", from_task=old.id)
    session.flush()
    return new


def _old_policy(session: Session, old: ResearchTaskRow) -> Policy:
    root = session.get(TaskRow, old.root_task_id)
    return Policy(**{k: v for k, v in (root.policy or {}).items() if k != "version"})


def _copy_material(session: Session, old: ResearchTaskRow, new: ResearchTaskRow) -> dict:
    ids: dict[str, str] = {}
    papers = list(session.scalars(select(PaperRow).where(PaperRow.task_id == old.id)))
    for p in papers:
        ids[p.id] = new_id("pap")
    docs = list(session.scalars(select(PaperDocumentRow).where(PaperDocumentRow.task_id == old.id)))
    for d in docs:
        ids[d.id] = new_id("doc")
    chunks = list(session.scalars(select(PaperChunkRow).where(PaperChunkRow.document_id.in_([d.id for d in docs]))))
    for c in chunks:
        ids[c.id] = f"chk-{ids[c.document_id][4:]}-{c.seq}"
    evidence = list(session.scalars(select(EvidenceRow).where(EvidenceRow.task_id == old.id)))
    for e in evidence:
        ids[e.id] = "ev-" + new_id("x")[2:12]
    sections = list(session.scalars(select(SectionRow).where(SectionRow.task_id == old.id)))
    for s in sections:
        ids[s.id] = new_id("sec")

    def clone(row: Any, **changes: Any) -> Any:
        data = {c.name: getattr(row, c.name) for c in row.__table__.columns
                if c.name not in ("created_at", "updated_at")}
        data.update(changes)
        return type(row)(**data)

    for p in papers:
        session.add(clone(p, id=ids[p.id], task_id=new.id, document_id=ids.get(p.document_id) if p.document_id else None))
    for d in docs:  # the stored files stay where they are; rows point at the same content hash
        session.add(clone(d, id=ids[d.id], task_id=new.id, paper_id=ids.get(d.paper_id) if d.paper_id else None))
    session.flush()
    for c in chunks:
        session.add(clone(c, id=ids[c.id], document_id=ids[c.document_id],
                          paper_id=ids.get(c.paper_id) if c.paper_id else None))
    for e in evidence:
        session.add(clone(e, id=ids[e.id], task_id=new.id, paper_id=ids[e.paper_id],
                          document_id=ids.get(e.document_id) if e.document_id else None,
                          chunk_id=ids.get(e.chunk_id) if e.chunk_id else None))
    for s in sections:
        session.add(clone(s, id=ids[s.id], task_id=new.id, evidence_ids=_remap(s.evidence_ids, ids),
                          claims=_remap(s.claims, ids), issues=_remap(s.issues, ids), review=_remap(s.review, ids)))
    for st in session.scalars(select(PaperStepRow).where(PaperStepRow.task_id == old.id,
                                                         PaperStepRow.status == "succeeded")):
        # Finished steps keep their keys, results and the calls that produced them (audit).
        session.add(PaperStepRow(task_id=new.id, key=st.key, kind=st.kind, name=st.name,
                                 target=ids.get(st.target, st.target), input_hash=st.input_hash, status=st.status,
                                 attempts=st.attempts, invocation_ids=list(st.invocation_ids or []),
                                 template=st.template, result=_remap(st.result, ids), error=st.error,
                                 started_at=st.started_at, finished_at=st.finished_at))
    req = session.scalar(select(SupplementRequestRow).where(SupplementRequestRow.task_id == old.id,
                                                            SupplementRequestRow.input_version == old.input_version))
    if req is not None:
        ids[req.id] = new_id("supreq")
        session.add(clone(req, id=ids[req.id], task_id=new.id, history=_remap(req.history, ids)))
        session.flush()
        for item in session.scalars(select(MissingItemRow).where(MissingItemRow.request_id == req.id)):
            session.add(clone(item, id=new_id("miss"), request_id=ids[req.id], paper_id=ids[item.paper_id],
                              document_id=ids.get(item.document_id) if item.document_id else None,
                              batch_id=None))
        for batch in session.scalars(select(SupplementBatchRow).where(SupplementBatchRow.request_id == req.id)):
            ids[batch.id] = new_id("batch")
            session.add(clone(batch, id=ids[batch.id], request_id=ids[req.id], result=_remap(batch.result, ids)))
            session.flush()
            for f in session.scalars(select(SupplementFileRow).where(SupplementFileRow.batch_id == batch.id)):
                data = {c.name: getattr(f, c.name) for c in f.__table__.columns if c.name not in ("id", "created_at")}
                data.update(batch_id=ids[batch.id], target_paper_id=ids.get(f.target_paper_id, f.target_paper_id),
                            matched_paper_id=ids.get(f.matched_paper_id, f.matched_paper_id),
                            document_id=ids.get(f.document_id, f.document_id))
                session.add(SupplementFileRow(**data))
    session.flush()
    return ids
