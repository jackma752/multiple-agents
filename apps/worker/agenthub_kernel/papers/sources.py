"""Public literature adapters: bounded query construction and normalized metadata.

No I/O, credentials, model calls or authorization here; pipeline tool execution
uses the approved fetcher and checks every redirect. Missing abstracts/PDFs stay
missing. The default arXiv connector retains its existing implementation.
"""
from __future__ import annotations

import json
import re
from html import unescape
from urllib.parse import urlencode, urlsplit

from agenthub_kernel.papers import arxiv


def text(value) -> str:
    return " ".join(unescape(re.sub(r"<[^>]*>", " ", str(value or ""))).split())


def doi(value) -> str | None:
    value = str(value or "").strip().lower()
    value = re.sub(r"^https?://(?:dx\.)?doi\.org/|^doi:\s*", "", value)
    return value if value.startswith("10.") and "/" in value else None


def _url(value):
    if not isinstance(value, str) or len(value)>2048: return None
    try: parts=urlsplit(value)
    except ValueError: return None
    return value if parts.scheme in ('http','https') and parts.hostname and not parts.username and not parts.password else None


def query_url(source: str, terms: list, *, year_from=None, year_to=None, max_results=15) -> str:
    if source == "arxiv":
        return arxiv.query_url(terms, year_from=year_from, year_to=year_to, max_results=max_results)
    query = " ".join(arxiv.clean_terms(terms))
    if not query:
        raise ValueError("empty search terms")
    limit = max(1, min(int(max_results), 50))
    if source == "openalex":
        params = {"search": query, "per-page": limit}
        filters = []
        if year_from: filters.append(f"from_publication_date:{year_from}-01-01")
        if year_to: filters.append(f"to_publication_date:{year_to}-12-31")
        if filters: params["filter"] = ",".join(filters)
        return "https://api.openalex.org/works?" + urlencode(params)
    if source == "crossref":
        params = {"query.bibliographic": query, "rows": limit}
        filters = []
        if year_from: filters.append(f"from-pub-date:{year_from}-01-01")
        if year_to: filters.append(f"until-pub-date:{year_to}-12-31")
        if filters: params["filter"] = ",".join(filters)
        return "https://api.crossref.org/works?" + urlencode(params)
    if source == "semantic_scholar":
        params = {"query": query, "limit": limit,
                  "fields": "paperId,title,authors,year,abstract,venue,url,externalIds,openAccessPdf"}
        if year_from or year_to: params["year"] = f"{year_from or ''}-{year_to or ''}"
        return "https://api.semanticscholar.org/graph/v1/paper/search?" + urlencode(params)
    raise ValueError("unknown literature source")


def _abstract(index):
    if not isinstance(index, dict): return None
    words = {}
    for word, positions in index.items():
        for pos in positions if isinstance(positions, list) else []:
            if type(pos) is int and 0 <= pos < 50_000: words[pos] = str(word)
    return text(" ".join(words[p] for p in sorted(words))) or None


def _record(source, sid, title, authors, year, *, abstract=None, url=None, pdf=None, identifier=None, venue=None):
    return {"source": source, "source_id": str(sid), "source_version": None, "title": text(title),
            "authors": [text(a) for a in authors if text(a)], "year": year if type(year) is int else None,
            "published": None, "venue": text(venue) or None, "doi": doi(identifier), "url": _url(url),
            "pdf_url": _url(pdf), "abstract": text(abstract) or None, "categories": []}


def parse_results(source: str, body: bytes) -> list[dict]:
    if source == "arxiv": return arxiv.parse_feed(body)
    try: data = json.loads(body)
    except (ValueError, UnicodeError) as exc: raise ValueError("invalid source JSON") from exc
    if not isinstance(data, dict): raise ValueError("invalid source response")
    rows = []
    if source == "openalex":
        if not isinstance(data.get("results"), list): raise ValueError("OpenAlex results missing")
        for w in data["results"]:
            if not isinstance(w, dict) or not w.get("id") or not w.get("title"): continue
            location = w.get("best_oa_location") or w.get("primary_location") or {}
            primary = w.get("primary_location") or {}
            rows.append(_record(source, w["id"], w["title"],
                [(a.get("author") or {}).get("display_name") for a in w.get("authorships", []) if isinstance(a, dict)],
                w.get("publication_year"), abstract=_abstract(w.get("abstract_inverted_index")),
                identifier=w.get("doi"), url=primary.get("landing_page_url") or w["id"],
                pdf=location.get("pdf_url"), venue=(primary.get("source") or {}).get("display_name")))
    elif source == "crossref":
        items = (data.get("message") or {}).get("items")
        if not isinstance(items, list): raise ValueError("Crossref items missing")
        for w in items:
            titles = w.get("title") or []
            if not w.get("DOI") or not titles: continue
            parts = ((w.get("published") or w.get("issued") or {}).get("date-parts") or [[]])[0]
            # Crossref link metadata may be TDM-only; do not treat it as open access.
            rows.append(_record(source, doi(w["DOI"]), titles[0],
                [" ".join(filter(None, [a.get("given"), a.get("family")])) or a.get("name", "")
                 for a in w.get("author", [])], parts[0] if parts else None,
                abstract=w.get("abstract"), identifier=w["DOI"], url=w.get("URL"),
                venue=(w.get("container-title") or [None])[0]))
    elif source == "semantic_scholar":
        if not isinstance(data.get("data"), list): raise ValueError("Semantic Scholar data missing")
        for w in data["data"]:
            if not w.get("paperId") or not w.get("title"): continue
            rows.append(_record(source, w["paperId"], w["title"],
                [a.get("name") for a in w.get("authors", [])], w.get("year"), abstract=w.get("abstract"),
                identifier=(w.get("externalIds") or {}).get("DOI"), url=w.get("url"),
                pdf=(w.get("openAccessPdf") or {}).get("url"), venue=w.get("venue")))
    else: raise ValueError("unknown literature source")
    return rows
