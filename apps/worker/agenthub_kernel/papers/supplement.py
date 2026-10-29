"""Missing full texts: one summary request, user choice, timer, batches (P6.1/P6.2).

Rules (``docs/p6-fulltext-supplementation.md``):

* the request is created once per task input version, only when every included
  paper's fetch/parse check has finished; it lists ALL missing papers at once;
* 方案一 (``continue_allowed``) asks first, with a 30-minute deadline that is
  stored, never restarted, and cleared the moment the user chooses to supplement;
* 方案二 (``wait_for_fulltext``) waits without a deadline;
* a submitted batch is validated as a whole; if it does not resolve everything,
  the user is asked ONCE whether to continue, without any deadline.

Every transition is one compare-and-set on the request status inside the
caller's ``BEGIN IMMEDIATE`` transaction, so a timeout and a user's choice cannot
both win. Nothing here calls a model.
"""

from __future__ import annotations

import datetime as dt
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from agenthub.papers import (
    CHOICE_CONTINUE, CHOICE_SUPPLEMENT, CHOICE_TIMEOUT_SECONDS, DECISION_COMPLETE, DECISION_TIMEOUT,
    FILE_ACCEPTED, FILE_AMBIGUOUS, FILE_MISMATCH, FILE_RECEIVED, MISSING_REASONS, SUP_AWAITING_CHOICE,
    SUP_AWAITING_FILES, SUP_AWAITING_MORE, SUP_RESOLVED, SUP_VALIDATING, choice_transition,
    initial_supplement_state, reference_text, timeout_due, title_tokens,
)
from agenthub_kernel.papers.db import (
    MissingItemRow, PaperRow, ResearchTaskRow, SupplementBatchRow, SupplementFileRow, SupplementRequestRow,
)
from agenthub_kernel.service import QuotaError, new_id

MAX_BATCH_FILES = 30
MATCH_ACCEPT = 0.75
MATCH_AUTO = 0.9


def _iso(value: Optional[dt.datetime]) -> Optional[str]:
    return value.isoformat() + "Z" if value else None


def _log(req: SupplementRequestRow, event: str, at: dt.datetime, **detail: Any) -> None:
    req.history = [*(req.history or []), {"event": event, "at": _iso(at), **detail}]


def paper_meta(paper: PaperRow) -> dict[str, Any]:
    return {"title": paper.title, "authors": list(paper.authors or []), "year": paper.year, "doi": paper.doi,
            "url": paper.url, "venue": paper.venue, "source": paper.source, "source_id": paper.source_id}


def current_request(session: Session, task: ResearchTaskRow) -> Optional[SupplementRequestRow]:
    return session.scalar(select(SupplementRequestRow).where(
        SupplementRequestRow.task_id == task.id, SupplementRequestRow.input_version == task.input_version))


def ensure_request(session: Session, task: ResearchTaskRow, now: dt.datetime) -> Optional[SupplementRequestRow]:
    """The request for the current material set, created once; None when nothing is missing."""
    existing = current_request(session, task)
    if existing is not None:
        return existing
    missing = session.scalars(select(PaperRow).where(
        PaperRow.task_id == task.id, PaperRow.included.is_(True), PaperRow.fulltext_status == "missing")
        .order_by(PaperRow.rank)).all()
    if not missing:
        return None
    policy = (task.request or {}).get("fulltext_policy")
    state = initial_supplement_state(policy)
    req = SupplementRequestRow(
        id=new_id("supreq"), task_id=task.id, input_version=task.input_version, policy=policy, status=state,
        round=1, first_asked_at=now,
        deadline_at=now + dt.timedelta(seconds=CHOICE_TIMEOUT_SECONDS) if state == SUP_AWAITING_CHOICE else None,
        history=[])
    session.add(req)
    for number, paper in enumerate(missing, 1):
        session.add(MissingItemRow(id=new_id("miss"), request_id=req.id, paper_id=paper.id, number=number,
                                   reference=reference_text(number, paper_meta(paper)),
                                   metadata_snapshot=paper_meta(paper), reason=paper.fulltext_reason or "download_failed",
                                   abstract_available=bool(paper.abstract), state="missing"))
    if state == SUP_AWAITING_FILES:
        _open_batch(session, req)
    _log(req, "requested", now, missing=len(missing), state=state, deadline=_iso(req.deadline_at))
    session.flush()
    return req


