"""Teaching/research path. All model calls reuse Ctx.model and its stage gate.

The structured units are also the patch/review boundary: a missing proof loads
new source chunks, never asks the writer to embellish the same quotations.
This module does not execute model-generated mathematics or code.
"""
from __future__ import annotations

import re
from sqlalchemy import select

from agenthub_kernel.papers.db import PaperChunkRow, PaperDocumentRow, PaperRow, PaperStepRow
from agenthub_kernel.papers.parse import quote_in
from agenthub_kernel.papers.templates import Template, _register
from agenthub_kernel.papers.validate import FormatError

KINDS = ("problem", "intuition", "prerequisites", "symbols", "setting", "assumption", "result", "step", "example", "next")
UNIT_SCHEMA = ('{"id":"stable unique id","kind":"problem|intuition|prerequisites|symbols|setting|assumption|result|step|example|next",'
               '"title":"...","text":"explanation","latex":"optional formula without delimiters",'
               '"reason":"why this step follows","depends_on":["earlier unit ids"],'
               '"definitions":[{"symbol":"x_n","meaning":"...","domain":"..."}],'
               '"origin":"source|reconstruction","sources":[{"chunk_id":"given id","quote":"exact source span"}]}')
MISSING_SCHEMA = '{"reason":"specific missing proof/definition or damaged formula","pages":[1],"terms":["Theorem 2"]}'
STUDY = _register(Template(
    "research.study.v1", 1, "read", ("paper", "chunks", "coverage"),
    '{"units":[' + UNIT_SCHEMA + '],"math_state":"complete|incomplete","missing":[' + MISSING_SCHEMA + ']}',
    "Original claims and equations require source spans from given chunks. Problem, setting, assumptions and result "
    "must each link to source spans even when paraphrased. Reconstruction is your teaching derivation, "
    "not an author's quotation: label it reconstruction, state assumptions and dependencies. Never guess broken PDF formulas.",
    "If a proof crosses the selected pages, request its pages/terms in missing and mark incomplete. A short excerpt is "
    "not the whole proof. Do not claim an entire paper or field was verified.",
    "Teach a first-time researcher this paper: problem, intuition, prerequisites, symbol definitions, mathematical setting, "
    "assumptions and why needed, main result, at least two explicit derivation steps with reasons and dependencies, a worked "
    "example with calculation, and next learning task. For theory explain axioms/operators/theorems; for empirical work "
    "explain objectives/constraints/updates/validation. Prefer one complete core argument over many shallow summaries. "
    "Every kind in the schema must have a unit. Use LaTeX. Reconstruct omitted elementary steps explicitly; do not invent proof.",
))
CHECK = _register(Template(
    "research.study-review.v1", 1, "review", ("units", "chunks", "previous_issues"),
    '{"issues":[{"id":"stable issue id","unit_id":"given id","severity":"critical|major|minor",'
    '"kind":"material|math|citation|style","problem":"specific fault","closure":"test for closing it",'
    '"pages":[1],"terms":["proof"]}]}',
    "Check the actual chain: definitions, assumptions, each inequality, limiting steps, example calculations, citations and "
    "whether a novice could follow. Quote consistency alone does not establish mathematics. Minor style must not block.",
    "Missing source/proof is a material issue with page/anchor requests, not a request to rewrite prose. "
    "On recheck inspect patched units and their dependencies only, reuse stable issue ids.",
    "Audit these units. Return every unresolved substantive issue with a concrete closure criterion; no issues means "
    "only that this model check found none, not a formal proof certificate. Keep previous issue ids for persistent faults.",
))
PATCH = _register(Template(
    "research.study-patch.v1", 1, "read", ("issues", "units", "chunks"),
    '{"replacements":[' + UNIT_SCHEMA + '],"additions":[' + UNIT_SCHEMA + '],"math_state":"complete|incomplete","missing":[' + MISSING_SCHEMA + ']}',
    "Only replace units named in editable_ids. Preserve ids, kinds, ordering and dependencies on unchanged units. "
    "If allow_additions is true, append new units needed to complete previously missing mathematics, with new ids. "
    "Use the NEW source material to close material issues, not stronger wording from the same old quotes.",
    "If new material is insufficient retain an explicit missing item and mark incomplete. Do not say an issue was "
    "resolved merely because it was rephrased.",
    "Repair only the flagged units, with explicit corrected equations/reasons and source spans. Return replacements only. "
    "Leave all other content untouched. The program retains it and separately rechecks your changes.",
))
IDEAS = _register(Template(
    "research.directions.v1", 1, "synthesize", ("studies", "related"),
    '{"comparison":"compare assumptions and key proof/algorithm steps", "candidates":[{"id":"A",'
    '"question":"...","baseline":"...","change":"one precise change", "rationale":"why plausible",'
    '"method":"LaTeX equations / proof route / pseudocode", "validation":"minimal example, counterexample or experiment",'
    '"success":"falsifiable criterion","failure_plan":"...","prerequisites":"...","first_week":"concrete tasks",'
    '"source_papers":["given paper id"],"novelty":"unverified"}]}',
    "Separate established source results, teaching reconstruction and NEW candidate ideas. Proposals may reason beyond "
    "the papers but must be labelled unverified. An omission in this sample is never proof of novelty. No invented results.",
    "When source mathematics is incomplete, explain what must be resolved before the proposal can be trusted. "
    "Do not recommend merely changing a space or constant as a contribution without testing equivalence.",
    "Give 2-3 actionable research candidates for a beginner. For theory specify a conjecture, required lemmas, key estimate "
    "and counterexample search; for algorithms specify update/termination/cost/convergence questions, baselines and ablations. "
    "Include cost/prerequisites, success and failure tests and first-week tasks. No claim of publication readiness.",
))


