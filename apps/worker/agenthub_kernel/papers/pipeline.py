"""The research pipeline as a stage plan (P6).

``advance`` runs inside the Autopilot's ``BEGIN IMMEDIATE`` pass for a stage
whose rules say ``plan == "paper"``. It never calls a model or the network
itself:

* a model step is :meth:`Autopilot._ensure` - an ordinary conversation turn,
  approved only under the stage's rules, started through the consume gate (so
  approval, policy, budget, pause/stop and unknown-state rules all apply) and
  counted in the same root task. A reply that fails validation gets a bounded
  repair call through the same path;
* a tool step (search, download+parse, batch validation) is written as a
  pending :class:`PaperStepRow`; :func:`run_tools` claims it in a short
  transaction (stage still runnable, rules unchanged), runs it outside any
  database lock with every URL / path checked against the resource rules, and
  commits its result.

Every step has an idempotency key (input version, step, target, input hash);
a succeeded step is never re-run, and a reply already produced for it is
reused rather than paid for again.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import time
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Optional

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from agenthub.papers import (
    ACTION_DOWNLOAD, ACTION_EXPORT, ACTION_FETCH_URL, ACTION_READ_LOCAL, ACTION_SEARCH, ACTION_SEND,
    DEPTH_ABSTRACT, DEPTH_FULLTEXT, DEPTH_METADATA, FILE_ACCEPTED, FILE_DUPLICATE,
    FILE_FETCH_FAILED, FILE_MISMATCH, FILE_PARSE_FAILED, FILE_REFUSED, ResearchRequest, SUP_AWAITING_CHOICE,
    SUP_RESOLVED, SUP_VALIDATING, check_url, normalize_title, resource_decision, resource_rules_of,
)
from agenthub.stage import STAGE_COMPLETED, STAGE_RUNNABLE, rules_of
from agenthub_kernel.autopilot import _Blocked, _event
from agenthub_kernel.db import ChatMessageRow, InvocationRow, StageGrantRow, StageRunRow
from agenthub_kernel.papers import arxiv, supplement, validate
from agenthub_kernel.papers.db import (
    EvidenceRow, MissingItemRow, PaperChunkRow, PaperDocumentRow, PaperRow, PaperStepRow, ResearchTaskRow,
    SectionRow, SupplementBatchRow, SupplementFileRow, SupplementRequestRow,
)
from agenthub_kernel.papers.parse import chunk, parse_bytes, quote_in
from agenthub_kernel.papers.templates import ABSTRACT, EXTRACT, OUTLINE, PLAN, REVIEW, SCREEN, SECTION, SYNTHESIZE, render
from agenthub_kernel.papers.validate import FormatError, parse_json
from agenthub_kernel.service import new_id

SCREEN_BATCH = 20
EXTRACT_BUDGET_CHARS = 16_000
MAX_REVISIONS = 1
TOOL_ATTEMPTS = 3
MAX_EVIDENCE_IN_PROMPT = 120
#: Model step failures that may end one paper/section without blocking the stage.
SOFT_REASONS = ("step_failed", "no_result", "format_invalid")


def _h(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)
                          .encode("utf-8")).hexdigest()


class PaperRuntime:
    """Process-wide tool dependencies: file store, fetcher, politeness delay."""

    def __init__(self, store: Any, fetcher: Any, *, polite_delay: float = 3.0) -> None:
        self.store = store
        self.fetcher = fetcher
        self.polite_delay = 0.0 if getattr(fetcher, "simulated", False) else polite_delay
        self.token = uuid.uuid4().hex
        self._last_api = 0.0

    def polite(self) -> None:
        wait = self._last_api + self.polite_delay - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        self._last_api = time.monotonic()


def task_of_stage(session: Session, stage: StageRunRow) -> Optional[ResearchTaskRow]:
    return session.scalar(select(ResearchTaskRow).where(ResearchTaskRow.stage_id == stage.id))


def _checked_rules(stage: StageRunRow, task: ResearchTaskRow):
    rules = rules_of(stage.rules)
    paper = rules.paper or {}
    resource = resource_rules_of(paper.get("resource_rules") or {})
    if (rules.rules_hash() != stage.rules_hash or paper.get("task_id") != task.id
            or paper.get("resource_rules_hash") != task.resource_rules_hash
            or resource.rules_hash() != task.resource_rules_hash
            or paper.get("request_hash") != task.request_hash):
        return None, None
    return rules, resource


# --------------------------------------------------------------------------- #
# step bookkeeping
# --------------------------------------------------------------------------- #


def _step(session: Session, task: ResearchTaskRow, key: str, *, kind: str, name: str,
          target: Optional[str] = None, input_hash: Optional[str] = None,
          payload: Optional[dict] = None) -> PaperStepRow:
    row = session.scalar(select(PaperStepRow).where(PaperStepRow.task_id == task.id, PaperStepRow.key == key))
    if row is None:
        row = PaperStepRow(task_id=task.id, key=key, kind=kind, name=name, target=target, input_hash=input_hash,
                           status="pending", attempts=0, invocation_ids=[],
                           result={"payload": payload} if payload is not None else None)
        session.add(row)
        session.flush()
    return row


@dataclass
class Ctx:
    ap: Any
    session: Session
    stage: StageRunRow
    task: ResearchTaskRow
    rules: Any
    resource: Any
    request: ResearchRequest
    to_start: list

    @property
    def now(self) -> dt.datetime:
        return self.ap.clock()

    @property
    def main(self) -> str:
        return self.rules.main_agent

    def outcome(self, **changes: Any) -> None:
        self.task.outcome = {**(self.task.outcome or {}), **changes}

    def insufficient(self, reason: str, detail: str = "") -> None:
        self.outcome(insufficient={"reason": reason, "detail": detail})
        _event(self.session, self.stage, "paper_insufficient", reason=reason)

    def tool(self, name: str, target: str, input_hash: str, payload: dict) -> PaperStepRow:
        key = f"v{self.task.input_version}:{name}:{target}:{input_hash[:12]}"
        return _step(self.session, self.task, key, kind="tool", name=name, target=target, input_hash=input_hash,
                     payload=payload)

    def model(self, name: str, target: Optional[str], template: Any, data: dict, input_hash: str,
              validator: Callable[[dict], Any], *, agent: Optional[str] = None,
              soft: bool = False) -> tuple[str, Any]:
        """``("done", result) | ("wait", None) | ("failed", None)`` for one model step."""
        agent = agent or self.main
        key = f"v{self.task.input_version}:{name}:{input_hash[:16]}"
        row = _step(self.session, self.task, key, kind="model", name=name.split(":")[0], target=target,
                    input_hash=input_hash)
        if row.status == "succeeded":
            return "done", row.result
        if row.status == "failed":
            return "failed", None
        ok, why = resource_decision(self.resource, ACTION_SEND, agent)
        if not ok:
            raise _Blocked(name, "plan_outside_rules", f"paper material may not be sent to {agent}: {why}")
        row.template = template.meta()
        stage_step = "p:" + name
        reused = self._reuse_previous_stage(row, validator, stage_step)
        if reused is not None:
            return reused
        try:
            inv, state, reply = self.ap._ensure(self.session, self.stage, stage_step, agent,
                                                render(template, data, language=self.request.language),
                                                self.to_start)
        except _Blocked as blocked:
            if soft and blocked.reason in SOFT_REASONS:
                return self._fail(row, blocked.reason, blocked.message)
            raise
        if inv is not None and inv.id not in (row.invocation_ids or []):
            row.invocation_ids = [*(row.invocation_ids or []), inv.id]
            row.attempts = len(row.invocation_ids)
            row.started_at = row.started_at or self.now
        row.status = "running"
        if state != "done":
            return "wait", None
        try:
            result = validator(parse_json(reply.text))
        except FormatError as exc:
            try:
                self.ap._retry(self.session, self.stage, stage_step, inv, f"format_invalid: {str(exc)[:200]}")
            except _Blocked as blocked:
                if soft:
                    return self._fail(row, "format_invalid", str(exc))
                raise blocked
            _event(self.session, self.stage, "paper_format_repair", invocation_id=inv.id, step=name)
            return "wait", None
        return self._succeed(row, result, inv.id)

    def _succeed(self, row: PaperStepRow, result: Any, invocation_id: str) -> tuple[str, Any]:
        row.status, row.result, row.finished_at, row.error = "succeeded", result, self.now, None
        row.result = {**result, "_invocation_id": invocation_id} if isinstance(result, dict) else result
        return "done", row.result

    def _fail(self, row: PaperStepRow, reason: str, message: str) -> tuple[str, Any]:
        row.status, row.error, row.finished_at = "failed", f"{reason}: {message}"[:500], self.now
        _event(self.session, self.stage, "paper_step_failed", step=row.key[:120], reason=reason)
        return "failed", None

    def _reuse_previous_stage(self, row: PaperStepRow, validator: Callable, stage_step: str) -> Optional[tuple]:
        """A reply produced under an earlier stage of this task is used, not bought again."""
        for inv_id in reversed(row.invocation_ids or []):
            grant = self.session.scalar(select(StageGrantRow).where(StageGrantRow.invocation_id == inv_id))
            if grant is None or grant.stage_id == self.stage.id:
                return None  # the current stage tracks its own calls (_ensure)
            inv = self.session.get(InvocationRow, inv_id)
            if inv is None:
                continue
            if inv.status == "unknown":
                raise _Blocked(stage_step, "status_unknown",
                               "An earlier call of this step has an unknown outcome; resolve it first", inv_id)
            if inv.status in ("starting", "running"):
                raise _Blocked(stage_step, "status_unknown", "An earlier call of this step is still running", inv_id)
            reply = self.session.scalar(select(ChatMessageRow).where(ChatMessageRow.invocation_id == inv_id,
                                                                     ChatMessageRow.role == "assistant"))
            if inv.status == "succeeded" and reply is not None:
                try:
                    result = validator(parse_json(reply.text))
                except FormatError:
                    return None
                _event(self.session, self.stage, "paper_reply_reused", invocation_id=inv_id, step=stage_step)
                return self._succeed(row, result, inv_id)
            return None
        return None


# --------------------------------------------------------------------------- #
# phases
# --------------------------------------------------------------------------- #


def _included(ctx: Ctx) -> list[PaperRow]:
    return list(ctx.session.scalars(select(PaperRow).where(PaperRow.task_id == ctx.task.id,
                                                           PaperRow.included.is_(True)).order_by(PaperRow.rank)))


def _years(req: ResearchRequest) -> str:
    return f"{req.year_from or '不限'}-{req.year_to or '不限'}"


def phase_plan(ctx: Ctx) -> Optional[str]:
    req = ctx.request
    data = {"topic": req.topic, "years": _years(req), "target_papers": req.target_papers,
            "source": "arXiv (title/abstract keyword search)"}
    state, result = ctx.model("plan", None, PLAN, data, _h([data, PLAN.sha256]), validate.plan)
    if state != "done":
        return None
    ctx.outcome(plan={k: v for k, v in result.items() if not k.startswith("_")})
    return "search"


def phase_search(ctx: Ctx) -> Optional[str]:
    req, plan = ctx.request, ctx.task.outcome["plan"]
    rows = []
    for index, query in enumerate(plan["queries"]):
        url = arxiv.query_url(query["terms"], year_from=req.year_from, year_to=req.year_to,
                              max_results=req.candidates_per_query)
        rows.append(ctx.tool("search", f"q{index}", _h(url), {"url": url, "terms": query["terms"], "index": index}))
    if any(r.status in ("pending", "running") for r in rows):
        return None
    total = len(ctx.session.scalars(select(PaperRow.id).where(PaperRow.task_id == ctx.task.id)).all())
    if not any(r.status == "succeeded" for r in rows):
        ctx.insufficient("search_failed", "; ".join(sorted({r.error or "" for r in rows}))[:300])
        return "export"
    if total == 0:
        ctx.insufficient("no_results", "arXiv 在给定年份与检索词下没有返回结果")
        return "export"
    return "screen"


def phase_screen(ctx: Ctx) -> Optional[str]:
    req, plan = ctx.request, ctx.task.outcome["plan"]
    papers = list(ctx.session.scalars(select(PaperRow).where(PaperRow.task_id == ctx.task.id)
                                      .order_by(PaperRow.rank)))
    decisions: dict[str, dict] = {}
    for b in range(0, len(papers), SCREEN_BATCH):
        batch = papers[b:b + SCREEN_BATCH]
        ids = [p.id for p in batch]
        data = {"topic": req.topic, "years": _years(req),
                "criteria": {"inclusion": plan.get("inclusion", []), "exclusion": plan.get("exclusion", [])},
                "candidates": [{"paper_id": p.id, "title": p.title, "year": p.year,
                                "abstract": (p.abstract or None) and p.abstract[:1500]} for p in batch]}
        state, result = ctx.model(f"screen:{b // SCREEN_BATCH}", None, SCREEN, data, _h([data, SCREEN.sha256]),
                                  lambda out, ids=ids: {"decisions": validate.screen(out, ids)})
        if state != "done":
            return None
        for pid, d in result["decisions"].items():
            decisions[pid] = {**d, "invocation_id": result.get("_invocation_id")}
    chosen = 0
    for wanted in ("include", "uncertain"):
        for p in papers:
            d = decisions.get(p.id, {})
            if d.get("decision") == wanted and chosen < req.target_papers and not p.included:
                p.included, chosen = True, chosen + 1
    for p in papers:
        d = decisions.get(p.id, {})
        p.screen, p.screen_invocation_id = d.get("decision"), d.get("invocation_id")
        p.screen_reason = d.get("reason")
        if not p.included and d.get("decision") in ("include", "uncertain"):
            p.screen_reason = (p.screen_reason or "") + "（超出目标篇数，未纳入）"
    ctx.outcome(screening={"candidates": len(papers), "included": chosen,
                           "include": sum(d.get("decision") == "include" for d in decisions.values()),
                           "uncertain": sum(d.get("decision") == "uncertain" for d in decisions.values()),
                           "exclude": sum(d.get("decision") == "exclude" for d in decisions.values()),
                           "short_of_target": max(0, req.target_papers - chosen)})
    if chosen == 0:
        ctx.insufficient("no_relevant_papers", "筛选后没有与主题相关的候选论文")
        return "export"
    return "fetch"


def phase_fetch(ctx: Ctx) -> Optional[str]:
    waiting = False
    for p in _included(ctx):
        if p.fulltext_status != "unchecked":
            continue
        if not p.pdf_url:
            p.fulltext_status, p.fulltext_reason = "missing", "no_fulltext_link"
            continue
        if not ctx.resource.allow_pdf_download:
            p.fulltext_status, p.fulltext_reason = "missing", "download_not_approved"
            continue
        row = ctx.tool("fetch", p.id, _h(p.pdf_url), {"paper_id": p.id, "url": p.pdf_url})
        if row.status in ("pending", "running"):
            waiting = True
    return None if waiting else "fulltext_gate"


def _unknown_calls(ctx: Ctx) -> Optional[str]:
    return ctx.session.scalar(select(InvocationRow.id).join(
        StageGrantRow, StageGrantRow.invocation_id == InvocationRow.id).where(
            StageGrantRow.stage_id == ctx.stage.id, InvocationRow.status == "unknown"))


def phase_fulltext_gate(ctx: Ctx) -> Optional[str]:
    req = supplement.ensure_request(ctx.session, ctx.task, ctx.now)
    if req is None:
        return "read"
    if req.status == SUP_AWAITING_CHOICE:
        unknown = _unknown_calls(ctx)
        if unknown:
            raise _Blocked("fulltext_gate", "status_unknown", "An earlier call is unknown; no timeout continuation",
                           unknown)
        if supplement.apply_timeout(ctx.session, req, ctx.now):
            _event(ctx.session, ctx.stage, "paper_supplement_timeout", request_id=req.id)
    if req.status == SUP_VALIDATING:
        batch = ctx.session.scalar(select(SupplementBatchRow).where(
            SupplementBatchRow.request_id == req.id, SupplementBatchRow.status == "submitted"))
        if batch is not None:
            ctx.tool("validate", batch.id, _h(batch.id), {"batch_id": batch.id, "request_id": req.id})
        return None
    if req.status == SUP_RESOLVED:
        ctx.outcome(supplement={"decision": req.decision, "rounds": req.round})
        return "read"
    return None


def _terms(ctx: Ctx) -> list[str]:
    plan = ctx.task.outcome.get("plan") or {}
    return [ctx.request.topic, *plan.get("subtopics", []), *(t for q in plan.get("queries", []) for t in q["terms"])]


def select_chunks(chunks: list[PaperChunkRow], terms: list[str], budget: int = EXTRACT_BUDGET_CHARS) -> list:
    words = {w for t in terms for w in normalize_title(t).split() if len(w) >= 4}
    scored = sorted(chunks, key=lambda c: (-sum(c.text.lower().count(w) for w in words), c.seq))
    chosen, size = set(), 0
    for c in [chunks[0], *scored] if chunks else []:
        if c.id in chosen or size + len(c.text) > budget:
            continue
        chosen.add(c.id)
        size += len(c.text)
    return [c for c in chunks if c.id in chosen]


def _model_of(ctx: Ctx, agent: str) -> Optional[str]:
    return (ctx.rules.models.get(agent) or {}).get("model")


def phase_read(ctx: Ctx) -> Optional[str]:
    if ctx.request.mode != "review":
        from agenthub_kernel.papers.guidance import phase_read as study_read
        return study_read(ctx)
    return phase_related_read(ctx)


def phase_related_read(ctx: Ctx) -> Optional[str]:
    plan = ctx.task.outcome.get("plan") or {}
    for p in _included(ctx):
        if p.id in (ctx.task.outcome or {}).get("studies", {}):
            continue
        if p.read_status in ("done", "failed", "skipped"):
            continue
        doc = ctx.session.get(PaperDocumentRow, p.document_id) if p.document_id else None
        paper = {"paper_id": p.id, "title": p.title, "year": p.year}
        if doc is not None and doc.parse_status == "ok":
            chunks = list(ctx.session.scalars(select(PaperChunkRow).where(PaperChunkRow.document_id == doc.id)
                                              .order_by(PaperChunkRow.seq)))
            picked = select_chunks(chunks, _terms(ctx))
            level = DEPTH_FULLTEXT
            data = {"topic": ctx.request.topic, "subtopics": plan.get("subtopics", []), "paper": paper,
                    "level": level, "coverage": f"{len(picked)} of {len(chunks)} chunks selected by keyword",
                    "chunks": [{"chunk_id": c.id, "pdf_page": c.page, "text": c.text} for c in picked]}
            source = {c.id: c.text for c in picked}
            input_hash = _h([doc.sha256, [c.id for c in picked], EXTRACT.sha256, _model_of(ctx, ctx.main)])
        elif p.abstract:
            level, picked, chunks, source = DEPTH_ABSTRACT, [], [], {}
            data = {"topic": ctx.request.topic, "subtopics": plan.get("subtopics", []), "paper": paper,
                    "level": level, "abstract": p.abstract}
            input_hash = _h([p.abstract, EXTRACT.sha256, _model_of(ctx, ctx.main)])
        else:
            p.read_depth, p.read_status = DEPTH_METADATA, "skipped"
            continue
        state, result = ctx.model(
            f"extract:{p.id}", p.id, EXTRACT, data, input_hash,
            lambda out, src=source, ab=p.abstract or "", lv=level: validate.extract(out, chunks=src, abstract=ab, level=lv),
            soft=True)
        if state == "wait":
            return None
        if state == "failed":
            p.read_status, p.read_depth = "failed", DEPTH_METADATA
            continue
        _save_evidence(ctx, p, doc if level == DEPTH_FULLTEXT else None, level, result, picked)
        p.read_status, p.read_depth = "done", level
    count = len(ctx.session.scalars(select(EvidenceRow.id).where(EvidenceRow.task_id == ctx.task.id)).all())
    if count == 0:
        if ctx.request.mode != "review":
            return "directions"
        ctx.insufficient("no_evidence", "所有纳入论文都没有得到可核对的证据")
        return "export"
    return "directions" if ctx.request.mode != "review" else "synthesize"


def _save_evidence(ctx: Ctx, paper: PaperRow, doc: Optional[PaperDocumentRow], level: str, result: dict,
                   picked: list) -> None:
    by_id = {c.id: c for c in picked}
    for n, claim in enumerate(result.get("claims", [])):
        chunk_row = by_id.get(claim.get("chunk_id")) if claim.get("chunk_id") else None
        location: dict[str, Any] = {"section": "abstract"} if chunk_row is None else {
            "pdf_page": chunk_row.page, "chunk_id": chunk_row.id, "chunk_seq": chunk_row.seq}
        if chunk_row is not None:
            pos = chunk_row.text.lower().find(claim["quote"].lower()[:40])
            if pos >= 0:
                location.update(char_start=chunk_row.char_start + pos,
                                char_end=chunk_row.char_start + pos + len(claim["quote"]))
        source = chunk_row.text if chunk_row is not None else (paper.abstract or "")
        ev_id = "ev-" + _h([ctx.task.id, paper.id, n, claim["quote"], claim.get("chunk_id")])[:10]
        if ctx.session.get(EvidenceRow, ev_id) is None:
            ctx.session.add(EvidenceRow(
                id=ev_id, task_id=ctx.task.id, paper_id=paper.id, document_id=doc.id if doc else None,
                chunk_id=chunk_row.id if chunk_row else None, level=level, kind=claim["kind"],
                statement=claim["statement"], quote=claim["quote"], location=location,
                attribution=claim["attribution"], verified=quote_in(claim["quote"], source),
                invocation_id=result.get("_invocation_id")))
    ctx.session.flush()


def _evidence(ctx: Ctx, ids: Optional[list] = None) -> list[EvidenceRow]:
    q = select(EvidenceRow).where(EvidenceRow.task_id == ctx.task.id)
    if ids is not None:
        q = q.where(EvidenceRow.id.in_(ids))
    return list(ctx.session.scalars(q.order_by(EvidenceRow.created_at, EvidenceRow.id)))


def _sample(ctx: Ctx) -> dict:
    papers = _included(ctx)
    return {"papers": len(papers), "fulltext": sum(p.read_depth == DEPTH_FULLTEXT for p in papers),
            "abstract_only": sum(p.read_depth == DEPTH_ABSTRACT for p in papers),
            "metadata_only": sum(p.read_depth in (DEPTH_METADATA, None) for p in papers),
            "source": "arXiv", "years": _years(ctx.request)}


def phase_synthesize(ctx: Ctx) -> Optional[str]:
    titles = {p.id: (p.title, p.year) for p in _included(ctx)}
    evidence = _evidence(ctx)[:MAX_EVIDENCE_IN_PROMPT]
    known = {e.id for e in evidence}
    data = {"topic": ctx.request.topic, "subtopics": (ctx.task.outcome.get("plan") or {}).get("subtopics", []),
            "sample": _sample(ctx),
            "evidence": [{"evidence_id": e.id, "paper_id": e.paper_id, "paper_title": titles.get(e.paper_id, ("",))[0],
                          "level": e.level, "kind": e.kind, "attribution": e.attribution, "statement": e.statement,
                          "quote": e.quote} for e in evidence]}
    state, result = ctx.model("synthesize", None, SYNTHESIZE, data, _h([data, SYNTHESIZE.sha256]),
                              lambda out: validate.synthesize(out, known))
    if state != "done":
        return None
    ctx.outcome(synthesis={k: v for k, v in result.items() if not k.startswith("_")})
    return "outline"


def phase_outline(ctx: Ctx) -> Optional[str]:
    evidence = _evidence(ctx)[:MAX_EVIDENCE_IN_PROMPT]
    known = {e.id for e in evidence}
    data = {"topic": ctx.request.topic, "sections": ctx.request.sections, "synthesis": ctx.task.outcome.get("synthesis"),
            "evidence_catalog": [{"evidence_id": e.id, "paper_id": e.paper_id, "kind": e.kind, "level": e.level,
                                  "statement": e.statement[:240]} for e in evidence]}
    state, result = ctx.model("outline", None, OUTLINE, data, _h([data, OUTLINE.sha256]),
                              lambda out: validate.outline(out, known, ctx.request.sections))
    if state != "done":
        return None
    if not ctx.session.scalar(select(SectionRow.id).where(SectionRow.task_id == ctx.task.id)):
        for idx, s in enumerate(result["sections"]):
            ctx.session.add(SectionRow(id=new_id("sec"), task_id=ctx.task.id, outline_version=1, idx=idx,
                                       title=s["title"], goal=s["goal"], evidence_ids=s["evidence_ids"],
                                       gaps=s["gaps"], status="pending", review_status="pending"))
        ctx.session.flush()
    ctx.outcome(outline={"title": result.get("title"), "unknown_ids": result.get("unknown_ids", [])})
    return "write"


def _sections(ctx: Ctx) -> list[SectionRow]:
    return list(ctx.session.scalars(select(SectionRow).where(SectionRow.task_id == ctx.task.id)
                                    .order_by(SectionRow.outline_version, SectionRow.idx)))


def _pack(ctx: Ctx, ids: list) -> list[dict]:
    titles = {p.id: (p.title, p.year) for p in _included(ctx)}
    return [{"evidence_id": e.id, "paper_id": e.paper_id, "paper_title": titles.get(e.paper_id, ("", None))[0],
             "year": titles.get(e.paper_id, ("", None))[1], "level": e.level, "kind": e.kind,
             "attribution": e.attribution, "statement": e.statement, "quote": e.quote}
            for e in _evidence(ctx, ids)]


GAP_NOTE = "（本节在当前样本中没有可核对的证据支持，保留为待补充内容；不据此推断整个领域的情况。）"


def phase_write(ctx: Ctx) -> Optional[str]:
    sections = _sections(ctx)
    for s in sections:
        if s.status not in ("pending", "needs_revision"):
            continue
        if not s.evidence_ids:
            s.text, s.status, s.review_status = GAP_NOTE, "gap", "not_applicable"
            continue
        pack = _pack(ctx, s.evidence_ids)
        known = {e["evidence_id"] for e in pack}
        data = {"topic": ctx.request.topic, "section": {"title": s.title, "goal": s.goal, "gaps": s.gaps},
                "evidence_pack": pack, "previous_sections": [x.title for x in sections if x.idx < s.idx],
                "review_notes": (s.review or {}).get("issues") if s.status == "needs_revision" else None,
                "previous_draft": s.text if s.status == "needs_revision" else None}
        state, result = ctx.model(f"section:{s.idx}:r{s.revisions}", s.id, SECTION, data,
                                  _h([data, SECTION.sha256]), lambda out, k=known: validate.section(out, k),
                                  soft=True)
        if state == "wait":
            return None
        if state == "failed":
            s.status, s.review_status = "failed", "not_applicable"
            continue
        s.text, s.claims, s.status = result["text"], result["claims"], "written"
        s.issues = ([{"kind": "unknown_evidence_id", "ids": result["unknown_ids"]}] if result["unknown_ids"] else []) + (
            [{"kind": "unsupported_points", "items": result["todo"]}] if result["todo"] else [])
        s.review_status = "pending"
    reviewer = ctx.rules.review_agent
    return "review" if reviewer else "abstract"


def phase_review(ctx: Ctx) -> Optional[str]:
    reviewer = ctx.rules.review_agent
    for s in _sections(ctx):
        if s.status != "written" or s.review_status != "pending":
            continue
        data = {"section": {"title": s.title, "goal": s.goal, "text": s.text}, "claims": s.claims,
                "evidence_pack": _pack(ctx, s.evidence_ids),
                "criteria": "每个事实与比较结论都有对应证据；摘要级证据被标明；不把当前样本的空白写成领域结论。"}
        state, result = ctx.model(f"review:{s.idx}:r{s.revisions}", s.id, REVIEW, data, _h([data, REVIEW.sha256]),
                                  validate.review, agent=reviewer, soft=True)
        if state == "wait":
            return None
        if state == "failed":
            s.review_status = "unavailable"  # never "passed" without a review
            continue
        s.review = {k: v for k, v in result.items() if not k.startswith("_")}
        if result["verdict"] == "pass":
            s.review_status = "passed"
        elif result["verdict"] == "revise" and s.revisions < MAX_REVISIONS:
            s.revisions += 1
            s.status, s.review_status = "needs_revision", "revising"
        else:
            s.review_status = "needs_review"
    if any(s.status == "needs_revision" for s in _sections(ctx)):
        return "write"
    return "abstract"


def phase_abstract(ctx: Ctx) -> Optional[str]:
    sections = [s for s in _sections(ctx) if s.status == "written"]
    if not sections:
        return "export"
    data = {"title": (ctx.task.outcome.get("outline") or {}).get("title"), "scope": _sample(ctx),
            "sections": [{"title": s.title, "text": (s.text or "")[:2500]} for s in sections]}
    state, result = ctx.model("abstract", None, ABSTRACT, data, _h([data, ABSTRACT.sha256]), validate.abstract,
                              soft=True)
    if state == "wait":
        return None
    ctx.outcome(abstract={k: v for k, v in result.items() if not k.startswith("_")} if state == "done" else None)
    return "export"


def phase_export(ctx: Ctx) -> Optional[str]:
    from agenthub_kernel.papers.export import export

    ok, why = resource_decision(ctx.resource, ACTION_EXPORT)
    if not ok:
        raise _Blocked("export", "plan_outside_rules", why)
    export(ctx)
    return "done"


from agenthub_kernel.papers.guidance import phase_directions

PHASE_HANDLERS = {"plan": phase_plan, "search": phase_search, "screen": phase_screen, "fetch": phase_fetch,
                  "fulltext_gate": phase_fulltext_gate, "read": phase_read, "synthesize": phase_synthesize,
                  "outline": phase_outline, "write": phase_write, "review": phase_review, "abstract": phase_abstract,
                  "export": phase_export, "related_read": phase_related_read, "directions": phase_directions}


def advance(ap: Any, session: Session, stage: StageRunRow, to_start: list) -> list:
    task = task_of_stage(session, stage)
    if task is None:
        raise _Blocked("paper", "start_refused", "no research task bound to this stage")
    rules, resource = _checked_rules(stage, task)
    if rules is None:
        raise _Blocked("paper", "plan_outside_rules", "research request or resource rules changed after authorization")
    ctx = Ctx(ap, session, stage, task, rules, resource, ResearchRequest(**task.request), to_start)
    for _ in range(40):
        handler = PHASE_HANDLERS.get(task.phase)
        if handler is None:
            break
        nxt = handler(ctx)
        if nxt is None:
            break
        task.phase = nxt
        _event(session, stage, "paper_phase", phase=nxt)
        if nxt == "done":
            task.execution_status = "completed"
            ap._end(session, stage, STAGE_COMPLETED)
            break
    session.flush()
    return to_start


# --------------------------------------------------------------------------- #
# tool steps
# --------------------------------------------------------------------------- #


def run_tools(ap: Any, stage_id: str, *, limit: int = 12) -> int:
    """Run this stage's pending tool steps, one at a time, outside the DB lock."""
    runtime = getattr(ap, "papers", None)
    if runtime is None:
        return 0
    done = 0
    for _ in range(limit):
        with ap.db.session() as session:
            session.execute(text("BEGIN IMMEDIATE"))
            stage = session.get(StageRunRow, stage_id)
            if stage is None or stage.status not in STAGE_RUNNABLE:
                return done  # paused, stopped or ended: no new resource request
            task = task_of_stage(session, stage)
            if task is None:
                return done
            rules, resource = _checked_rules(stage, task)
            if rules is None:
                return done
            row = session.scalar(select(PaperStepRow).where(
                PaperStepRow.task_id == task.id, PaperStepRow.kind == "tool",
                PaperStepRow.status.in_(("pending", "running"))).order_by(PaperStepRow.id))
            if row is None:
                return done
            if row.status == "running":
                # Claimed by an earlier process that stopped before committing a
                # result: nothing of it was recorded. Its stored output is
                # content-addressed, so the step is re-checked and re-run boundedly.
                if row.attempts >= TOOL_ATTEMPTS:
                    row.status, row.error = "failed", "interrupted repeatedly; not retried again"
                    _apply_tool_failure(session, task, row, "download_failed")
                    continue
                _event(session, stage, "paper_tool_rechecked", step=row.key[:120])
            payload = dict((row.result or {}).get("payload") or {})
            row.status, row.attempts, row.started_at = "running", row.attempts + 1, ap.clock()
            row.result = {"payload": payload, "runner": runtime.token}
            job = _prepare(session, task, row, payload, resource)
            step_id = row.id
        try:
            outcome = _execute(runtime, job, resource)
        except Exception as exc:  # noqa: BLE001 - a tool bug must not leave the step claimed forever
            outcome = {"error": "tool_error:" + type(exc).__name__}
        with ap.db.session() as session:
            session.execute(text("BEGIN IMMEDIATE"))
            row = session.get(PaperStepRow, step_id)
            if row is None or row.status != "running":
                continue
            task = session.get(ResearchTaskRow, row.task_id)
            _commit(session, runtime, task, row, job, outcome, ap.clock())
            done += 1
    return done