def _open_batch(session: Session, req: SupplementRequestRow) -> SupplementBatchRow:
    batch = session.scalar(select(SupplementBatchRow).where(SupplementBatchRow.request_id == req.id,
                                                            SupplementBatchRow.round == req.round))
    if batch is None:
        batch = SupplementBatchRow(id=new_id("batch"), request_id=req.id, round=req.round, status="open")
        session.add(batch)
        session.flush()
    return batch


def open_batch(session: Session, req: SupplementRequestRow) -> Optional[SupplementBatchRow]:
    batch = session.scalar(select(SupplementBatchRow).where(SupplementBatchRow.request_id == req.id,
                                                            SupplementBatchRow.round == req.round))
    return batch if batch is not None and batch.status == "open" else None


def remaining(session: Session, req: SupplementRequestRow) -> list[MissingItemRow]:
    return list(session.scalars(select(MissingItemRow).where(
        MissingItemRow.request_id == req.id, MissingItemRow.state == "missing").order_by(MissingItemRow.number)))


def _close(session: Session, req: SupplementRequestRow, decision: str, by: str, now: dt.datetime) -> None:
    req.status, req.decision, req.decided_by, req.decided_at, req.deadline_at = SUP_RESOLVED, decision, by, now, None
    for item in remaining(session, req):
        item.state = "not_supplied"
    batch = open_batch(session, req)
    if batch is not None and not session.scalar(select(SupplementFileRow.id).where(
            SupplementFileRow.batch_id == batch.id)):
        session.delete(batch)  # an empty, never-submitted batch leaves no trace
    _log(req, "resolved", now, decision=decision, by=by)


def choose(session: Session, req: SupplementRequestRow, choice: str, *, user: str, now: dt.datetime) -> dict:
    """A user's answer; the caller holds the write lock. Repeating the same answer is a no-op."""
    if req.status == SUP_RESOLVED:
        if choice == CHOICE_CONTINUE:
            return {"changed": False, "status": req.status}
        raise QuotaError("request_closed", "补充请求已关闭（" + (req.decision or "") + "）；迟到资料不会悄悄并入已在分析的版本")
    if req.status == SUP_AWAITING_FILES and choice == CHOICE_SUPPLEMENT:
        return {"changed": False, "status": req.status}  # already waiting for files
    if req.status == SUP_VALIDATING:
        raise QuotaError("validating", "本批资料正在核对，完成后再选择")
    try:
        state, decision = choice_transition(req.status, choice)
    except ValueError as exc:
        raise QuotaError("choice_not_available", str(exc))
    if decision is not None:
        _close(session, req, decision, user, now)
    else:
        if req.status == SUP_AWAITING_MORE:
            req.round += 1
        req.status = state
        req.deadline_at = None  # chosen to supplement: no automatic continuation any more
        req.supplement_chosen_at = req.supplement_chosen_at or now
        _open_batch(session, req)
        _log(req, "supplement_chosen", now, by=user, round=req.round)
    session.flush()
    return {"changed": True, "status": req.status}


def apply_timeout(session: Session, req: SupplementRequestRow, now: dt.datetime) -> bool:
    """方案一's single default: no answer by the stored deadline. Caller holds the write lock."""
    if not timeout_due(req.status, req.deadline_at, now):
        return False
    _close(session, req, DECISION_TIMEOUT, "timeout", now)
    session.flush()
    return True


def begin_supplement(session: Session, req: SupplementRequestRow, *, user: str, now: dt.datetime) -> SupplementBatchRow:
    """Entry of every upload/reference: atomically records "supplement" first, then returns the open batch."""
    if req.status in (SUP_AWAITING_CHOICE, SUP_AWAITING_MORE):
        choose(session, req, CHOICE_SUPPLEMENT, user=user, now=now)
    if req.status != SUP_AWAITING_FILES:
        if req.status == SUP_RESOLVED:
            raise QuotaError("request_closed", "补充请求已关闭（" + (req.decision or "") + "）；不会把迟到资料并入当前版本")
        raise QuotaError("validating", "本批资料正在核对，完成后再补充")
    batch = open_batch(session, req) or _open_batch(session, req)
    count = len(session.scalars(select(SupplementFileRow.id).where(SupplementFileRow.batch_id == batch.id)).all())
    if count >= MAX_BATCH_FILES:
        raise QuotaError("batch_full", f"一批最多 {MAX_BATCH_FILES} 份资料")
    return batch


