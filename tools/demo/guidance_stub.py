"""Authored, offline contract fixtures. NOT paper interpretation or quality evidence."""
import copy


def teaching(data):
    c=data["chunks"][0]
    units=[]
    fields=[("problem","材料中的问题",c["text"][:150]),("intuition","直观解释","以下是流程验收使用的教学算例，不代表材料原文的数学结论。"),
        ("prerequisites","预备知识","几何级数、完备度量空间、Cauchy 序列。"),
        ("symbols","符号","x_n 是第 n 次迭代；d 为距离；q 为压缩常数。"),
        ("setting","数学设定","教学算例：在实数上取 T(x)=(x+1)/2。"),
        ("assumption","假设","经典教学算例需要完备性以及 0≤q<1。"),
        ("result","目标结论","说明经典收敛链；这不是对所给样本的新定理。"),
        ("step","相邻步距离","由压缩条件递推，得到几何衰减。"),
        ("step","Cauchy 估计","对相邻步距离求和，尾项趋于零；完备性给出极限。"),
        ("example","手算例子","x_0=0，x_1=1/2，x_2=3/4，极限是 1。"),
        ("next","下一项学习任务","解释完备性在哪一步使用，并检查不完备空间的反例。")]
    counts={}
    for kind,title,text in fields:
        counts[kind]=counts.get(kind,0)+1
        uid=kind+str(counts[kind])
        dep=[] if kind=="problem" else [units[-1]["id"]]
        units.append({"id":uid,"kind":kind,"title":title,"text":text,"latex":"",
            "reason":"由前述假设和三角不等式/几何级数得到。" if kind in ("step","example","assumption") else "",
            "definitions":[{"symbol":"x_n","meaning":"第 n 次迭代","domain":"实数"},{"symbol":"q","meaning":"压缩常数","domain":"[0,1)"}] if kind=="symbols" else [],
            "origin":"source" if kind=="problem" else "reconstruction","depends_on":dep,
            "sources":[{"chunk_id":c["chunk_id"],"quote":c["text"][:150]}] if kind in ("problem","setting","assumption","result") else []})
    units[7]["latex"]=r"d(x_{n+1},x_n)\le q^n d(x_1,x_0)"
    units[8]["latex"]=r"d(x_m,x_n)\le \frac{q^n}{1-q}d(x_1,x_0)\to0"
    missing=[{"reason":"需要 Appendix Z 的完整证明","pages":[999],"terms":["Appendix Z"]}] if "SIM_STUDY_GAP" in data.get("topic","") else []
    return {"units":units,"math_state":"incomplete" if missing else "complete","missing":missing}


def respond(template,data):
    if template == 'research.source-select.v1':
        return {'selections': [{'unit_id': i['unit_id'], 'source_indices': list(range(i['limit']))}
                               for i in data['issues']]}
    if template in ("research.study.v1", "research.study.v2"):return teaching(data)
    if template == 'research.source-repair.v1':
        c = data['chunks'][0]
        return {'replacements': [{'unit_id': i['unit_id'], 'source_index': i['source_index'],
            'source': {'source_id': c['source_id'], 'quote': c['text'][:150]}} for i in data['issues']]}
    if template=="research.study-review.v1":
        last=next((u for u in data["units"] if u["id"]=="step2"),None)
        return {"issues":[{"id":"math-1","unit_id":"step2","severity":"major","kind":"math",
            "problem":"请明确写出 q<1 的作用","closure":"解释几何级数尾项","pages":[],"terms":[]}]
            if last and "修复后" not in last["text"] else []}
    if template=="research.study-patch.v1":
        units=[copy.deepcopy(u) for u in data["units"] if u["id"] in data["editable_ids"]]
        for u in units:u["text"]+=" 修复后：q<1 保证几何级数尾和趋于零。"
        return {"replacements":units,"math_state":"complete","missing":[]}
    if template=="research.directions.v1":
        return {"comparison":"教学演示：应逐项比较假设和关键估计，不做跨设定排名。","candidates":[{
            "id":name,"question":"核对迭代条件是否必要（模拟候选）","baseline":"经典压缩教学算例",
            "change":"改变一个假设后先搜索反例","rationale":"用反例排除无效推广","method":r"x_{n+1}=Tx_n",
            "validation":"先手算 T(x)=(x+1)/2，再检查边界参数。","success":"证明目标命题或找到反例",
            "failure_plan":"收紧假设并检查是否等价于已有结论","prerequisites":"完备性、几何级数；无额外计算工具要求",
            "first_week":"第 1 天解释基线；第 2 天列假设；第 3 天手算；随后查找相近定理。",
            "source_papers":[data["studies"][0]["paper_id"]],"novelty":"unverified"} for name in ("A","B")]}
    return None