def _prepare(session: Session, task: ResearchTaskRow, row: PaperStepRow, payload: dict, resource: Any) -> dict:
    job = {"name": row.name, "task_id": task.id, "payload": payload}
    if row.name == "validate":
        batch = session.get(SupplementBatchRow, payload["batch_id"])
        files = session.scalars(select(SupplementFileRow).where(SupplementFileRow.batch_id == batch.id)
                                .order_by(SupplementFileRow.id)).all()
        job["files"] = [{"id": f.id, "kind": f.kind, "origin": f.origin_ref, "rel_path": f.rel_path,
                         "sha256": f.sha256} for f in files]
    return job


def _execute(runtime: PaperRuntime, job: dict, resource: Any) -> dict:
    name, payload = job["name"], job["payload"]
    fetcher = runtime.fetcher
    if name == "search":
        ok, why = resource_decision(resource, ACTION_SEARCH, "arxiv")
        if not ok:
            return {"error": why}
        runtime.polite()
        got = fetcher.fetch(payload["url"], max_bytes=5 * 1024 * 1024, accept="application/atom+xml",
                            allowed=lambda u: check_url(u, hosts=resource.hosts(), resolve=fetcher.resolve))
        if not got.ok:
            return {"error": got.error}
        try:
            return {"entries": arxiv.parse_feed(got.body)}
        except ValueError as exc:
            return {"error": "bad_feed:" + str(exc)[:80]}
    if name == "fetch":
        return _download(runtime, job["task_id"], payload["url"], resource,
                         lambda u: resource_decision(resource, ACTION_DOWNLOAD, u, resolve=fetcher.resolve),
                         origin="download")
    if name == "validate":
        return {"files": [_obtain(runtime, job["task_id"], f, resource) for f in job["files"]]}
    return {"error": "unknown_tool"}