def _text(obj, key, required=True):
    if not isinstance(obj,dict):raise FormatError("structured object required")
    value = obj.get(key, "")
    if not isinstance(value, str) or len(value) > 24000 or required and not value.strip():
        raise FormatError(f"{key}: nonempty text required")
    return value


def missing_items(out):
    items = out.get("missing", [])
    if not isinstance(items, list) or len(items) > 12:
        raise FormatError("missing must be a bounded list")
    for item in items:
        _text(item, "reason")
        selectors(item)
    if out.get("math_state") not in ("complete", "incomplete"):
        raise FormatError("math_state required")
    if out["math_state"] == "incomplete" and not items:
        raise FormatError("incomplete needs a specific missing reason")
    return items


def selectors(item):
    pages, terms = item.get("pages", []), item.get("terms", [])
    if not isinstance(pages, list) or len(pages) > 20 or any(type(p) is not int or p < 1 for p in pages):
        raise FormatError("pages must be positive PDF page indices")
    if not isinstance(terms, list) or len(terms) > 12 or any(not isinstance(t, str) or not t.strip() or len(t)>200 for t in terms):
        raise FormatError("terms must be short anchors")


def study_result(out, chunks):
    units = out.get("units")
    if not isinstance(units, list) or not 1 <= len(units) <= 60:
        raise FormatError("study needs all teaching units and at least two derivation steps")
    seen, kinds = set(), []
    source = {c.id: c for c in chunks}
    for unit in units:
        uid = _text(unit, "id")
        if uid in seen or len(uid)>80 or not re.fullmatch(r"[A-Za-z0-9_-]+", uid):
            raise FormatError("invalid or repeated unit id")
        deps = unit.get("depends_on", [])
        if not isinstance(deps, list) or any(not isinstance(d,str) or d not in seen for d in deps):
            raise FormatError("dependency must point to an earlier unit")
        kind = unit.get("kind")
        if kind not in KINDS:
            raise FormatError("unknown teaching unit kind")
        for key in ("text", "title"):
            _text(unit, key)
        _text(unit, "latex", False)
        if kind=="symbols":
            definitions=unit.get("definitions")
            if not isinstance(definitions,list) or not 1<=len(definitions)<=80:
                raise FormatError("symbols need explicit symbol/meaning/domain definitions")
            for definition in definitions:
                for key in ("symbol","meaning","domain"):_text(definition,key)
        if kind=="assumption":_text(unit,"reason")
        if kind in ("step", "example"):
            _text(unit, "reason")
            if not deps:
                raise FormatError("steps/examples must name their assumptions or previous steps")
        if kind=="step":_text(unit,"latex")
        if unit.get("origin") not in ("source", "reconstruction"):
            raise FormatError("origin must distinguish source from teaching reconstruction")
        refs = unit.get("sources", [])
        if not isinstance(refs,list) or len(refs)>12 or (unit["origin"]=="source" or kind in ("problem","setting","assumption","result")) and not refs:
            raise FormatError("source units require locations")
        for ref in refs:
            if not isinstance(ref,dict):raise FormatError("source location must be an object")
            c = source.get(ref.get("chunk_id"))
            if c is None or not isinstance(ref.get("quote"),str) or not quote_in(ref["quote"], c.text):
                raise FormatError("source span not found in the supplied chunk")
            ref["pdf_page"]=c.page
        kinds.append(kind); seen.add(uid)
    if out.get("math_state")=="complete" and (set(KINDS)-set(kinds) or kinds.count("step")<2):
        raise FormatError("missing teaching components")
    missing=missing_items(out)
    return {"units":units,"math_state":out["math_state"],"missing":missing}