def _target(session: Session, req: SupplementRequestRow, paper_id: Optional[str]) -> Optional[str]:
    if not paper_id:
        return None
    item = session.scalar(select(MissingItemRow).where(MissingItemRow.request_id == req.id,
                                                       MissingItemRow.paper_id == paper_id))
    if item is None:
        raise QuotaError("unknown_target", "目标论文不在本次缺失清单中")
    return paper_id


def add_file(session: Session, req: SupplementRequestRow, batch: SupplementBatchRow, *, kind: str, origin: str,
             target: Optional[str], rel_path: Optional[str] = None, sha256: Optional[str] = None,
             size: Optional[int] = None) -> SupplementFileRow:
    row = SupplementFileRow(batch_id=batch.id, kind=kind, origin_ref=origin[:1000],
                            target_paper_id=_target(session, req, target), rel_path=rel_path, sha256=sha256,
                            size=size, status=FILE_RECEIVED)
    session.add(row)
    session.flush()
    return row


def submit(session: Session, req: SupplementRequestRow, batch_id: str, *, user: str, now: dt.datetime) -> dict:
    batch = session.get(SupplementBatchRow, batch_id)
    if batch is None or batch.request_id != req.id:
        raise QuotaError("batch_not_found", "batch not found")
    if batch.status != "open":
        return {"changed": False, "status": req.status, "batch_status": batch.status}  # repeated submit
    if req.status != SUP_AWAITING_FILES:
        raise QuotaError("request_closed", "补充请求不在等待资料状态")
    files = session.scalars(select(SupplementFileRow).where(SupplementFileRow.batch_id == batch.id)).all()
    if not files:
        raise QuotaError("empty_batch", "本批还没有任何资料")
    batch.status, batch.submitted_at = "submitted", now
    req.status = SUP_VALIDATING
    _log(req, "batch_submitted", now, by=user, round=batch.round, files=len(files))
    session.flush()
    return {"changed": True, "status": req.status, "batch_status": batch.status}


# --------------------------------------------------------------------------- #
# validation of a submitted batch (the tool step's commit half)
# --------------------------------------------------------------------------- #


HEAD_CHARS = 1500


def title_score(title: str, head: str) -> float:
    """1.0 when the whole title appears near the top; word overlap alone tops out at 0.85.

    So an untargeted file is matched automatically only by its exact title,
    while a file the user aimed at a paper can be confirmed by strong overlap.
    """
    top = head[:HEAD_CHARS]
    wanted = " ".join(title_tokens(title))
    if wanted and wanted in " ".join(title_tokens(top)):
        return 1.0
    words = [w for w in title_tokens(title) if len(w) >= 3 or "一" <= w <= "鿿"]
    if not words:
        return 0.0
    hay = set(title_tokens(top))
    return 0.85 * sum(1 for w in words if w in hay) / len(words)


def match(head: str, items: list[MissingItemRow], target: Optional[str]) -> tuple[str, Optional[str], str]:
    """``(status, paper_id, detail)`` from a file's leading text; never from its file name."""
    scores = sorted(((title_score(i.metadata_snapshot.get("title", ""), head), i) for i in items),
                    key=lambda p: -p[0])
    if target:
        mine = next((s for s, i in scores if i.paper_id == target), 0.0)
        other = next(((s, i) for s, i in scores if i.paper_id != target), (0.0, None))
        if mine >= MATCH_ACCEPT:
            return FILE_ACCEPTED, target, f"题名匹配 {mine:.0%}"
        if other[1] is not None and other[0] >= MATCH_ACCEPT:
            return FILE_MISMATCH, None, f"内容似乎是另一篇缺失论文 [{other[1].number}]，不是所选论文"
        return FILE_MISMATCH, None, f"文件开头未找到所选论文的题名（匹配 {mine:.0%}），可能是错论文"
    strong = [(s, i) for s, i in scores if s >= MATCH_ACCEPT]
    if len(strong) == 1 and strong[0][0] >= MATCH_AUTO:
        return FILE_ACCEPTED, strong[0][1].paper_id, f"按内容题名自动匹配 [{strong[0][1].number}] {strong[0][0]:.0%}"
    if strong:
        return FILE_AMBIGUOUS, None, "可能对应：" + "、".join(f"[{i.number}]" for _, i in strong[:4]) + "；请在下一批指定目标论文"
    return FILE_MISMATCH, None, "未匹配到任何缺失论文的题名"