def _parsed(runtime: PaperRuntime, task_id: str, data: bytes, *, name: str, content_type: str) -> dict:
    result = parse_bytes(data, name=name, content_type=content_type)
    suffix = ".pdf" if data[:5] == b"%PDF-" else ".txt"
    rel, sha, size = runtime.store.save(task_id, "documents", data, suffix)
    out = {"rel_path": rel, "sha256": sha, "size": size, "parse_status": result.status,
           "parse_error": result.error, "extractor": result.extractor, "pages": len(result.pages),
           "chars": len(result.text), "content_type": content_type[:80]}
    if result.status == "ok":
        out["chunks"] = [{"seq": c.seq, "page": c.page, "char_start": c.char_start, "char_end": c.char_end,
                          "text": c.text, "sha256": c.sha256} for c in chunk(result)]
        out["head"] = result.text[:5000]
    return out


def _download(runtime: PaperRuntime, task_id: str, url: str, resource: Any, allowed: Callable, *,
              origin: str) -> dict:
    got = runtime.fetcher.fetch(url, allowed=allowed, max_bytes=resource.max_download_bytes,
                                accept="application/pdf,text/plain;q=0.5")
    if not got.ok:
        reason = got.error
        mapped = ("too_large" if reason == "too_large" else "host_not_allowed" if reason in (
            "host_not_allowed", "address_not_public", "scheme_not_allowed", "port_not_allowed") else "download_failed")
        return {"error": reason, "reason": mapped, "hops": got.hops}
    if got.body[:5] != b"%PDF-" and "pdf" in (got.content_type or "").lower():
        return {"error": "not_pdf", "reason": "corrupt"}
    if got.body[:5] != b"%PDF-" and not (got.content_type or "").startswith("text/plain"):
        return {"error": "not_pdf", "reason": "not_pdf"}
    return {"document": _parsed(runtime, task_id, got.body, name=url.rsplit("/", 1)[-1],
                                content_type=got.content_type or ""), "origin": origin, "final_url": got.final_url}


