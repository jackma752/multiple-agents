"""OFFLINE fixture corpus for the P6 paper pipeline: a fake arXiv API and PDFs.

Every paper here is SYNTHETIC, written for tests and demos; none is a real
publication and no title, author or arXiv id refers to one. The fake API answers
``export.arxiv.org/api/query`` by keyword-filtering this corpus and serves the
PDFs generated below, so the whole pipeline runs without the network.

Covered cases: two readable PDFs, a paper without a full-text link, a corrupt
PDF, a scanned-looking PDF, an off-topic paper, a paper without an abstract and
two duplicates (same title + first author; same DOI).
"""

from __future__ import annotations

import re
import textwrap
from typing import Optional
from urllib.parse import parse_qs, unquote, urlsplit
from xml.sax.saxutils import escape


def make_pdf(pages: list[str], *, text: bool = True) -> bytes:
    """A minimal valid PDF (Helvetica, one text object per page)."""
    objects: list[bytes] = []
    page_ids = []
    font_id = 3
    objects.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    objects.append(b"")  # pages, filled below
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>")
    for page in pages:
        lines = []
        for para in page.split("\n"):
            lines += textwrap.wrap(para, 95) or [""]
        if text:
            body = ["BT", "/F1 10 Tf", "12 TL", "50 760 Td"]
            for line in lines[:60]:
                safe = line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
                safe = safe.encode("latin-1", "replace").decode("latin-1")
                body.append(f"({safe}) Tj T*")
            body.append("ET")
        else:
            body = ["0 0 0 rg", "50 50 500 700 re f"]  # an image-like page without text
        stream = "\n".join(body).encode("latin-1")
        content_id = len(objects) + 1
        objects.append(b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream")
        page_id = len(objects) + 1
        objects.append(b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents %d 0 R "
                       b"/Resources << /Font << /F1 %d 0 R >> >> >>" % (content_id, font_id))
        page_ids.append(page_id)
    kids = b" ".join(b"%d 0 R" % i for i in page_ids)
    objects[1] = b"<< /Type /Pages /Kids [" + kids + b"] /Count %d >>" % len(page_ids)
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for n, obj in enumerate(objects, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % n + obj + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    for off in offsets:
        out += b"%010d 00000 n \n" % off
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, xref)
    return bytes(out)


def _pages(title: str, focus: str, finding: str, limit: str) -> list[str]:
    return [
        f"{title}\nSynthetic fixture paper for offline tests. Not a real publication.\n"
        f"Abstract. We study {focus} for retrieval augmented generation evaluation.\n"
        "1 Introduction\nRetrieval augmented generation systems combine a retriever with a generator. "
        f"Evaluating such systems requires attention to {focus}. This fixture describes a benchmark setting "
        "with 1,200 questions drawn from three public domains.",
        "2 Method\nWe construct an evaluation protocol that scores answers for faithfulness to retrieved passages. "
        f"The protocol measures {focus} using paired human judgements on 300 answers. "
        "Each answer is labelled by two annotators and disagreements are adjudicated.\n"
        f"3 Results\n{finding} The agreement between the automatic metric and human labels reached 0.71 on the "
        "held-out split.",
        f"4 Limitations\n{limit} The study covers English questions only and three domains.\n"
        "5 Conclusion\nThe protocol offers a reproducible way to compare retrieval augmented generation systems.",
    ]


CORPUS = [
    {"id": "2601.00101", "title": "Faithfulness Metrics for Retrieval Augmented Generation Evaluation",
     "authors": ["Lin Ana", "Okafor Ben"], "year": 2026, "doi": "10.5555/fixture.0101", "pdf": "ok",
     "abstract": "We propose faithfulness metrics for retrieval augmented generation evaluation and compare them "
                 "with human judgements on a benchmark of 1,200 questions.",
     "focus": "answer faithfulness", "finding": "Faithfulness scores correlated with human ratings more strongly than "
     "lexical overlap scores.", "limit": "Faithfulness labels depend on annotator guidelines."},
    {"id": "2601.00102", "title": "Benchmarking Retrieval Quality in Retrieval Augmented Generation",
     "authors": ["Moreau Celine", "Tan Wei"], "year": 2026, "doi": None, "pdf": "ok",
     "abstract": "This work benchmarks retrieval quality for retrieval augmented generation evaluation and shows "
                 "that retrieval recall bounds end-task accuracy.",
     "focus": "retrieval recall", "finding": "Higher retrieval recall was associated with higher answer accuracy, "
     "but gains flattened above recall 0.8.", "limit": "The benchmark uses a single retriever family."},
    {"id": "2601.00103", "title": "Human Evaluation Protocols for Retrieval Augmented Generation",
     "authors": ["Silva Duarte"], "year": 2025, "doi": None, "pdf": None,
     "abstract": "We review human evaluation protocols for retrieval augmented generation and report inter-annotator "
                 "agreement across three protocols.",
     "focus": "human evaluation protocols", "finding": "Pairwise protocols produced higher inter-annotator agreement "
     "than absolute rating scales.", "limit": "Annotators were recruited from a single platform."},
    {"id": "2601.00104", "title": "Cost-Aware Evaluation of Retrieval Augmented Generation Pipelines",
     "authors": ["Novak Eva", "Haddad Farid"], "year": 2026, "doi": None, "pdf": "corrupt",
     "abstract": "We study cost-aware evaluation of retrieval augmented generation pipelines, trading off "
                 "evaluation cost and reliability.",
     "focus": "evaluation cost", "finding": "Sampling 20 percent of answers for human review preserved system "
     "rankings in most settings.", "limit": "Cost estimates assume fixed annotator wages."},
    {"id": "2601.00105", "title": "Robustness Tests for Retrieval Augmented Generation Evaluation",
     "authors": ["Kowalski Grzegorz"], "year": 2025, "doi": None, "pdf": "scanned",
     "abstract": "We introduce robustness tests for retrieval augmented generation evaluation using perturbed "
                 "retrieval contexts.",
     "focus": "robustness to noisy context", "finding": "Injecting irrelevant passages reduced faithfulness scores "
     "by a large margin for all tested generators.", "limit": "Perturbations were synthetic."},
    {"id": "2601.00106", "title": "OFFTOPIC Protein Folding with Diffusion Models",
     "authors": ["Ivanova Hana"], "year": 2026, "doi": None, "pdf": "ok",
     "abstract": "A diffusion model for protein folding; unrelated to generation evaluation of text.",
     "focus": "protein structure", "finding": "Folding accuracy improved.", "limit": "Small proteins only."},
    {"id": "2601.00107", "title": "Evaluation Datasets for Retrieval Augmented Generation in Low Resource Languages",
     "authors": ["Ndlovu Iman"], "year": 2026, "doi": None, "pdf": None, "abstract": None,
     "focus": "low resource languages", "finding": "Datasets were scarce.", "limit": "Few languages."},
    # duplicates of the first paper (same title + first author; same DOI)
    {"id": "2601.00191", "title": "Faithfulness metrics for retrieval-augmented generation evaluation",
     "authors": ["Lin Ana", "Okafor Ben"], "year": 2026, "doi": None, "pdf": "ok",
     "abstract": "Duplicate record of a fixture paper.", "focus": "answer faithfulness", "finding": "Same.",
     "limit": "Same."},
    {"id": "2601.00192", "title": "A Different Title Sharing a DOI with the Faithfulness Fixture",
     "authors": ["Someone Else"], "year": 2026, "doi": "10.5555/FIXTURE.0101", "pdf": "ok",
     "abstract": "A record that shares the DOI of a fixture paper.", "focus": "x", "finding": "x", "limit": "x"},
]
BY_ID = {p["id"]: p for p in CORPUS}


def pages_of(paper: dict) -> list[str]:
    return _pages(paper["title"], paper["focus"], paper["finding"], paper["limit"])


def pdf_of(paper: dict) -> Optional[bytes]:
    kind = paper.get("pdf")
    if kind == "ok":
        return make_pdf(pages_of(paper))
    if kind == "corrupt":
        return b"%PDF-1.4\n1 0 obj << /Type /Catalog broken"
    if kind == "scanned":
        return make_pdf(["", "", ""], text=False)
    return None


def feed(entries: list[dict]) -> bytes:
    parts = ['<?xml version="1.0" encoding="UTF-8"?>',
             '<feed xmlns="http://www.w3.org/2005/Atom" xmlns:arxiv="http://arxiv.org/schemas/atom">',
             "<title>fixture</title>"]
    for p in entries:
        parts.append("<entry>")
        parts.append(f"<id>http://arxiv.org/abs/{p['id']}v1</id>")
        parts.append(f"<published>{p['year']}-01-15T00:00:00Z</published>")
        parts.append(f"<title>{escape(p['title'])}</title>")
        if p.get("abstract"):
            parts.append(f"<summary>{escape(p['abstract'])}</summary>")
        for a in p["authors"]:
            parts.append(f"<author><name>{escape(a)}</name></author>")
        if p.get("doi"):
            parts.append(f"<arxiv:doi>{escape(p['doi'])}</arxiv:doi>")
        parts.append(f'<link href="http://arxiv.org/abs/{p["id"]}v1" rel="alternate" type="text/html"/>')
        if p.get("pdf"):
            parts.append(f'<link title="pdf" href="http://arxiv.org/pdf/{p["id"]}v1" rel="related" type="application/pdf"/>')
        parts.append('<category term="cs.CL"/>')
        parts.append("</entry>")
    parts.append("</feed>")
    return "\n".join(parts).encode("utf-8")


_WORD = re.compile(r'all:"([^"]+)"|all:(\S+)')


class FixtureWeb:
    """``routes(url)`` for :class:`agenthub_kernel.papers.net.FakeFetcher`."""

    def __init__(self) -> None:
        self.extra: dict[str, tuple] = {}  # url -> (status, content_type, body)

    def __call__(self, url: str) -> Optional[tuple]:
        if url in self.extra:
            return self.extra[url]
        parts = urlsplit(url)
        if parts.hostname == "export.arxiv.org" and parts.path == "/api/query":
            q = unquote(parse_qs(parts.query).get("search_query", [""])[0])
            limit = int(parse_qs(parts.query).get("max_results", ["10"])[0])
            terms = [a or b for a, b in _WORD.findall(q)]
            words = {w for t in terms for w in re.findall(r"[a-z]+", t.lower()) if len(w) >= 4}
            hits = [p for p in CORPUS if words & set(re.findall(r"[a-z]+", (p["title"] + " " + (p.get("abstract") or "")).lower()))]
            return 200, "application/atom+xml", feed(hits[:limit])
        if parts.hostname == "arxiv.org" and parts.path.startswith("/pdf/"):
            paper = BY_ID.get(parts.path[len("/pdf/"):].split("v")[0])
            data = pdf_of(paper) if paper else None
            return (200, "application/pdf", data) if data else (404, "text/html", b"not found")
        return None


def supplement_pdf(paper_id: str) -> bytes:
    """A readable full text of a fixture paper, as a user would upload it."""
    return make_pdf(pages_of(BY_ID[paper_id]))