def review_result(out, ids):
    issues=out.get("issues")
    if not isinstance(issues,list) or len(issues)>30:
        raise FormatError("issues must be a bounded list")
    seen=set()
    for issue in issues:
        iid=_text(issue,"id")
        if iid in seen or issue.get("unit_id") not in ids:
            raise FormatError("invalid issue target/id")
        seen.add(iid)
        if issue.get("severity") not in ("critical","major","minor") or issue.get("kind") not in ("material","math","citation","style"):
            raise FormatError("invalid issue classification")
        _text(issue,"problem");_text(issue,"closure");selectors(issue)
    return {"issues":issues}


def patch_result(out, study, chunks, editable, allow_additions=False):
    replacements=out.get("replacements")
    additions=out.get("additions",[])
    if not isinstance(additions,list) or len(additions)>60 or additions and not allow_additions:
        raise FormatError("new units not allowed without a material gap")
    if not isinstance(replacements,list) or not replacements and not additions:
        raise FormatError("replacements required")
    if any(not isinstance(u,dict) for u in replacements+additions):
        raise FormatError("patch units must be objects")
    updates={u.get("id"):u for u in replacements}
    old={u["id"]:u for u in study["units"]}
    if len(updates)!=len(replacements) or set(updates)-set(editable):
        raise FormatError("patch changed an unapproved unit")
    if any(u.get("kind")!=old[i]["kind"] for i,u in updates.items()):
        raise FormatError("patch changed a unit kind")
    candidate={"units":[updates.get(u["id"],u) for u in study["units"]]+additions,
               "math_state":out.get("math_state"),"missing":out.get("missing",[])}
    return study_result(candidate,chunks)


def ideas_result(out, papers):
    _text(out,"comparison")
    candidates=out.get("candidates")
    if not isinstance(candidates,list) or not 2<=len(candidates)<=3:
        raise FormatError("provide 2-3 candidates")
    seen=set()
    for c in candidates:
        cid=_text(c,"id")
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,40}",cid) or cid in seen:
            raise FormatError("invalid candidate id")
        seen.add(cid)
        for key in ("question","baseline","change","rationale","method","validation","success","failure_plan","prerequisites","first_week"):
            _text(c,key)
        if c.get("novelty")!="unverified":
            raise FormatError("candidate novelty must remain unverified")
        refs=c.get("source_papers")
        if not isinstance(refs,list) or not refs or any(p not in papers for p in refs):
            raise FormatError("candidate source paper not in sample")
    return {"comparison":out["comparison"],"candidates":candidates}