def _obtain(runtime: PaperRuntime, task_id: str, f: dict, resource: Any) -> dict:
    fetcher = runtime.fetcher
    if f["kind"] == "upload":
        try:
            data = runtime.store.read(f["rel_path"], f["sha256"])
        except (OSError, ValueError):
            return {"id": f["id"], "status": FILE_FETCH_FAILED, "detail": "上传的文件已不可读"}
        return {"id": f["id"], "document": _parsed(runtime, task_id, data, name=f["origin"], content_type="")}
    if f["kind"] == "local":
        ok, why = resource_decision(resource, ACTION_READ_LOCAL, f["origin"])
        if not ok:
            return {"id": f["id"], "status": FILE_REFUSED, "detail": f"路径不在授权目录内（{why}）"}
        try:
            path = os.path.realpath(f["origin"])
            if not os.path.isfile(path):
                return {"id": f["id"], "status": FILE_FETCH_FAILED, "detail": "路径不存在或不是文件"}
            if os.path.getsize(path) > resource.max_download_bytes:
                return {"id": f["id"], "status": FILE_FETCH_FAILED, "detail": "文件超过允许大小"}
            with open(path, "rb") as fh:
                data = fh.read(resource.max_download_bytes + 1)
        except OSError as exc:
            return {"id": f["id"], "status": FILE_FETCH_FAILED, "detail": f"读取失败：{type(exc).__name__}"}
        return {"id": f["id"], "document": _parsed(runtime, task_id, data, name=f["origin"], content_type="")}
    got = _download(runtime, task_id, f["origin"], resource,
                    lambda u: resource_decision(resource, ACTION_FETCH_URL, u, resolve=fetcher.resolve), origin="url")
    if "document" not in got:
        return {"id": f["id"], "status": FILE_REFUSED if got.get("reason") == "host_not_allowed" else FILE_FETCH_FAILED,
                "detail": f"地址不可用：{got.get('error')}"}
    return {"id": f["id"], "document": got["document"]}


