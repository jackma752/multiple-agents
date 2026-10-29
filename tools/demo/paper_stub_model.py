"""OFFLINE stand-in for a model answering P6 research templates. Not a model.

``respond(prompt)`` reads the template id and DATA block the Hub rendered and
returns deterministic JSON built only from that data (quotes are copied from
the given chunks, ids from the given lists). It exists so the pipeline,
gates and checks can be exercised end to end without any provider; its prose
is placeholder text and says nothing about real research quality.

Markers in the topic switch on failure paths for tests:
``SIM_BAD_JSON`` (first plan reply is not JSON), ``SIM_FORGE`` (extraction
quotes that are not in the source), ``SIM_REVISE`` (first review asks for a
revision), ``SIM_UNKNOWN_EV`` (a section cites an evidence id that does not exist),
``SIM_SHORT_EV`` (sections cite evidence without the ``ev-`` prefix).
"""

from __future__ import annotations

import json
import re
from typing import Any

_TEMPLATE = re.compile(r"^AGENTHUB-TEMPLATE: (\S+)", re.MULTILINE)
_DATA = re.compile(r"<<<DATA \(untrusted material\)\n(.*)\nDATA>>>", re.DOTALL)


def _data(prompt: str) -> dict[str, Any]:
    m = _DATA.search(prompt)
    return json.loads(m.group(1)) if m else {}


def _sentences(text: str) -> list[str]:
    out = []
    for s in re.split(r"(?<=[.。])\s+|\n", text or ""):
        s = s.strip()
        if 25 <= len(s) <= 280:
            out.append(s)
    return out


