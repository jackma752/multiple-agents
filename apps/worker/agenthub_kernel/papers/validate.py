"""Structural checks of model step outputs. Deterministic; no model involved.

A :class:`FormatError` means the reply is not usable as given: the step gets a
bounded repair call (counted and authorized like any other). Content problems
that are detectable (a quote not present in the source, an unknown id) are not
format errors: the item is dropped or flagged and the problem is recorded.
"""

from __future__ import annotations

import json
import re
from typing import Any, Iterable

from agenthub_kernel.papers.parse import quote_in


class FormatError(ValueError):
    pass


_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


def parse_json(text: str) -> dict[str, Any]:
    candidates = [m.group(1) for m in _FENCE.finditer(text or "")] + [text or ""]
    for candidate in candidates:
        start = candidate.find("{")
        end = candidate.rfind("}")
        if start < 0 or end <= start:
            continue
        try:
            value = json.loads(candidate[start:end + 1])
        except ValueError:
            continue
        if isinstance(value, dict):
            return value
    raise FormatError("reply is not one JSON object")


def _strs(value: Any, name: str, *, limit: int = 20, required: bool = False) -> list[str]:
    if value is None and not required:
        return []
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise FormatError(f"{name} must be a list of strings")
    out = [" ".join(v.split()) for v in value if v.strip()]
    if required and not out:
        raise FormatError(f"{name} must not be empty")
    return out[:limit]


def plan(out: dict[str, Any]) -> dict[str, Any]:
    queries = out.get("queries")
    if not isinstance(queries, list) or not queries:
        raise FormatError("queries must be a non-empty list")
    clean = []
    for q in queries[:3]:
        if not isinstance(q, dict):
            raise FormatError("each query must be an object")
        terms = _strs(q.get("terms"), "queries[].terms", limit=4, required=True)
        clean.append({"terms": terms, "rationale": str(q.get("rationale") or "")[:300]})
    return {"queries": clean, "subtopics": _strs(out.get("subtopics"), "subtopics", limit=4, required=True),
            "inclusion": _strs(out.get("inclusion"), "inclusion"), "exclusion": _strs(out.get("exclusion"), "exclusion"),
            "ambiguities": _strs(out.get("ambiguities"), "ambiguities")}


def screen(out: dict[str, Any], ids: Iterable[str]) -> dict[str, dict]:
    expected = set(ids)
    decisions = out.get("decisions")
    if not isinstance(decisions, list):
        raise FormatError("decisions must be a list")
    seen: dict[str, dict] = {}
    for d in decisions:
        if not isinstance(d, dict) or d.get("paper_id") not in expected:
            continue  # an id we did not send is ignored, never added as a paper
        if d.get("decision") not in ("include", "exclude", "uncertain"):
            raise FormatError("decision must be include, exclude or uncertain")
        seen[d["paper_id"]] = {"decision": d["decision"], "reason": str(d.get("reason") or "")[:400]}
    missing = expected - set(seen)
    if missing:
        raise FormatError(f"no decision for {len(missing)} candidate(s)")
    return seen


KINDS = ("method", "data", "result", "limitation", "claim")


def extract(out: dict[str, Any], *, chunks: dict[str, str], abstract: str, level: str) -> dict[str, Any]:
    """Keep claims whose quote is found in the named source; report the rest."""
    claims = out.get("claims")
    if not isinstance(claims, list):
        raise FormatError("claims must be a list")
    kept, dropped = [], []
    for c in claims[:30]:
        if not isinstance(c, dict) or not isinstance(c.get("statement"), str) or not isinstance(c.get("quote"), str):
            raise FormatError("each claim needs statement and quote strings")
        chunk_id = c.get("chunk_id")
        source = chunks.get(chunk_id) if chunk_id else (abstract if level == "abstract" or not chunks else None)
        if chunk_id and chunk_id not in chunks:
            dropped.append({"statement": c["statement"][:200], "why": "unknown_chunk"})
            continue
        if source is None:
            dropped.append({"statement": c["statement"][:200], "why": "no_source_named"})
            continue
        if not quote_in(c["quote"], source):
            dropped.append({"statement": c["statement"][:200], "why": "quote_not_in_source"})
            continue
        kept.append({"statement": " ".join(c["statement"].split())[:800], "quote": " ".join(c["quote"].split())[:600],
                     "chunk_id": chunk_id, "kind": c.get("kind") if c.get("kind") in KINDS else "claim",
                     "attribution": "speculated" if c.get("attribution") == "speculated" else "reported",
                     "subtopic": c.get("subtopic") if isinstance(c.get("subtopic"), str) else None})
    if claims and not kept:
        raise FormatError("no claim quote was found verbatim in the material")
    return {"claims": kept, "dropped": dropped, "missing_fields": _strs(out.get("missing_fields"), "missing_fields"),
            "coverage": str(out.get("coverage") or "")[:300], "warnings": _strs(out.get("warnings"), "warnings")}