def _store_document(session: Session, task: ResearchTaskRow, paper_id: Optional[str], doc: dict, *,
                    origin: str, origin_ref: str) -> PaperDocumentRow:
    row = PaperDocumentRow(id=new_id("doc"), task_id=task.id, paper_id=paper_id, origin=origin,
                           origin_ref=origin_ref[:1000], rel_path=doc["rel_path"], sha256=doc["sha256"],
                           size=doc["size"], content_type=doc.get("content_type"), parse_status=doc["parse_status"],
                           parse_error=doc.get("parse_error"), extractor=doc.get("extractor"),
                           pages=doc.get("pages"), chars=doc.get("chars"))
    session.add(row)
    session.flush()
    for c in doc.get("chunks") or []:
        session.add(PaperChunkRow(id=f"chk-{row.id[4:]}-{c['seq']}", document_id=row.id, paper_id=paper_id,
                                  seq=c["seq"], page=c["page"], char_start=c["char_start"], char_end=c["char_end"],
                                  text=c["text"], sha256=c["sha256"]))
    session.flush()
    return row


PARSE_REASON = {"encrypted": "encrypted", "corrupt": "corrupt", "empty_text": "empty_text", "scanned": "scanned",
                "unsupported": "corrupt"}


def _apply_tool_failure(session: Session, task: ResearchTaskRow, row: PaperStepRow, reason: str) -> None:
    if row.name == "fetch":
        paper = session.get(PaperRow, row.target)
        if paper is not None and paper.fulltext_status == "unchecked":
            paper.fulltext_status, paper.fulltext_reason = "missing", reason