def respond(prompt: str) -> str:
    m = _TEMPLATE.search(prompt)
    if not m:
        return "Offline stage step completed."
    template, data = m.group(1), _data(prompt)
    repair = "Repair attempt" in prompt
    topic = data.get("topic", "")
    if template == "research.plan.v1":
        if "SIM_BAD_JSON" in topic and not repair:
            return "I think the plan is: search for papers."  # not JSON: triggers one repair call
        return json.dumps({"subtopics": ["faithfulness", "retrieval quality", "human evaluation"],
                           "queries": [{"terms": ["retrieval augmented generation", "evaluation"], "rationale": "core"},
                                       {"terms": ["retrieval augmented generation", "faithfulness"], "rationale": "metric"}],
                           "inclusion": ["evaluates retrieval augmented generation"],
                           "exclusion": ["not about text generation"], "ambiguities": []})
    if template == "research.screen.v1":
        decisions = []
        for c in data.get("candidates", []):
            title = (c.get("title") or "").lower()
            if "offtopic" in title or "protein" in title:
                d, why = "exclude", "主题无关（蛋白质结构）"
            elif not c.get("abstract"):
                d, why = "uncertain", "缺少摘要，仅凭题名判断"
            else:
                d, why = "include", "评测检索增强生成"
            decisions.append({"paper_id": c["paper_id"], "decision": d, "reason": why, "criteria": []})
        return json.dumps({"decisions": decisions}, ensure_ascii=False)
    if template == "research.extract.v1":
        claims = []
        kinds = ["method", "result", "limitation", "data"]
        if data.get("level") == "abstract":
            for s in _sentences(data.get("abstract", ""))[:1]:
                claims.append({"statement": "摘要报告：" + s[:60], "kind": "claim", "attribution": "reported",
                               "quote": s, "chunk_id": None, "subtopic": None})
        else:
            for n, c in enumerate(data.get("chunks", [])[:4]):
                sentences = _sentences(c["text"])
                if not sentences:
                    continue
                pick = next((s for s in sentences if any(k in s.lower() for k in ("score", "recall", "agreement",
                                                                                   "protocol", "limited", "only"))),
                            sentences[0])
                quote = pick if "SIM_FORGE" not in topic else "This sentence does not appear anywhere in the paper."
                claims.append({"statement": "论文报告：" + pick[:80], "kind": kinds[n % 4], "attribution": "reported",
                               "quote": quote, "chunk_id": c["chunk_id"], "subtopic": None})
        return json.dumps({"claims": claims, "missing_fields": [], "coverage": "selected chunks only",
                           "warnings": []}, ensure_ascii=False)
    if template == "research.synthesize.v1":
        ev = [e["evidence_id"] for e in data.get("evidence", [])]
        subs = data.get("subtopics") or ["general"]
        n = data.get("sample", {}).get("papers", 0)
        return json.dumps({
            "subtopics": [{"name": s, "summary": f"{s} 方面的证据汇总（模拟）", "evidence_ids": ev[i::len(subs)]}
                          for i, s in enumerate(subs)],
            "agreements": [{"text": "多篇论文都以人工判断作为参照（模拟）", "evidence_ids": ev[:2]}],
            "conflicts": [], "gaps": [{"text": f"当前样本（{n} 篇）中未发现多语言评测", "evidence_ids": []}],
            "coverage_limits": ["部分论文仅有摘要"]}, ensure_ascii=False)
    if template == "research.outline.v1":
        ev = [e["evidence_id"] for e in data.get("evidence_catalog", [])]
        k = int(data.get("sections") or 3)
        return json.dumps({"title": "检索增强生成评测研究综述（离线模拟）", "sections": [
            {"title": f"第{i + 1}部分", "goal": "归纳该方面的证据", "evidence_ids": ev[i::k], "gaps": []}
            for i in range(k)]}, ensure_ascii=False)
    if template == "research.section.v1":
        pack = data.get("evidence_pack", [])
        short = "SIM_SHORT_EV" in topic  # writes [ev:04c3...] for ev-04c3..., as a real model once did
        body = "".join(f"有研究指出：{e['statement'][:60]}[ev:{e['evidence_id'][3:] if short else e['evidence_id']}]。"
                       for e in pack[:4])
        if "SIM_UNKNOWN_EV" in topic:
            body += "另一项结论缺少对应证据[ev:ev-0000000000]。"
        if "SIM_REVISE" in topic:
            body += "（SIM_REVISE）"
        if data.get("review_notes"):
            body += "（修订稿：已按审阅意见补充适用条件。）"
        claims = [{"text": e["statement"][:60], "evidence_ids": [e["evidence_id"]]} for e in pack[:4]]
        return json.dumps({"text": body or "本节资料不足。", "claims": claims, "todo": []}, ensure_ascii=False)
    if template == "research.review.v1":
        text = (data.get("section") or {}).get("text", "")
        if "SIM_REVISE" in json.dumps(data, ensure_ascii=False) and "修订稿" not in text:
            return json.dumps({"verdict": "revise", "issues": [
                {"severity": "major", "location": text[:40], "problem": "未说明适用条件", "evidence_ids": [],
                 "suggestion": "补充研究设置"}]}, ensure_ascii=False)
        return json.dumps({"verdict": "pass", "issues": []})
    if template == "research.abstract.v1":
        n = (data.get("scope") or {}).get("papers", 0)
        return json.dumps({"abstract": f"本文基于 {n} 篇论文的离线模拟材料生成，用于验证流程，不代表真实研究结论。",
                           "keywords": ["检索增强生成", "评测"]}, ensure_ascii=False)
    return "{}"


class DeepSeekPaperTransport:
    """An offline DeepSeek-shaped stream whose content is :func:`respond`."""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, payload: dict, headers: dict):
        self.calls += 1
        prompt = (payload.get("messages") or [{}])[-1].get("content", "")
        reply = respond(prompt)
        yield {"id": "sim", "model": "deepseek-sim", "choices": [{"index": 0, "delta": {"content": reply},
                                                                  "finish_reason": "stop"}]}
        yield {"id": "sim", "model": "deepseek-sim", "choices": [],
               "usage": {"prompt_tokens": 40, "completion_tokens": 20, "total_tokens": 60,
                         "prompt_cache_hit_tokens": 0, "prompt_cache_miss_tokens": 40}}