def _ids(value: Any, known: set, unknown: list) -> list[str]:
    if not isinstance(value, list):
        return []
    out = []
    for v in value:
        if isinstance(v, str) and v in known:
            if v not in out:
                out.append(v)
        elif isinstance(v, str):
            unknown.append(v)
    return out


def synthesize(out: dict[str, Any], known: set) -> dict[str, Any]:
    unknown: list[str] = []

    def rows(name: str) -> list[dict]:
        value = out.get(name) or []
        if not isinstance(value, list):
            raise FormatError(f"{name} must be a list")
        return [{"text": " ".join(str(r.get("text") or r.get("summary") or "").split())[:1200],
                 "name": str(r.get("name") or "")[:120] or None,
                 "evidence_ids": _ids(r.get("evidence_ids"), known, unknown)}
                for r in value if isinstance(r, dict)]

    subtopics = rows("subtopics")
    if not subtopics:
        raise FormatError("subtopics must not be empty")
    return {"subtopics": subtopics, "agreements": rows("agreements"), "conflicts": rows("conflicts"),
            "gaps": rows("gaps"), "coverage_limits": _strs(out.get("coverage_limits"), "coverage_limits"),
            "unknown_ids": sorted(set(unknown))}


def outline(out: dict[str, Any], known: set, sections: int) -> dict[str, Any]:
    raw = out.get("sections")
    if not isinstance(raw, list) or not raw:
        raise FormatError("sections must be a non-empty list")
    unknown: list[str] = []
    clean = []
    for s in raw[:sections + 1]:
        if not isinstance(s, dict) or not isinstance(s.get("title"), str) or not s["title"].strip():
            raise FormatError("each section needs a title")
        clean.append({"title": " ".join(s["title"].split())[:200], "goal": str(s.get("goal") or "")[:800],
                      "evidence_ids": _ids(s.get("evidence_ids"), known, unknown),
                      "gaps": _strs(s.get("gaps"), "sections[].gaps")})
    return {"title": " ".join(str(out.get("title") or "").split())[:200], "sections": clean,
            "unknown_ids": sorted(set(unknown))}


MARKER = re.compile(r"\[ev:([A-Za-z0-9_-]+)\]")
EVIDENCE_PREFIX = "ev-"


def resolve_marker(marker: str, known: set) -> str | None:
    """The evidence id a ``[ev:...]`` marker names, or None.

    Models sometimes write ``[ev:04c3832d91]`` for ``ev-04c3832d91``. That is
    accepted only when adding the prefix gives exactly an id of this task -
    no fuzzy matching, so a made-up marker stays unknown.
    """
    if marker in known:
        return marker
    if not marker.startswith(EVIDENCE_PREFIX) and EVIDENCE_PREFIX + marker in known:
        return EVIDENCE_PREFIX + marker
    return None


def section(out: dict[str, Any], known: set) -> dict[str, Any]:
    text = out.get("text")
    if not isinstance(text, str) or len(text.strip()) < 20:
        raise FormatError("text must be the section body")
    markers = MARKER.findall(text)
    unknown: list[str] = [m for m in markers if resolve_marker(m, known) is None]
    claims = []
    for c in out.get("claims") or []:
        if isinstance(c, dict) and isinstance(c.get("text"), str):
            ids = [resolve_marker(i, known) or i for i in c.get("evidence_ids") or [] if isinstance(i, str)]
            claims.append({"text": c["text"][:600], "evidence_ids": _ids(ids, known, unknown)})
    return {"text": text.strip(), "claims": claims[:40], "todo": _strs(out.get("todo"), "todo"),
            "unknown_ids": sorted(set(unknown)),
            "cited": sorted({r for r in (resolve_marker(m, known) for m in markers) if r})}


def review(out: dict[str, Any]) -> dict[str, Any]:
    verdict = out.get("verdict")
    if verdict not in ("pass", "revise", "insufficient"):
        raise FormatError("verdict must be pass, revise or insufficient")
    issues = []
    for i in out.get("issues") or []:
        if isinstance(i, dict):
            issues.append({"severity": i.get("severity") if i.get("severity") in ("critical", "major", "minor")
                           else "major", "location": str(i.get("location") or "")[:300],
                           "problem": str(i.get("problem") or "")[:600], "suggestion": str(i.get("suggestion") or "")[:600]})
    if verdict == "pass" and any(i["severity"] in ("critical", "major") for i in issues):
        verdict = "revise"  # a pass that lists evidence problems is not a pass
    return {"verdict": verdict, "issues": issues[:20]}


def abstract(out: dict[str, Any]) -> dict[str, Any]:
    text = out.get("abstract")
    if not isinstance(text, str) or len(text.strip()) < 20:
        raise FormatError("abstract must be text")
    return {"abstract": text.strip()[:3000], "keywords": _strs(out.get("keywords"), "keywords", limit=8)}