def finish_validation(session: Session, req: SupplementRequestRow, batch: SupplementBatchRow,
                      now: dt.datetime) -> dict:
    """After every file got its status: one remaining list, one follow-up question (or resolved)."""
    files = session.scalars(select(SupplementFileRow).where(SupplementFileRow.batch_id == batch.id)).all()
    left = remaining(session, req)
    summary = {"files": len(files), "accepted": sum(f.status == FILE_ACCEPTED for f in files),
               "problems": sum(f.status not in (FILE_ACCEPTED,) for f in files), "remaining": len(left)}
    batch.status, batch.validated_at, batch.result = "validated", now, summary
    if not left:
        _close(session, req, DECISION_COMPLETE, "validation", now)
    else:
        req.status = SUP_AWAITING_MORE  # asked once for this batch; no deadline
        _log(req, "batch_incomplete", now, round=batch.round, remaining=len(left))
    session.flush()
    return summary


def request_view(session: Session, req: Optional[SupplementRequestRow], now: dt.datetime) -> Optional[dict]:
    if req is None:
        return None
    items = session.scalars(select(MissingItemRow).where(MissingItemRow.request_id == req.id)
                            .order_by(MissingItemRow.number)).all()
    batches = session.scalars(select(SupplementBatchRow).where(SupplementBatchRow.request_id == req.id)
                              .order_by(SupplementBatchRow.round)).all()
    out_batches = []
    for b in batches:
        files = session.scalars(select(SupplementFileRow).where(SupplementFileRow.batch_id == b.id)
                                .order_by(SupplementFileRow.id)).all()
        out_batches.append({"batch_id": b.id, "round": b.round, "status": b.status, "result": b.result,
                            "submitted_at": _iso(b.submitted_at), "files": [
                                {"file_id": f.id, "kind": f.kind, "origin": f.origin_ref, "target_paper_id": f.target_paper_id,
                                 "status": f.status, "detail": f.detail, "matched_paper_id": f.matched_paper_id,
                                 "size": f.size} for f in files]})
    remaining_seconds = None
    if req.deadline_at is not None:
        remaining_seconds = max(0, int((req.deadline_at - now).total_seconds()))
    return {
        "request_id": req.id, "status": req.status, "policy": req.policy, "round": req.round,
        "first_asked_at": _iso(req.first_asked_at), "deadline_at": _iso(req.deadline_at),
        "remaining_seconds": remaining_seconds, "decision": req.decision, "decided_by": req.decided_by,
        "decided_at": _iso(req.decided_at), "supplement_chosen_at": _iso(req.supplement_chosen_at),
        "choices": {"awaiting_choice": ["supplement", "continue"], "awaiting_files": ["continue"],
                    "awaiting_more_choice": ["supplement", "continue"]}.get(req.status, []),
        "items": [{"item_id": i.id, "paper_id": i.paper_id, "number": i.number, "reference": i.reference,
                   "reason": i.reason, "reason_text": MISSING_REASONS.get(i.reason, i.reason),
                   "abstract_available": i.abstract_available, "state": i.state, "batch_id": i.batch_id}
                  for i in items],
        "remaining": sum(i.state == "missing" for i in items),
        "batches": out_batches, "history": req.history or [],
        "open_batch_id": (open_batch(session, req).id if open_batch(session, req) else None),
    }


def references_text(session: Session, req: SupplementRequestRow, *, only_missing: bool = True) -> str:
    items = session.scalars(select(MissingItemRow).where(MissingItemRow.request_id == req.id)
                            .order_by(MissingItemRow.number)).all()
    lines = []
    for i in items:
        if only_missing and i.state != "missing":
            continue
        lines.append(f"{i.reference}    （缺失原因：{MISSING_REASONS.get(i.reason, i.reason)}；"
                     f"摘要{'可读' if i.abstract_available else '不可得'}）")
    return "\n".join(lines) + ("\n" if lines else "")
