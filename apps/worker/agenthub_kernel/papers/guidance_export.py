"""Readable teaching report; provenance is adjacent to mathematics and in an appendix."""
from sqlalchemy import select

from agenthub.papers import reference_text
from agenthub_kernel.papers.db import ArtifactRow, PaperRow, PaperStepRow, ProjectRevisionRow
from agenthub_kernel.papers.math_markdown import display, prose, symbol
from agenthub_kernel.service import new_id

DEPTH = {"material_missing":"缺少核心全文", "partial":"片段已读，推导有缺口", "method_read":"方法已整理，推导未核查",
         "model_checked":"推导经模型审阅（非形式化证明）"}


def quality(studies, reviewer):
    source = bool(studies) and all(s.get("units") for s in studies.values())
    math = source and all(s.get("depth")=="model_checked" for s in studies.values())
    dimensions={"sources":"located" if source else "missing", "mathematics":"model_checked" if math else "needs_review",
                "research_plan":"unverified", "language":"not_separately_reviewed"}
    state="passed" if math else "needs_review" if reviewer else "unavailable"
    return state, dimensions


def unit_markdown(unit):
    """Presentation only: raw units and quoted evidence remain unchanged."""
    md=[prose(unit["text"],preserve_quotes=False), ""]
    for definition in unit.get("definitions", []):
        md += [f"- {symbol(definition['symbol'])}：{prose(definition['meaning'],preserve_quotes=False)}；范围：{prose(definition['domain'],preserve_quotes=False)}"]
    if unit.get("definitions"):md += [""]
    if unit.get("latex"):md += [display(unit["latex"]), ""]
    if unit.get("reason"):md += ["这一步的理由："+prose(unit["reason"],preserve_quotes=False), ""]
    if unit.get("depends_on"):md += ["依赖："+", ".join(unit["depends_on"]), ""]
    return "\n".join(md)


def display_studies(studies):
    """Add display Markdown to response copies; never alter saved study data."""
    return {key:{**study,"units":[{**unit,"display_markdown":unit_markdown(unit)}
                                 for unit in study.get("units",[])]}
            for key,study in studies.items()}


def render_studies(studies):
    md=["# 论文理解与数学推导", "", "> 来源核对与模型审阅不等于数学证明已获形式化验证。", ""]
    for s in studies.values():
        md += [f"## {s['title']}", "", f"阅读状态：{DEPTH[s['depth']]}", ""]
        for u in s.get("units",[]):
            md += [f"### {u['title']}（{u['id']}）", "", unit_markdown(u)]
            md += ["来源：原文内容" if u["origin"]=="source" else "来源：本报告补充讲解/推导，仍需核对", ""]
        if s.get("missing"):
            md += ["### 继续理解前需补齐", ""]+["- "+prose(m["reason"],preserve_quotes=False) for m in s["missing"]]+[""]
        if s.get("issues"):
            md += ["### 审阅的具体问题与关闭条件", ""]
            md += [f"- {i['id']} · {i['unit_id']} · {i['severity']}：{prose(i['problem'],preserve_quotes=False)}；关闭条件：{prose(i['closure'],preserve_quotes=False)}" for i in s["issues"]]+[""]
        md += ["<details><summary>查看来源位置与阅读覆盖</summary>", ""]
        for u in s.get("units",[]):
            for ref in u.get("sources",[]):
                md += [f"- {u['id']} · PDF 页 {ref.get('pdf_page','未知')} · {ref['chunk_id']}：{ref['quote']}"]
        cov=s.get("coverage",{})
        md += [f"- PDF 页序：{cov.get('pdf_pages',[])}；已读分块 {cov.get('selected',0)}/{cov.get('total',0)}", "", "</details>", ""]
    return "\n".join(md)


def render_directions(directions):
    md=["# 方法比较与可尝试的研究方案", "", prose(directions.get("comparison","尚未形成方案"),preserve_quotes=False), "",
        "> 以下均为待查重、待证明或待实验的候选，不能直接写成论文成果。", ""]
    labels={"question":"研究问题","baseline":"已有基线","change":"要改变的一步","rationale":"为什么值得尝试",
            "method":"公式、证明路线或算法","validation":"最小验证","success":"成功标准","failure_plan":"失败后怎么办",
            "prerequisites":"知识与工具准备","first_week":"第一周任务"}
    for c in directions.get("candidates",[]):
        md += [f"## 方案 {c['id']}：{c['question']}", ""]
        for key,label in labels.items():md += ["### "+label, "", prose(c[key],preserve_quotes=False), ""]
        md += ["依据论文："+", ".join(c["source_papers"]), ""]
    return "\n".join(md)