def _commit(session: Session, runtime: PaperRuntime, task: ResearchTaskRow, row: PaperStepRow, job: dict,
            outcome: dict, now: dt.datetime) -> None:
    row.finished_at = now
    summary: dict[str, Any] = {"payload": job["payload"]}
    if row.name == "search":
        if "error" in outcome:
            row.status, row.error = "failed", outcome["error"][:300]
        else:
            summary.update(_upsert_papers(session, task, outcome["entries"], job["payload"]["index"]))
            row.status = "succeeded"
    elif row.name == "fetch":
        paper = session.get(PaperRow, row.target)
        if "document" in outcome:
            doc = _store_document(session, task, paper.id, outcome["document"], origin="download",
                                  origin_ref=job["payload"]["url"])
            if doc.parse_status == "ok":
                paper.fulltext_status, paper.fulltext_reason, paper.document_id = "available", None, doc.id
            else:
                paper.fulltext_status = "missing"
                paper.fulltext_reason = PARSE_REASON.get(doc.parse_status, "corrupt")
            summary.update(document_id=doc.id, parse_status=doc.parse_status)
            row.status = "succeeded"
        else:
            # A definitive tool failure is the paper's missing reason, not a pipeline error.
            paper.fulltext_status, paper.fulltext_reason = "missing", outcome.get("reason", "download_failed")
            summary.update(error=outcome.get("error"))
            row.status, row.error = "succeeded", None
    elif row.name == "validate":
        _commit_validation(session, task, row, job, outcome, now)
        summary.update(validated=True)
        row.status = "succeeded"
    else:
        row.status, row.error = "failed", "unknown tool"
    row.result = summary