STRUCTURE=re.compile(r"\b(definition|theorem|lemma|proposition|proof|method|algorithm|appendix|assumption)\b|定义|定理|引理|证明|算法|附录",re.I)


def pick(chunks, requests=(), *, exclude=(), budget=24000):
    """Whole chunks, adjacent proof pages; deterministic, no model-granted file access."""
    wanted=set()
    for req in requests:
        pages=req.get("pages",[]); terms=req.get("terms",[])
        for i,c in enumerate(chunks):
            if c.page in pages or any(t.lower() in c.text.lower() for t in terms):
                wanted.update(range(max(0,i-1),min(len(chunks),i+3)))
    if not requests:
        for i,c in enumerate(chunks):
            if STRUCTURE.search(c.text):
                wanted.update(range(max(0,i-1),min(len(chunks),i+3)))
    order=sorted(wanted) if requests else sorted(wanted)+[i for i in range(len(chunks)) if i not in wanted]
    selected=[];size=0
    for i in order:
        c=chunks[i]
        if c.id in exclude or size+len(c.text)>budget:continue
        selected.append(c);size+=len(c.text)
    return sorted(selected,key=lambda c:c.seq)


def payload(chunks):
    return [{"chunk_id":c.id,"pdf_page":c.page,"text":c.text} for c in chunks]