PROJECT_FIELDS=("contribution","proof_notes","implementation","experiments","results","draft","venue","revision")
PROJECT_LABELS=("研究问题与贡献候选","证明与方法笔记","实现计划","实验/反例与验证计划","实际结果与图表记录","论文草稿","投稿准备","审稿意见与返修")


def project_of(session, task):
    row=session.scalar(select(ProjectRevisionRow).where(ProjectRevisionRow.task_id==task.id).order_by(ProjectRevisionRow.version.desc()))
    return {"version":row.version,"selected_candidate":row.selected_candidate,"notes":row.notes} if row else {"version":0,"selected_candidate":"","notes":{}}


def project_markdown(project, directions):
    chosen=next((c for c in directions.get("candidates",[]) if c["id"]==project["selected_candidate"]),None)
    md=["# 我的论文研究项目", "", f"项目记录版本：{project['version']}", "",
        "选定候选："+(chosen["question"] if chosen else "尚未选择，请先比较研究方案"), "",
        "理论文章：定义 → 引理 → 主定理 → 证明 → 例子/应用。", "",
        "经验研究：问题 → 相关工作 → 方法 → 实验 → 讨论。", "",
        "先完成可核对的最小验证，再把实际结果写进草稿。期刊要求应在选定目标后查官方说明。", ""]
    if chosen:md += ["## 当前第一步", "", chosen["first_week"], "", "## 验证路线", "", chosen["validation"], ""]
    for key,label in zip(PROJECT_FIELDS,PROJECT_LABELS):md += ["## "+label, "", prose(project["notes"].get(key) or "待完成（不会自动伪造证明、实验或投稿记录）"), ""]
    return "\n".join(md)


def export_guidance(ctx):
    from agenthub_kernel.papers.export import _copy_out, _json, _meta, usage_of
    task,session=ctx.task,ctx.session
    studies=(task.outcome or {}).get("studies",{})
    directions=(task.outcome or {}).get("directions",{})
    state,dimensions=quality(studies,ctx.rules.review_agent)
    if len(studies)<ctx.request.core_papers:
        state="needs_review";dimensions["sources"]="missing"
    if ctx.request.mode!="study" and not directions.get("candidates"):state="needs_review"
    # A successful teaching check does not upgrade candidate novelty/validation.
    papers=list(session.scalars(select(PaperRow).where(PaperRow.task_id==task.id,PaperRow.included.is_(True)).order_by(PaperRow.rank)))
    references=[{"paper_id":p.id,"reference":reference_text(i+1,_meta(p))} for i,p in enumerate(papers)]
    appendix="\n\n## 参考文献\n\n"+"\n".join(f"- {r['paper_id']}：{r['reference']}" for r in references)
    study=render_studies(studies)+appendix
    plan=render_directions(directions)
    steps=list(session.scalars(select(PaperStepRow).where(PaperStepRow.task_id==task.id)))
    usage=usage_of(session,[i for st in steps for i in (st.invocation_ids or [])])
    project=project_of(session,task)
    report={"contract":"agenthub.guidance/1","task_id":task.id,"request":task.request,"studies":studies,
            "source_results":(task.outcome or {}).get("search_sources",[]),
            "directions":directions,"quality_status":state,"quality_dimensions":dimensions,"usage":usage,
            "usage_by_step":{n:usage_of(session,[i for s in steps if s.name==n for i in (s.invocation_ids or [])]) for n in {s.name for s in steps}}}
    files={"review.md":(study+"\n\n"+plan).encode("utf-8"),"study.md":study.encode("utf-8"),
           "research-plan.md":plan.encode("utf-8"),"references.json":_json(references),"research-report.json":_json(report)}
    if ctx.request.mode=="project":
        files.update({"paper-project.md":project_markdown(project,directions).encode("utf-8"),"project.json":_json(project)})
    from agenthub_kernel.papers.experiments import export_files
    from agenthub_kernel.papers.portable import enrich
    files.update(export_files(session,task))
    files=enrich(files)
    for name,data in files.items():
        rel,sha,size=ctx.ap.papers.store.write_export(task.id,task.input_version,name,data)
        row=session.scalar(select(ArtifactRow).where(ArtifactRow.task_id==task.id,ArtifactRow.version==task.input_version,ArtifactRow.name==name))
        if row is None:
            row=ArtifactRow(id=new_id("art"),task_id=task.id,version=task.input_version,name=name);session.add(row)
        row.rel_path,row.sha256,row.size,row.final=rel,sha,size,state=="passed"
    task.quality_status=state
    task.outcome={**(task.outcome or {}),"quality_dimensions":dimensions,
        "quality_reasons":[] if state=="passed" else ["请核对推导缺口；研究候选尚未查重和验证"],
        "export_copy":_copy_out(ctx,files,task.input_version)}
    return report