def _surname(authors: list) -> str:
    words = (authors[0] if authors else "").split()
    return words[-1].lower() if words else ""


def _upsert_papers(session: Session, task: ResearchTaskRow, entries: list, index: int) -> dict:
    """Merge on source id, then DOI, then normalized title AND first-author surname - never title alone."""
    existing = list(session.scalars(select(PaperRow).where(PaperRow.task_id == task.id)))
    by_source = {(p.source, p.source_id): p for p in existing}
    by_doi = {p.doi.lower(): p for p in existing if p.doi}
    by_title = {(normalize_title(p.title), _surname(p.authors)): p for p in existing if p.authors}
    new = merged = 0
    rank = len(existing)
    for e in entries:
        key = (e["source"], e["source_id"])
        first = _surname(e.get("authors") or [])
        hit, why = by_source.get(key), "same_source_id"
        if hit is None and e.get("doi"):
            hit, why = by_doi.get(e["doi"].lower()), "same_doi"
        if hit is None and e.get("authors"):
            hit, why = by_title.get((normalize_title(e["title"]), first)), "same_title_and_first_author"
        if hit is not None:
            if f"q{index}" not in (hit.queries or []):
                hit.queries = [*(hit.queries or []), f"q{index}"]
            if why != "same_source_id" and e["source_id"] not in {m["source_id"] for m in hit.merged or []}:
                hit.merged = [*(hit.merged or []), {"source_id": e["source_id"], "reason": why}]
                merged += 1
            continue
        paper = PaperRow(id=new_id("pap"), task_id=task.id, rank=rank, queries=[f"q{index}"], merged=[],
                         **{k: e.get(k) for k in ("source", "source_id", "source_version", "title", "authors", "year",
                                                  "published", "venue", "doi", "url", "pdf_url", "abstract",
                                                  "categories")})
        rank += 1
        session.add(paper)
        by_source[key] = paper
        if paper.doi:
            by_doi[paper.doi.lower()] = paper
        if paper.authors:
            by_title[(normalize_title(paper.title), first)] = paper
        new += 1
    session.flush()
    return {"entries": len(entries), "new": new, "merged": merged}


