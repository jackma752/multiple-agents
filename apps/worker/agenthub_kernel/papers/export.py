"""Export: ``review.md``, ``references.json``, ``evidence.json``, ``research-report.json``.

Citations are built here from stored metadata; the model only ever wrote
``[ev:ID]`` markers. A marker for an unknown id stays visible as a missing
citation and lowers the quality status - it is never silently removed.
Execution finishing and quality passing are recorded separately: missing
review, failed checks or an exhausted revision limit are never ``passed``.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any, Optional

from sqlalchemy import select

from agenthub.papers import (
    ACTION_EXPORT, DEPTH_ABSTRACT, DEPTH_FULLTEXT, LANGUAGES, QUALITY_FAILED, QUALITY_NEEDS_REVIEW, QUALITY_PASSED,
    QUALITY_UNAVAILABLE, reference_text, resource_decision,
)
from agenthub_kernel.db import UsageLedgerRow
from agenthub_kernel.papers.db import (
    ArtifactRow, EvidenceRow, PaperRow, PaperStepRow, SectionRow, SupplementRequestRow,
)
from agenthub_kernel.papers.supplement import request_view
from agenthub_kernel.papers.validate import MARKER, resolve_marker
from agenthub_kernel.service import new_id

DECISIONS = {"user_continue": "用户选择不补充，按现有资料继续", "timeout_continue": "方案一首次询问 30 分钟未选择，默认按现有资料继续",
             "all_supplied": "缺失全文已全部补齐"}


def usage_of(session, invocation_ids: list) -> dict:
    totals = {"calls": len(invocation_ids), "input": 0, "output": 0, "unknown": 0}
    for inv_id in invocation_ids:
        row = session.get(UsageLedgerRow, inv_id)
        if row is None or row.input_total is None:
            totals["unknown"] += 1
        else:
            totals["input"] += row.input_total
            totals["output"] += row.output_total or 0
    return totals


def compute_quality(sections: list, *, insufficient: bool, unknown_ids: bool, reviewer: bool) -> tuple[str, list]:
    reasons = []
    if insufficient:
        reasons.append("资料不足，未生成有依据的综述正文")
        return QUALITY_FAILED, reasons
    written = [s for s in sections if s.status == "written"]
    if not written:
        return QUALITY_FAILED, ["没有成功写出的章节"]
    if any(s.status == "failed" for s in sections):
        reasons.append("有章节写作失败")
    if unknown_ids:
        reasons.append("正文含无法对应证据的引用标记")
    if reasons:
        return QUALITY_FAILED, reasons
    if not reviewer:
        return QUALITY_UNAVAILABLE, ["未配置审阅 Agent，正文未经审阅"]
    if any(s.review_status == "unavailable" for s in written):
        return QUALITY_UNAVAILABLE, ["部分章节审阅不可用"]
    if any(s.review_status != "passed" for s in written):
        return QUALITY_NEEDS_REVIEW, ["修订达到上限后仍有审阅问题，或审阅认为证据不足"]
    gaps = [s for s in sections if s.status == "gap"]
    if gaps:
        return QUALITY_NEEDS_REVIEW, [f"{len(gaps)} 个章节在当前样本中缺少证据"]
    return QUALITY_PASSED, []


def export(ctx: Any) -> dict:
    session, task, req = ctx.session, ctx.task, ctx.request
    store = ctx.ap.papers.store
    outcome = task.outcome or {}
    papers = list(session.scalars(select(PaperRow).where(PaperRow.task_id == task.id).order_by(PaperRow.rank)))
    included = [p for p in papers if p.included]
    evidence = {e.id: e for e in session.scalars(select(EvidenceRow).where(EvidenceRow.task_id == task.id))}
    sections = list(session.scalars(select(SectionRow).where(SectionRow.task_id == task.id).order_by(SectionRow.idx)))
    supplement = session.scalar(select(SupplementRequestRow).where(SupplementRequestRow.task_id == task.id,
                                                                   SupplementRequestRow.input_version == task.input_version))
    steps = list(session.scalars(select(PaperStepRow).where(PaperStepRow.task_id == task.id).order_by(PaperStepRow.id)))

    # Number papers by first citation in the text; references from metadata only.
    numbers: dict[str, int] = {}
    unknown: set = set()
    used_in: dict[str, list] = {}

    known = set(evidence)

    def cite(match: re.Match, section_id: str) -> str:
        resolved = resolve_marker(match.group(1), known)
        ev = evidence.get(resolved) if resolved else None
        if ev is None:
            unknown.add(match.group(1))
            return f"[引用缺失:{match.group(1)}]"
        numbers.setdefault(ev.paper_id, len(numbers) + 1)
        used_in.setdefault(ev.id, []).append(section_id)
        mark = f"[{numbers[ev.paper_id]}]"
        return mark + ("ᵃ" if ev.level == DEPTH_ABSTRACT else "")

    rendered = []
    for s in sections:
        # The writing-time check is redone with the same resolver, so a warning never goes stale.
        still_unknown = sorted({m for m in MARKER.findall(s.text or "") if resolve_marker(m, known) is None})
        s.issues = [i for i in (s.issues or []) if i.get("kind") != "unknown_evidence_id"] + (
            [{"kind": "unknown_evidence_id", "ids": still_unknown}] if still_unknown else [])
        body = MARKER.sub(lambda m, sid=s.id: cite(m, sid), s.text or "") if s.text else ""
        body = re.sub(r"(\[\d+\]ᵃ?)(?:\s*\1)+", r"\1", body)  # same citation repeated back to back
        rendered.append((s, body))

    insufficient = outcome.get("insufficient")
    quality, quality_reasons = compute_quality(sections, insufficient=bool(insufficient), unknown_ids=bool(unknown),
                                               reviewer=bool(ctx.rules.review_agent))
    sample = {"included": len(included), "fulltext": sum(p.read_depth == DEPTH_FULLTEXT for p in included),
              "abstract_only": sum(p.read_depth == DEPTH_ABSTRACT for p in included),
              "metadata_only": sum(p.read_depth not in (DEPTH_FULLTEXT, DEPTH_ABSTRACT) for p in included),
              "candidates": len(papers)}
    paper_by_id = {p.id: p for p in papers}
    references = [{"number": n, "paper_id": pid, "reference": reference_text(n, _meta(paper_by_id[pid])),
                   **_meta(paper_by_id[pid]), "read_depth": paper_by_id[pid].read_depth}
                  for pid, n in sorted(numbers.items(), key=lambda kv: kv[1])]

    lang = LANGUAGES.get(req.language, req.language)
    title = (outcome.get("outline") or {}).get("title") or f"{req.topic}：文献综述（研究简报）"
    md = [f"# {title}", ""]
    banner = f"> 执行状态：已完成；质量状态：{quality}"
    if quality != QUALITY_PASSED:
        banner += "（草稿：" + "；".join(quality_reasons) + "）"
    md += [banner, f"> 性质：基于 {sample['included']} 篇 arXiv 论文的可追溯叙述性综述/研究简报，"
           "不是系统综述、荟萃分析或原创实验报告。", f"> 输出语言：{lang}", ""]
    if insufficient:
        md += ["## 资料不足说明", "", f"本次未能形成有依据的综述正文：{insufficient.get('detail') or insufficient['reason']}。",
               "下面列出已获得的资料与检索条件，供调整主题、年份或补充资料后重新开始。", ""]
    abstract = outcome.get("abstract")
    if abstract and not insufficient:
        md += ["## 摘要", "", abstract["abstract"], "", "关键词：" + "；".join(abstract.get("keywords") or []), ""]
    for s, body in rendered:
        md += [f"## {s.idx + 1}. {s.title}", "", body or "（本节未生成）", ""]
        if s.review_status not in ("passed", "not_applicable"):
            md += [f"> 本节审阅状态：{s.review_status}", ""]
    md += ["## 研究范围与局限", "",
           f"- 检索来源：arXiv；年份：{req.year_from or '不限'}–{req.year_to or '不限'}；候选 {sample['candidates']} 篇，"
           f"纳入 {sample['included']} 篇（目标 {req.target_papers} 篇）。",
           f"- 阅读深度：全文 {sample['fulltext']} 篇，仅摘要 {sample['abstract_only']} 篇，仅题录 {sample['metadata_only']} 篇。"
           "带 ᵃ 的引用只有摘要级证据。",
           "- 全文阅读只覆盖按关键词选出的片段，页码为 PDF 页序号，不是印刷页码。",
           "- 文中“当前样本未发现”仅指本次纳入的论文，不代表整个领域。"]
    if supplement is not None:
        md.append(f"- 全文缺失处理：{DECISIONS.get(supplement.decision, supplement.decision or '未结束')}。")
    for gap in (outcome.get("synthesis") or {}).get("gaps", []):
        md.append(f"- 当前样本中的空白：{gap['text']}")
    md.append("")
    md += ["## 参考文献", ""] + [r["reference"] for r in references] + [""]
    if not references:
        md += ["（正文没有引用任何论文）", ""]

    report = {
        "contract": "agenthub.papers/1", "task_id": task.id, "version": task.input_version,
        "request": task.request, "request_hash": task.request_hash, "resource_rules_hash": task.resource_rules_hash,
        "roles": task.roles, "execution_status": "completed", "quality_status": quality,
        "quality_reasons": quality_reasons, "insufficient": insufficient,
        "plan": outcome.get("plan"), "screening": outcome.get("screening"), "sample": sample,
        "papers": [{"paper_id": p.id, "title": p.title, "source_id": p.source_id, "screen": p.screen,
                    "screen_reason": p.screen_reason, "included": p.included, "fulltext_status": p.fulltext_status,
                    "fulltext_reason": p.fulltext_reason, "read_depth": p.read_depth, "read_status": p.read_status,
                    "merged": p.merged} for p in papers],
        "supplement": request_view(session, supplement, ctx.now) if supplement is not None else None,
        "synthesis": outcome.get("synthesis"),
        "sections": [{"section_id": s.id, "title": s.title, "status": s.status, "review_status": s.review_status,
                      "revisions": s.revisions, "evidence_ids": s.evidence_ids, "issues": s.issues,
                      "review": s.review} for s in sections],
        "citation_check": {"unknown_ids": sorted(unknown), "cited_papers": len(numbers),
                           "evidence_quotes_verified": all(e.verified for e in evidence.values())},
        "steps": [{"key": st.key, "kind": st.kind, "name": st.name, "status": st.status, "attempts": st.attempts,
                   "invocation_ids": st.invocation_ids, "template": st.template, "error": st.error} for st in steps],
        "usage": usage_of(session, [i for st in steps for i in (st.invocation_ids or [])]),
        "models": {"roles": task.roles},
        "limitations": ["首版仅 arXiv 来源", "关键词片段选择，不是语义检索", "不支持 OCR、公式与表格精读",
                        "质量状态来自确定性检查与模型审阅，不能替代人工核对"],
    }
    evidence_out = [{"evidence_id": e.id, "paper_id": e.paper_id, "level": e.level, "kind": e.kind,
                     "attribution": e.attribution, "statement": e.statement, "quote": e.quote, "location": e.location,
                     "document_id": e.document_id, "chunk_id": e.chunk_id, "verified": e.verified,
                     "invocation_id": e.invocation_id, "used_in_sections": used_in.get(e.id, [])}
                    for e in evidence.values()]
    files = {
        "review.md": "\n".join(md).encode("utf-8"),
        "references.json": _json(references),
        "evidence.json": _json(evidence_out),
        "research-report.json": _json(report),
    }
    version = task.input_version
    for name, data in files.items():
        rel, sha, size = store.write_export(task.id, version, name, data)
        row = session.scalar(select(ArtifactRow).where(ArtifactRow.task_id == task.id, ArtifactRow.version == version,
                                                       ArtifactRow.name == name))
        if row is None:
            row = ArtifactRow(id=new_id("art"), task_id=task.id, version=version, name=name)
            session.add(row)
        row.rel_path, row.sha256, row.size, row.final = rel, sha, size, quality == QUALITY_PASSED
    task.quality_status = quality
    task.outcome = {**outcome, "quality_reasons": quality_reasons, "sample": sample,
                    "missing_citations": sorted(unknown), "export_copy": _copy_out(ctx, files, version)}
    session.flush()
    return report


def _copy_out(ctx: Any, files: dict, version: int) -> Optional[dict]:
    """A copy in the folder the user approved, ``<export_dir>/<task>/v<n>/``; each path re-checked.

    The Hub's own copy (downloadable in the page) is already written; a failure
    here is reported, never turned into a failed or a silently partial export.
    """
    directory = ctx.resource.export_dir
    if not directory:
        return None
    target = os.path.join(directory, ctx.task.id, f"v{version}")
    written = []
    try:
        for name, data in files.items():
            path = os.path.join(target, name)
            ok, why = resource_decision(ctx.resource, ACTION_EXPORT, path)
            if not ok:
                return {"directory": target, "ok": False, "error": why, "written": written}
            os.makedirs(target, exist_ok=True)
            tmp = path + ".part"
            with open(tmp, "wb") as fh:
                fh.write(data)
            os.replace(tmp, path)
            written.append(name)
    except OSError as exc:
        return {"directory": target, "ok": False, "error": type(exc).__name__, "written": written}
    return {"directory": target, "ok": True, "written": written}


def _meta(p: PaperRow) -> dict:
    return {"title": p.title, "authors": list(p.authors or []), "year": p.year, "doi": p.doi, "url": p.url,
            "venue": p.venue, "arxiv_id": p.source_id if p.source == "arxiv" else None}


def _json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, indent=2, default=str).encode("utf-8")
