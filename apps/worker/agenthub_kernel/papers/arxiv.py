"""arXiv connector: query URL construction and Atom parsing (no I/O here).

The query uses the public export API (``export.arxiv.org/api/query``). Terms come
from the validated search plan; they are quoted and joined, never passed as
raw query syntax, so a plan cannot reach another endpoint or parameter.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from typing import Any, Optional
from urllib.parse import quote

API = "https://export.arxiv.org/api/query"
ATOM = "{http://www.w3.org/2005/Atom}"
ARXIV = "{http://arxiv.org/schemas/atom}"
_ID = re.compile(r"arxiv\.org/abs/([^\s?#]+?)(v\d+)?$")
_TERM = re.compile(r"[^\w\s\-]+", re.UNICODE)
MAX_TERMS = 6


def clean_terms(terms: list) -> list[str]:
    out: list[str] = []
    for term in terms or []:
        if not isinstance(term, str):
            continue
        text = " ".join(_TERM.sub(" ", term).split())[:60]
        if text and text.lower() not in (t.lower() for t in out):
            out.append(text)
    return out[:MAX_TERMS]


def query_url(terms: list, *, year_from: Optional[int], year_to: Optional[int], max_results: int) -> str:
    words = clean_terms(terms)
    if not words:
        raise ValueError("empty search terms")
    expr = " AND ".join(f'all:"{w}"' if " " in w else f"all:{w}" for w in words)
    if year_from or year_to:
        expr = f"({expr}) AND submittedDate:[{year_from or 1990}01010000 TO {year_to or 2100}12312359]"
    return (f"{API}?search_query={quote(expr, safe='')}&start=0&max_results={int(max_results)}"
            "&sortBy=relevance&sortOrder=descending")


def _text(node: Optional[ET.Element]) -> str:
    return " ".join((node.text or "").split()) if node is not None else ""


def parse_feed(body: bytes) -> list[dict[str, Any]]:
    """Entries of an arXiv Atom feed; ValueError when it is not one."""
    try:
        root = ET.fromstring(body)
    except ET.ParseError as exc:
        raise ValueError("not an Atom feed") from exc
    if root.tag != f"{ATOM}feed":
        raise ValueError("not an Atom feed")
    out = []
    for entry in root.findall(f"{ATOM}entry"):
        raw_id = _text(entry.find(f"{ATOM}id"))
        match = _ID.search(raw_id)
        if not match:
            continue  # arXiv returns an error "entry" without an abs id on bad queries
        source_id, version = match.group(1), (match.group(2) or "").lstrip("v") or None
        published = _text(entry.find(f"{ATOM}published"))
        pdf_url = None
        for link in entry.findall(f"{ATOM}link"):
            if link.get("title") == "pdf" or link.get("type") == "application/pdf":
                pdf_url = link.get("href")
        if pdf_url and pdf_url.startswith("http://"):
            pdf_url = "https://" + pdf_url[len("http://"):]
        doi = _text(entry.find(f"{ARXIV}doi")) or None
        out.append({
            "source": "arxiv", "source_id": source_id, "source_version": version,
            "title": _text(entry.find(f"{ATOM}title")),
            "authors": [_text(a.find(f"{ATOM}name")) for a in entry.findall(f"{ATOM}author")
                        if _text(a.find(f"{ATOM}name"))],
            "abstract": _text(entry.find(f"{ATOM}summary")) or None,
            "published": published or None,
            "year": int(published[:4]) if published[:4].isdigit() else None,
            "doi": doi,
            "venue": _text(entry.find(f"{ARXIV}journal_ref")) or None,
            "url": f"https://arxiv.org/abs/{source_id}",
            "pdf_url": pdf_url,
            "categories": [c.get("term") for c in entry.findall(f"{ATOM}category") if c.get("term")],
        })
    return out