def _commit_validation(session: Session, task: ResearchTaskRow, row: PaperStepRow, job: dict, outcome: dict,
                       now: dt.datetime) -> None:
    batch = session.get(SupplementBatchRow, job["payload"]["batch_id"])
    req = session.get(SupplementRequestRow, batch.request_id)
    if req.status != SUP_VALIDATING:
        return
    results = {r["id"]: r for r in outcome.get("files", [])} if "files" in outcome else {}
    accepted = dict(session.execute(select(PaperDocumentRow.sha256, PaperDocumentRow.paper_id).where(
        PaperDocumentRow.task_id == task.id, PaperDocumentRow.parse_status == "ok",
        PaperDocumentRow.paper_id.is_not(None))).all())
    for f in session.scalars(select(SupplementFileRow).where(SupplementFileRow.batch_id == batch.id)
                             .order_by(SupplementFileRow.id)):
        got = results.get(f.id) or {"status": FILE_FETCH_FAILED, "detail": outcome.get("error", "核对未完成")}
        doc = got.get("document")
        if doc is None:
            f.status, f.detail = got["status"], got.get("detail")
            continue
        f.sha256, f.size, f.rel_path = doc["sha256"], doc["size"], f.rel_path or doc["rel_path"]
        if doc["parse_status"] != "ok":
            f.status = FILE_PARSE_FAILED
            f.detail = {"encrypted": "PDF 已加密", "corrupt": "文件损坏或格式不受支持", "empty_text": "解析结果为空",
                        "scanned": "疑似扫描件，首版不支持 OCR", "unsupported": "仅支持 PDF 与 UTF-8 文本"}.get(
                doc["parse_status"], doc["parse_status"])
            continue
        if doc["sha256"] in accepted:
            owner = accepted[doc["sha256"]]
            if f.target_paper_id and owner != f.target_paper_id:
                other = session.get(PaperRow, owner)
                f.status, f.detail = FILE_MISMATCH, f"内容与已有全文的另一篇论文相同：{(other.title if other else owner)[:80]}"
            else:
                f.status, f.detail = FILE_DUPLICATE, "与已收到的全文内容相同"
            continue
        left = supplement.remaining(session, req)
        status, paper_id, detail = supplement.match(doc.get("head", ""), left, f.target_paper_id)
        if status == FILE_MISMATCH and f.target_paper_id:
            done_item = session.scalar(select(MissingItemRow).where(
                MissingItemRow.request_id == req.id, MissingItemRow.paper_id == f.target_paper_id,
                MissingItemRow.state == "resolved"))
            if done_item is not None:
                status, detail = FILE_DUPLICATE, "该论文已补齐"
        f.status, f.detail = status, detail
        if status != FILE_ACCEPTED:
            continue
        paper = session.get(PaperRow, paper_id)
        document = _store_document(session, task, paper_id, doc, origin=f.kind, origin_ref=f.origin_ref)
        paper.fulltext_status, paper.fulltext_reason, paper.document_id = "supplied", None, document.id
        item = session.scalar(select(MissingItemRow).where(MissingItemRow.request_id == req.id,
                                                           MissingItemRow.paper_id == paper_id))
        item.state, item.document_id, item.batch_id = "resolved", document.id, batch.id
        f.matched_paper_id, f.document_id = paper_id, document.id
        accepted[doc["sha256"]] = paper_id
    supplement.finish_validation(session, req, batch, now)