def phase_read(ctx):
    from agenthub_kernel.papers.pipeline import _included, _h
    papers=_included(ctx)
    # First included papers in the persisted screening rank; never silently replace an unavailable core.
    core=papers[:ctx.request.core_papers]
    studies=dict((ctx.task.outcome or {}).get("studies") or {})
    for paper in core:
        previous=studies.get(paper.id,{})
        if previous.get("finished"):continue
        doc=ctx.session.get(PaperDocumentRow,paper.document_id) if paper.document_id else None
        if doc is None or doc.parse_status!="ok":
            studies[paper.id]={"paper_id":paper.id,"title":paper.title,"finished":True,"depth":"material_missing",
                               "units":[],"issues":[],"missing":[{"reason":"核心论文缺少可解析全文，不能重建证明"}],"review_status":"unavailable"}
            ctx.outcome(studies=studies);continue
        chunks=list(ctx.session.scalars(select(PaperChunkRow).where(PaperChunkRow.document_id==doc.id).order_by(PaperChunkRow.seq)))
        selected=pick(chunks)
        state,result=ctx.model(f"study:{paper.id}",paper.id,STUDY,
            {"topic":ctx.request.topic,"paper":{"paper_id":paper.id,"title":paper.title},"chunks":payload(selected),
             "coverage":{"selected":len(selected),"total":len(chunks),"selection":"definitions/proofs and adjacent chunks; bounded"}},
            _h([doc.sha256,[c.id for c in selected],STUDY.sha256]),lambda out:study_result(out,selected),soft=True)
        if state=="wait":return None
        if state=="failed":
            studies[paper.id]={"paper_id":paper.id,"title":paper.title,"finished":True,"depth":"partial","units":[],
                               "missing":[{"reason":"方法结构校验或模型调用失败"}],"issues":[],"review_status":"unavailable"}
            ctx.outcome(studies=studies);continue
        study={k:v for k,v in result.items() if not k.startswith('_')}
        seen=list(selected); issues=[];status="unavailable"
        for round_no in range(ctx.request.repair_rounds+1):
            if ctx.rules.review_agent:
                changed={u["id"] for u in study["units"]} if round_no==0 else editable | added_ids
                # Include dependencies, but not unrelated already-checked units.
                relevant=set(changed)
                for u in reversed(study["units"]):
                    if u["id"] in relevant:relevant.update(u.get("depends_on",[]))
                ref_ids={r["chunk_id"] for u in study["units"] if u["id"] in relevant for r in u.get("sources",[])}
                check_chunks=seen if round_no==0 else [c for c in seen if c.id in ref_ids or c in fresh]
                data={"paper_id":paper.id,"units":[u for u in study["units"] if u["id"] in relevant],
                      "changed_ids":sorted(changed),"chunks":payload(check_chunks),"previous_issues":issues}
                state,review=ctx.model(f"study_review:{paper.id}:r{round_no}",paper.id,CHECK,data,
                    _h([data,CHECK.sha256]),lambda out:review_result(out,relevant),agent=ctx.rules.review_agent,soft=True)
                if state=="wait":return None
                if state=="failed":status="unavailable";break
                new_issues=review["issues"]
                # Keep issues on unchanged units; a recheck cannot erase them by omission.
                issues=[i for i in issues if i["unit_id"] not in changed]+new_issues
                status="needs_review" if any(i["severity"]!="minor" and i["kind"]!="style" for i in issues) else "model_checked"
            substantive=[i for i in issues if i["severity"]!="minor" and i["kind"]!="style"]
            requests=study["missing"]+[i for i in substantive if i["kind"]=="material"]
            if not requests and not substantive:break
            if round_no>=ctx.request.repair_rounds:break
            fresh=pick(chunks,requests,exclude={c.id for c in seen}) if requests else []
            # No new source available: don't pay for another paraphrase of a missing proof.
            if requests and not fresh:break
            editable={i["unit_id"] for i in substantive}
            if requests and not editable:
                editable={u["id"] for u in study["units"] if u["kind"] in ("setting","assumption","result","step","example")}
            repair_ids=set(editable)
            for u in reversed(study["units"]):
                if u["id"] in repair_ids:repair_ids.update(u.get("depends_on",[]))
            data={"topic":ctx.request.topic,"issues":substantive,"missing":study["missing"],
                  "editable_ids":sorted(editable),"units":[u for u in study["units"] if u["id"] in repair_ids],
                  "allow_additions":bool(requests),
                  "chunks":payload(fresh)}
            all_seen=seen+fresh
            state,patched=ctx.model(f"study_patch:{paper.id}:r{round_no}",paper.id,PATCH,data,
                _h([data,PATCH.sha256]),lambda out:patch_result(out,study,all_seen,editable,bool(requests)),soft=True)
            if state=="wait":return None
            if state=="failed":status="needs_review";break
            added_ids={u['id'] for u in patched['units']}-{u['id'] for u in study['units']}
            study={k:v for k,v in patched.items() if not k.startswith('_')};seen=all_seen
        depth="method_read" if study["math_state"]=="complete" and not study["missing"] else "partial"
        if depth=="method_read" and status=="model_checked":depth="model_checked"
        studies[paper.id]={**study,"paper_id":paper.id,"title":paper.title,"finished":True,"depth":depth,
            "review_status":status,"issues":issues,"coverage":{"chunk_ids":[c.id for c in seen],
                "pdf_pages":sorted({c.page for c in seen if c.page}),"selected":len(seen),"total":len(chunks),
                "source_hash":doc.sha256}}
        paper.read_depth="fulltext";paper.read_status="done"
        ctx.outcome(studies=studies)
    return "related_read"


def phase_directions(ctx):
    from agenthub_kernel.papers.pipeline import _h, _evidence
    if ctx.request.mode=="study":return "export"
    studies=(ctx.task.outcome or {}).get("studies",{})
    valid=[s for s in studies.values() if s.get("units")]
    if not valid:
        ctx.outcome(directions={"comparison":"缺少可用的核心论文讲解，先补材料。","candidates":[]});return "export"
    # Send the structured mathematics, not the full PDFs again.
    related=[{"paper_id":e.paper_id,"statement":e.statement,"level":e.level} for e in _evidence(ctx)][:60]
    data={"topic":ctx.request.topic,"studies":valid,"related":related}
    ids={s["paper_id"] for s in valid}|{e["paper_id"] for e in related}
    state,result=ctx.model("directions",None,IDEAS,data,_h([data,IDEAS.sha256]),lambda out:ideas_result(out,ids),soft=True)
    if state=="wait":return None
    ctx.outcome(directions={k:v for k,v in (result or {"comparison":"方案生成失败，已有讲解保留。","candidates":[]}).items() if not k.startswith('_')})
    return "export"
